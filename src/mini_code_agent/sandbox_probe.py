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
_DETAIL_LIMIT = 500


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
    return executor.redactor.redact_text(truncate_text(detail, _DETAIL_LIMIT))


def _result_detail(executor: Any, result: Any, summary: str) -> str:
    parts = [summary, f"exit={result.returncode}"]
    output = str(result.output or "").strip()
    if output:
        parts.append(output)
    exception_info = str(result.exception_info or "").strip()
    if exception_info:
        parts.append(exception_info)
    return _redact(executor, ": ".join(parts))


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
        "errors=[]; "
        "\nfor path in paths:"
        "\n sock=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); sock.settimeout(2)"
        "\n try: sock.connect(path)"
        "\n except OSError as exc: errors.append(type(exc).__name__)"
        "\n else: print('reachable:' + path); sys.exit(0)"
        "\n finally: sock.close()"
        "\nprint('blocked:' + ','.join(errors)); "
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

    with tempfile.TemporaryDirectory(prefix="mca-sandbox-probe-") as raw_root:
        root = Path(raw_root).resolve()
        workspace = root / "workspace"
        protected = root / "protected.txt"
        workspace.mkdir(mode=0o700)
        protected.write_text("protected\n", encoding="utf-8")
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
                workspace_result.returncode == 0,
                _result_detail(executor, workspace_result, "workspace write"),
            )
        )

        outside_result = executor.execute_bash(
            "printf 'tampered\\n' > ../protected.txt"
        )
        try:
            protected_unchanged = (
                protected.read_text(encoding="utf-8") == "protected\n"
            )
        except (FileNotFoundError, OSError):
            protected_unchanged = False
        outside_passed = outside_result.returncode != 0 and protected_unchanged
        outside_summary = (
            "outside write denied and protected file unchanged"
            if outside_passed
            else "outside write was not safely denied"
        )
        checks.append(
            SandboxCheck(
                "outside_write",
                outside_passed,
                _result_detail(executor, outside_result, outside_summary),
            )
        )

        unix_listener: socket.socket | None = None
        unix_paths = _known_unix_sockets()
        try:
            if not unix_paths:
                unix_path = root / "host.sock"
                unix_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                unix_listener.bind(str(unix_path))
                unix_listener.listen(1)
                unix_paths = [unix_path]
            unix_result = executor.execute_bash(
                _python_command(_unix_socket_source(unix_paths))
            )
        finally:
            if unix_listener is not None:
                unix_listener.close()
        unix_passed = unix_result.returncode == _CONNECTION_BLOCKED_EXIT
        checks.append(
            SandboxCheck(
                "unix_socket",
                unix_passed,
                _result_detail(executor, unix_result, "Unix socket connection"),
            )
        )

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp_listener:
            tcp_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            tcp_listener.bind(("127.0.0.1", 0))
            tcp_listener.listen(1)
            port = int(tcp_listener.getsockname()[1])
            network_result = executor.execute_bash(
                _python_command(_tcp_source(port))
            )
        network_passed = network_result.returncode == _CONNECTION_BLOCKED_EXIT
        checks.append(
            SandboxCheck(
                "network",
                network_passed,
                _result_detail(executor, network_result, "TCP connection"),
            )
        )

        return SandboxProbeReport(backend=backend, checks=tuple(checks))
