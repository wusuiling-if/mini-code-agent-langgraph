from __future__ import annotations

import os
import shlex
import socket
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mini_code_agent.executor import BashExecutor
from mini_code_agent.utils import truncate_text


_CONNECTION_BLOCKED_EXIT = 73
_SOCKET_VISIBLE_BLOCKED_EXIT = 74
_BOUNDARY_EVIDENCE_EXIT = 75
_UNEXPECTED_OSERROR_EXIT = 76
_DETAIL_LIMIT = 500
_HOST_TEMP_BASE = Path("/var/tmp")
_PROTECTED_CONTENT = "protected\n"


@dataclass(frozen=True)
class SandboxCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class SandboxProbeReport:
    backend: str
    checks: tuple[SandboxCheck, ...]

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)


def _redact(executor: Any, detail: str) -> str:
    redacted = executor.redactor.redact_text(detail)
    return truncate_text(redacted, _DETAIL_LIMIT)[:_DETAIL_LIMIT]


def _result_detail(executor: Any, result: Any, summary: str) -> str:
    parts = [summary, f"exit={result.returncode}"]
    output = str(result.output or "").strip()
    if output:
        parts.append(output)
    exception_info = str(result.exception_info or "").strip()
    if exception_info:
        parts.append(exception_info)
    return _redact(executor, ": ".join(parts))


def _returned(result: Any, returncode: int) -> bool:
    return result.returncode == returncode and not str(
        result.exception_info or ""
    ).strip()


def _verified_host_temp_base(candidate: Path = _HOST_TEMP_BASE) -> Path | None:
    """Return a host-visible temp base only after proving it writable."""

    fd: int | None = None
    raw_path: str | None = None
    resolved_candidate: Path | None = None
    verified = False
    try:
        resolved_candidate = candidate.resolve(strict=True)
        masked_tmp = Path("/tmp").resolve(strict=True)
        try:
            resolved_candidate.relative_to(masked_tmp)
        except ValueError:
            pass
        else:
            return None
        if not resolved_candidate.is_dir():
            return None
        fd, raw_path = tempfile.mkstemp(
            prefix=".mca-sandbox-probe-", dir=resolved_candidate
        )
        verified = os.write(fd, b"verified\n") == len(b"verified\n")
    except OSError:
        verified = False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                verified = False
        if raw_path is not None:
            try:
                Path(raw_path).unlink()
            except OSError:
                verified = False
    return resolved_candidate if verified and resolved_candidate is not None else None


def _python_command(source: str) -> str:
    return f"python3 -c {shlex.quote(source)}"


def _known_unix_sockets() -> list[Path]:
    candidates = [
        Path("/var/run/docker.sock"),
        Path("/run/docker.sock"),
        Path("/var/run/dbus/system_bus_socket"),
        Path("/run/dbus/system_bus_socket"),
    ]
    runtime_dir = os.getenv("XDG_RUNTIME_DIR")
    if runtime_dir:
        candidates.append(Path(runtime_dir) / "bus")
    if hasattr(os, "getuid"):
        candidates.append(Path("/run/user") / str(os.getuid()) / "bus")

    sockets: list[Path] = []
    for candidate in candidates:
        try:
            if stat.S_ISSOCK(candidate.stat().st_mode):
                resolved = candidate.resolve()
                if resolved not in sockets:
                    sockets.append(resolved)
        except (FileNotFoundError, OSError):
            continue
    return sockets


def _unix_socket_source(paths: list[Path]) -> str:
    raw_paths = [str(path) for path in paths]
    return (
        "import socket,sys; "
        f"paths={raw_paths!r}; "
        "visible=[]; errors=[]; "
        "\nfor path in paths:"
        "\n try: visible_now=__import__('os').path.exists(path)"
        "\n except OSError: visible_now=False"
        "\n if visible_now: visible.append(path)"
        "\n sock=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); sock.settimeout(2)"
        "\n try: sock.connect(path)"
        "\n except OSError as exc: errors.append(type(exc).__name__)"
        "\n else: print('reachable:' + path); sys.exit(0)"
        "\n finally: sock.close()"
        "\nif visible: print('visible-but-blocked:' + ','.join(visible) + ':' + ','.join(errors)); "
        f"sys.exit({_SOCKET_VISIBLE_BLOCKED_EXIT})"
        "\nprint('invisible:' + ','.join(errors)); "
        f"sys.exit({_CONNECTION_BLOCKED_EXIT})"
    )


def _tcp_source(port: int) -> str:
    return (
        "import socket,sys; "
        "sock=socket.socket(socket.AF_INET, socket.SOCK_STREAM); sock.settimeout(2); "
        "\ntry: sock.connect(('127.0.0.1', "
        f"{port}))"
        "\nexcept OSError as exc: print('blocked:' + type(exc).__name__); "
        f"sys.exit({_CONNECTION_BLOCKED_EXIT})"
        "\nelse: print('reachable'); sys.exit(0)"
        "\nfinally: sock.close()"
    )


def _udp_route_source() -> str:
    return (
        "import socket,sys; "
        "sock=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sock.settimeout(2); "
        "\ntry: sock.connect(('198.51.100.1', 9))"
        "\nexcept OSError as exc: print('route-blocked:' + type(exc).__name__); "
        f"sys.exit({_CONNECTION_BLOCKED_EXIT})"
        "\nelse: print('route-present'); sys.exit(0)"
        "\nfinally: sock.close()"
    )


def _native_mutation_source(path: Path) -> str:
    return (
        "import errno,sys; "
        f"path={str(path)!r}; "
        "\ntry:"
        "\n with open(path, 'w', encoding='utf-8') as handle: handle.write('tampered\\n')"
        "\nexcept OSError as exc:"
        "\n if exc.errno in (errno.EPERM, errno.EACCES, errno.EROFS): "
        "print('mutation-blocked:' + type(exc).__name__); "
        f"sys.exit({_BOUNDARY_EVIDENCE_EXIT})"
        "\n print('mutation-error:' + type(exc).__name__); "
        f"sys.exit({_UNEXPECTED_OSERROR_EXIT})"
        "\nelse: print('mutation-succeeded'); sys.exit(0)"
    )


def _docker_root_readonly_source() -> str:
    return (
        "import os,sys; flags=os.statvfs('/').f_flag; "
        "\nif flags & os.ST_RDONLY: print('root-read-only'); "
        f"sys.exit({_BOUNDARY_EVIDENCE_EXIT})"
        "\nprint('root-not-read-only'); sys.exit(0)"
    )


def run_sandbox_probe(
    *,
    sandbox_mode: str = "auto",
    docker_image: str = "python:3.11-slim",
    timeout_seconds: int = 10,
) -> SandboxProbeReport:
    if sandbox_mode == "none":
        raise ValueError("sandbox_mode='none' cannot verify isolation")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")

    temp_base = _verified_host_temp_base()
    if temp_base is None:
        return SandboxProbeReport(
            backend=sandbox_mode,
            checks=(
                SandboxCheck(
                    "outside_write",
                    False,
                    "host temp base /var/tmp is unavailable or not writable",
                ),
            ),
        )

    with tempfile.TemporaryDirectory(
        prefix="mca-sandbox-probe-", dir=str(temp_base)
    ) as raw_root:
        root = Path(raw_root).resolve()
        workspace = root / "workspace"
        protected = root / "protected.txt"
        workspace.mkdir(mode=0o700)
        protected.write_text(_PROTECTED_CONTENT, encoding="utf-8")
        executor = BashExecutor(
            workspace,
            approval_mode="yolo",
            allow_shell=True,
            sandbox_mode=sandbox_mode,
            docker_image=docker_image,
            timeout_seconds=timeout_seconds,
        )

        backend_ok, backend_detail = executor.sandbox_probe()
        backend = str(backend_detail)
        sandbox_status = getattr(executor, "sandbox_status", None)
        if callable(sandbox_status):
            backend = str(sandbox_status())
        if not backend_ok:
            return SandboxProbeReport(
                backend=backend,
                checks=(
                    SandboxCheck(
                        "backend",
                        False,
                        _redact(executor, f"backend probe failed: {backend_detail}"),
                    ),
                ),
            )

        checks: list[SandboxCheck] = []

        workspace_result = executor.execute_bash(
            "printf 'workspace-write\\n' > .mca-sandbox-probe-write"
        )
        checks.append(
            SandboxCheck(
                "workspace_write",
                _returned(workspace_result, 0),
                _result_detail(executor, workspace_result, "workspace write"),
            )
        )

        protected_command_path = shlex.quote(str(protected))
        outside_results: list[tuple[Any, str]] = []
        if backend == "docker":
            visibility_result = executor.execute_bash(
                f"test ! -e {protected_command_path}"
            )
            outside_results.append((visibility_result, "host sentinel invisibility"))
            outside_result = executor.execute_bash(
                _python_command(_docker_root_readonly_source())
            )
            outside_results.append((outside_result, "read-only root mount flag"))
            outside_precondition = _returned(visibility_result, 0)
            outside_denied = _returned(
                outside_result, _BOUNDARY_EVIDENCE_EXIT
            )
        else:
            visibility_result = executor.execute_bash(
                f"cat -- {protected_command_path}"
            )
            outside_results.append((visibility_result, "host sentinel read"))
            outside_precondition = _returned(
                visibility_result, 0
            ) and visibility_result.output == _PROTECTED_CONTENT
            if outside_precondition:
                outside_result = executor.execute_bash(
                    _python_command(_native_mutation_source(protected))
                )
                outside_results.append((outside_result, "host sentinel write"))
                outside_denied = _returned(
                    outside_result, _BOUNDARY_EVIDENCE_EXIT
                )
            else:
                outside_denied = False
        try:
            protected_unchanged = (
                protected.read_text(encoding="utf-8") == _PROTECTED_CONTENT
            )
        except (FileNotFoundError, OSError):
            protected_unchanged = False
        outside_passed = (
            outside_precondition and outside_denied and protected_unchanged
        )
        if not outside_passed:
            outside_summary = "outside boundary was not safely verified"
        elif backend == "docker":
            outside_summary = (
                "host sentinel invisible, read-only root mount verified, "
                "sentinel unchanged"
            )
        else:
            outside_summary = (
                "outside boundary precondition passed, write denied, "
                "sentinel unchanged"
            )
        outside_detail = "; ".join(
            _result_detail(executor, result, summary)
            for result, summary in outside_results
        )
        checks.append(
            SandboxCheck(
                "outside_write",
                outside_passed,
                _redact(executor, f"{outside_summary}: {outside_detail}"),
            )
        )

        try:
            with tempfile.TemporaryDirectory(
                prefix="mca-sandbox-probe-socket-", dir="/tmp"
            ) as raw_socket_root:
                unix_path = Path(raw_socket_root) / "host.sock"
                unix_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    unix_listener.bind(str(unix_path))
                    unix_listener.listen(1)
                    unix_paths = [unix_path, *_known_unix_sockets()]
                    unix_result = executor.execute_bash(
                        _python_command(_unix_socket_source(unix_paths))
                    )
                finally:
                    unix_listener.close()
            unix_passed = _returned(
                unix_result, _CONNECTION_BLOCKED_EXIT
            ) or (
                backend == "sandbox-exec"
                and _returned(unix_result, _SOCKET_VISIBLE_BLOCKED_EXIT)
            )
            unix_detail = _result_detail(
                executor, unix_result, "Unix socket visibility and connection"
            )
        except OSError as exc:
            unix_passed = False
            unix_detail = _redact(
                executor,
                f"controlled Unix socket setup failed: {type(exc).__name__}: {exc}",
            )
        checks.append(
            SandboxCheck(
                "unix_socket",
                unix_passed,
                unix_detail,
            )
        )

        route_result = executor.execute_bash(_python_command(_udp_route_source()))
        if _returned(route_result, _CONNECTION_BLOCKED_EXIT):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp_listener:
                tcp_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                tcp_listener.bind(("127.0.0.1", 0))
                tcp_listener.listen(1)
                port = int(tcp_listener.getsockname()[1])
                network_result = executor.execute_bash(
                    _python_command(_tcp_source(port))
                )
            network_passed = _returned(
                network_result, _CONNECTION_BLOCKED_EXIT
            )
            network_detail = _redact(
                executor,
                "; ".join(
                    (
                        _result_detail(executor, route_result, "UDP route"),
                        _result_detail(executor, network_result, "controlled TCP"),
                    )
                ),
            )
        else:
            network_passed = False
            network_detail = _result_detail(executor, route_result, "UDP route")
        checks.append(
            SandboxCheck(
                "network",
                network_passed,
                network_detail,
            )
        )

        return SandboxProbeReport(backend=backend, checks=tuple(checks))
