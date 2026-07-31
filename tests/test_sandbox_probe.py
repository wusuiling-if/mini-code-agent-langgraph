from __future__ import annotations

import ast
import shlex
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

import mini_code_agent.sandbox_probe as probe_module
from mini_code_agent.contracts import ToolResult
from mini_code_agent.sandbox_probe import (
    SandboxCheck,
    SandboxProbeReport,
    run_sandbox_probe,
)


class _Redactor:
    def redact_text(self, text: str) -> str:
        return text.replace("sensitive", "[REDACTED]")


class _ExpandingRedactor:
    def __init__(self, secret: str):
        self.secret = secret
        self.seen: list[str] = []

    def redact_text(self, text: str) -> str:
        self.seen.append(text)
        return text.replace(self.secret, "[REDACTED-LONG-VALUE]")


class _ExecutorWithRedactor:
    def __init__(self, redactor: object):
        self.redactor = redactor


@dataclass
class _FakeExecutor:
    workspace: object
    approval_mode: str
    allow_shell: bool
    sandbox_mode: str
    docker_image: str
    timeout_seconds: int

    def __post_init__(self) -> None:
        self.redactor = _Redactor()
        self._results = iter(
            (
                ToolResult("bash", "sensitive write allowed", 0, 0),
                ToolResult("bash", "protected\n", 0, 0),
                ToolResult("bash", "sensitive write denied", 75, 0),
                ToolResult("bash", "sensitive socket denied", 73, 0),
                ToolResult("bash", "sensitive route denied", 73, 0),
                ToolResult("bash", "sensitive network denied", 73, 0),
            )
        )

    def sandbox_probe(self) -> tuple[bool, str]:
        return True, "fake"

    def execute_bash(self, command: str) -> ToolResult:
        return next(self._results)


class _FakeOutsideWriteSucceeds(_FakeExecutor):
    def __post_init__(self) -> None:
        self.redactor = _Redactor()
        self._results = iter(
            (
                ToolResult("bash", "write allowed", 0, 0),
                ToolResult("bash", "protected\n", 0, 0),
                ToolResult("bash", "unexpectedly allowed", 0, 0),
                ToolResult("bash", "socket denied", 73, 0),
                ToolResult("bash", "route denied", 73, 0),
                ToolResult("bash", "network denied", 73, 0),
            )
        )


class _FakeExecutorInspectingUnixListener(_FakeExecutor):
    controlled_listener_was_live = False

    def __post_init__(self) -> None:
        super().__post_init__()
        self._command_count = 0

    def execute_bash(self, command: str) -> ToolResult:
        self._command_count += 1
        if self._command_count == 4:
            source = shlex.split(command)[2]
            paths_source = source.split("paths=", 1)[1].split("; visible=", 1)[0]
            paths = [Path(path) for path in ast.literal_eval(paths_source)]
            controlled_paths = [
                path for path in paths if path.name == "host.sock"
            ]
            if controlled_paths:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(1)
                    client.connect(str(controlled_paths[0]))
                type(self).controlled_listener_was_live = True
        return super().execute_bash(command)


@dataclass
class _CommandRoutingExecutor:
    workspace: object
    approval_mode: str
    allow_shell: bool
    sandbox_mode: str
    docker_image: str
    timeout_seconds: int

    unix_returncode = 73
    udp_returncode = 73
    tcp_returncode = 73
    native_read_output = "protected\n"
    native_read_returncode = 0
    native_read_exception = ""
    docker_visibility_returncode = 0
    docker_visibility_exception = ""
    mutation_returncode = 75
    mutation_exception = ""

    def __post_init__(self) -> None:
        self.redactor = _Redactor()
        self.commands: list[str] = []

    def sandbox_probe(self) -> tuple[bool, str]:
        return True, self.sandbox_mode

    def sandbox_status(self) -> str:
        return self.sandbox_mode

    def execute_bash(self, command: str) -> ToolResult:
        self.commands.append(command)
        if ".mca-sandbox-probe-write" in command:
            return ToolResult("bash", "", 0, 0)
        if "AF_UNIX" in command:
            return ToolResult("bash", "socket result", self.unix_returncode, 0)
        if "SOCK_DGRAM" in command:
            return ToolResult("bash", "route result", self.udp_returncode, 0)
        if "AF_INET" in command and "SOCK_STREAM" in command:
            return ToolResult("bash", "tcp result", self.tcp_returncode, 0)
        if "statvfs" in command:
            return ToolResult(
                "bash",
                "root mount result",
                self.mutation_returncode,
                0,
                exception_info=self.mutation_exception,
            )
        if command.startswith("cat "):
            return ToolResult(
                "bash",
                self.native_read_output,
                self.native_read_returncode,
                0,
                exception_info=self.native_read_exception,
            )
        if command.startswith("test ! -e "):
            return ToolResult(
                "bash",
                "",
                self.docker_visibility_returncode,
                0,
                exception_info=self.docker_visibility_exception,
            )
        if "protected.txt" in command or "/mca-sandbox-probe-outside" in command:
            return ToolResult(
                "bash",
                "write result",
                self.mutation_returncode,
                0,
                exception_info=self.mutation_exception,
            )
        raise AssertionError(f"unexpected probe command: {command}")


def _check(report: SandboxProbeReport, name: str) -> SandboxCheck:
    return next(check for check in report.checks if check.name == name)


def test_probe_rejects_none():
    with pytest.raises(ValueError, match="cannot verify isolation"):
        run_sandbox_probe(sandbox_mode="none")


def test_report_is_not_ok_when_any_check_fails():
    report = SandboxProbeReport(
        backend="fake",
        checks=(
            SandboxCheck("workspace_write", True, "allowed"),
            SandboxCheck("network", False, "reachable"),
        ),
    )

    assert report.ok is False


def test_probe_aggregates_redacted_results_from_executor(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(probe_module, "BashExecutor", _FakeExecutor)

    report = run_sandbox_probe(sandbox_mode="docker")

    assert report.backend == "fake"
    assert report.ok is True
    assert [check.name for check in report.checks] == [
        "workspace_write",
        "outside_write",
        "unix_socket",
        "network",
    ]
    assert all("sensitive" not in check.detail for check in report.checks)


def test_outside_write_requires_command_failure_even_when_file_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(probe_module, "BashExecutor", _FakeOutsideWriteSucceeds)

    report = run_sandbox_probe(sandbox_mode="docker")

    outside_write = next(
        check for check in report.checks if check.name == "outside_write"
    )
    assert outside_write.passed is False
    assert report.ok is False


def test_unix_check_uses_live_controlled_listener_when_known_socket_is_stale(
    monkeypatch: pytest.MonkeyPatch,
):
    with tempfile.TemporaryDirectory(dir="/tmp") as raw_root:
        stale_path = Path(raw_root) / "stale.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale_socket:
            stale_socket.bind(str(stale_path))
        _FakeExecutorInspectingUnixListener.controlled_listener_was_live = False
        monkeypatch.setattr(
            probe_module, "BashExecutor", _FakeExecutorInspectingUnixListener
        )
        monkeypatch.setattr(
            probe_module, "_known_unix_sockets", lambda: [stale_path]
        )

        report = run_sandbox_probe(sandbox_mode="docker")

        assert report.ok is True
        assert _FakeExecutorInspectingUnixListener.controlled_listener_was_live is True


def test_network_fails_when_udp_route_exists_even_if_controlled_tcp_is_denied(
    monkeypatch: pytest.MonkeyPatch,
):
    class RoutedNetworkExecutor(_CommandRoutingExecutor):
        udp_returncode = 0

    monkeypatch.setattr(probe_module, "BashExecutor", RoutedNetworkExecutor)

    report = run_sandbox_probe(sandbox_mode="docker")

    assert _check(report, "network").passed is False


def test_udp_source_reports_blocked_route_without_claiming_physical_absence():
    source = probe_module._udp_route_source()

    assert "route-blocked:" in source
    assert "no-route:" not in source


def test_unix_source_distinguishes_visible_but_blocked_from_invisible(
    tmp_path: Path,
):
    visible_non_socket = tmp_path / "visible"
    visible_non_socket.write_text("not a socket", encoding="utf-8")

    visible = subprocess.run(
        [sys.executable, "-c", probe_module._unix_socket_source([visible_non_socket])],
        text=True,
        capture_output=True,
        check=False,
    )
    invisible = subprocess.run(
        [sys.executable, "-c", probe_module._unix_socket_source([tmp_path / "missing"])],
        text=True,
        capture_output=True,
        check=False,
    )

    assert visible.returncode == 74
    assert "visible-but-blocked" in visible.stdout
    assert invisible.returncode == 73
    assert "invisible" in invisible.stdout


def test_sandbox_exec_allows_visible_but_blocked_unix_sockets(
    monkeypatch: pytest.MonkeyPatch,
):
    class VisibleSocketExecutor(_CommandRoutingExecutor):
        unix_returncode = 74

    monkeypatch.setattr(probe_module, "BashExecutor", VisibleSocketExecutor)

    report = run_sandbox_probe(sandbox_mode="sandbox-exec")

    assert _check(report, "unix_socket").passed is True


def test_docker_requires_unix_socket_invisibility(
    monkeypatch: pytest.MonkeyPatch,
):
    class VisibleSocketExecutor(_CommandRoutingExecutor):
        unix_returncode = 74

    monkeypatch.setattr(probe_module, "BashExecutor", VisibleSocketExecutor)

    report = run_sandbox_probe(sandbox_mode="docker")

    assert _check(report, "unix_socket").passed is False


def test_native_outside_write_first_reads_exact_absolute_sentinel(
    monkeypatch: pytest.MonkeyPatch,
):
    created: list[_CommandRoutingExecutor] = []

    class RecordingExecutor(_CommandRoutingExecutor):
        def __post_init__(self) -> None:
            super().__post_init__()
            created.append(self)

    monkeypatch.setattr(probe_module, "BashExecutor", RecordingExecutor)

    report = run_sandbox_probe(sandbox_mode="sandbox-exec")

    assert _check(report, "outside_write").passed is True
    read_commands = [command for command in created[0].commands if command.startswith("cat ")]
    assert len(read_commands) == 1
    assert read_commands[0].endswith("/protected.txt")
    assert not read_commands[0].endswith("../protected.txt")


def test_native_mutation_uses_reserved_errno_evidence(
    monkeypatch: pytest.MonkeyPatch,
):
    created: list[_CommandRoutingExecutor] = []

    class RecordingExecutor(_CommandRoutingExecutor):
        def __post_init__(self) -> None:
            super().__post_init__()
            created.append(self)

    monkeypatch.setattr(probe_module, "BashExecutor", RecordingExecutor)

    report = run_sandbox_probe(sandbox_mode="sandbox-exec")

    assert _check(report, "outside_write").passed is True
    mutation = next(
        command
        for command in created[0].commands
        if "protected.txt" in command and not command.startswith("cat ")
    )
    assert "python3 -c" in mutation
    assert "errno.EPERM" in mutation
    assert "errno.EACCES" in mutation
    assert "errno.EROFS" in mutation
    assert "sys.exit(75)" in mutation


def test_native_mutation_source_returns_zero_after_successful_write(tmp_path: Path):
    target = tmp_path / "protected.txt"
    target.write_text("protected\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-c", probe_module._native_mutation_source(target)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert target.read_text(encoding="utf-8") == "tampered\n"


def test_native_mutation_source_uses_distinct_exit_for_unrelated_oserror(
    tmp_path: Path,
):
    target = tmp_path / "missing" / "protected.txt"

    completed = subprocess.run(
        [sys.executable, "-c", probe_module._native_mutation_source(target)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 76
    assert "mutation-error:FileNotFoundError" in completed.stdout


@pytest.mark.parametrize(
    ("returncode", "exception_info"),
    [(-1, ""), (1, "ExecutorFailure"), (-1, "TimeoutExpired")],
    ids=["negative-return", "exception", "timeout"],
)
def test_native_outside_write_rejects_nonordinary_executor_results(
    monkeypatch: pytest.MonkeyPatch, returncode: int, exception_info: str
):
    class FailedMutationExecutor(_CommandRoutingExecutor):
        mutation_returncode = returncode
        mutation_exception = exception_info

    monkeypatch.setattr(probe_module, "BashExecutor", FailedMutationExecutor)

    report = run_sandbox_probe(sandbox_mode="sandbox-exec")

    assert _check(report, "outside_write").passed is False


@pytest.mark.parametrize("returncode", [125, 126, 127, 137])
def test_native_outside_write_rejects_unrelated_positive_exit_codes(
    monkeypatch: pytest.MonkeyPatch, returncode: int
):
    class UnrelatedFailureExecutor(_CommandRoutingExecutor):
        mutation_returncode = returncode

    monkeypatch.setattr(probe_module, "BashExecutor", UnrelatedFailureExecutor)

    report = run_sandbox_probe(sandbox_mode="sandbox-exec")

    assert _check(report, "outside_write").passed is False


def test_native_outside_write_fails_when_sentinel_content_is_not_exact(
    monkeypatch: pytest.MonkeyPatch,
):
    class WrongSentinelExecutor(_CommandRoutingExecutor):
        native_read_output = "wrong\n"

    monkeypatch.setattr(probe_module, "BashExecutor", WrongSentinelExecutor)

    report = run_sandbox_probe(sandbox_mode="bwrap")

    assert _check(report, "outside_write").passed is False


def test_docker_outside_write_requires_host_sentinel_invisibility(
    monkeypatch: pytest.MonkeyPatch,
):
    class FailedVisibilityExecutor(_CommandRoutingExecutor):
        docker_visibility_returncode = -1
        docker_visibility_exception = "DockerLaunchFailed"

    monkeypatch.setattr(probe_module, "BashExecutor", FailedVisibilityExecutor)

    report = run_sandbox_probe(sandbox_mode="docker")

    assert _check(report, "outside_write").passed is False


def test_docker_root_readonly_uses_statvfs_reserved_evidence(
    monkeypatch: pytest.MonkeyPatch,
):
    created: list[_CommandRoutingExecutor] = []

    class RecordingExecutor(_CommandRoutingExecutor):
        def __post_init__(self) -> None:
            super().__post_init__()
            created.append(self)

    monkeypatch.setattr(probe_module, "BashExecutor", RecordingExecutor)

    report = run_sandbox_probe(sandbox_mode="docker")

    assert _check(report, "outside_write").passed is True
    readonly_command = next(
        command for command in created[0].commands if "statvfs" in command
    )
    readonly_source = shlex.split(readonly_command)[2]
    assert "os.statvfs('/')" in readonly_source
    assert "os.ST_RDONLY" in readonly_source
    assert "sys.exit(75)" in readonly_source


def test_docker_outside_detail_reports_mount_flag_evidence(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(probe_module, "BashExecutor", _CommandRoutingExecutor)

    report = run_sandbox_probe(sandbox_mode="docker")

    detail = _check(report, "outside_write").detail
    assert "read-only root mount verified" in detail
    assert "write denied" not in detail


@pytest.mark.parametrize(
    ("returncode", "exception_info"),
    [(-1, ""), (1, "ExecutorFailure"), (-1, "TimeoutExpired")],
    ids=["negative-return", "exception", "timeout"],
)
def test_docker_outside_write_rejects_nonordinary_root_write_results(
    monkeypatch: pytest.MonkeyPatch, returncode: int, exception_info: str
):
    class FailedMutationExecutor(_CommandRoutingExecutor):
        mutation_returncode = returncode
        mutation_exception = exception_info

    monkeypatch.setattr(probe_module, "BashExecutor", FailedMutationExecutor)

    report = run_sandbox_probe(sandbox_mode="docker")

    assert _check(report, "outside_write").passed is False


@pytest.mark.parametrize("returncode", [125, 126, 127, 137])
def test_docker_root_readonly_rejects_unrelated_positive_exit_codes(
    monkeypatch: pytest.MonkeyPatch, returncode: int
):
    class UnrelatedFailureExecutor(_CommandRoutingExecutor):
        mutation_returncode = returncode

    monkeypatch.setattr(probe_module, "BashExecutor", UnrelatedFailureExecutor)

    report = run_sandbox_probe(sandbox_mode="docker")

    assert _check(report, "outside_write").passed is False


def test_probe_uses_var_tmp_for_sentinel_and_tmp_for_controlled_socket(
    monkeypatch: pytest.MonkeyPatch,
):
    real_temporary_directory = tempfile.TemporaryDirectory
    selected_dirs: list[str | None] = []

    def recording_temporary_directory(*args, **kwargs):
        selected_dirs.append(kwargs.get("dir"))
        return real_temporary_directory(*args, **kwargs)

    monkeypatch.setattr(probe_module, "BashExecutor", _CommandRoutingExecutor)
    monkeypatch.setattr(
        probe_module.tempfile, "TemporaryDirectory", recording_temporary_directory
    )

    run_sandbox_probe(sandbox_mode="docker")

    assert selected_dirs == [str(Path("/var/tmp").resolve()), "/tmp"]


def test_verified_host_temp_base_fails_closed_when_candidate_is_unavailable(
    tmp_path: Path,
):
    missing = tmp_path / "missing"

    assert probe_module._verified_host_temp_base(missing) is None


def test_verified_host_temp_base_rejects_symlink_into_masked_tmp(tmp_path: Path):
    with tempfile.TemporaryDirectory(dir="/tmp") as raw_masked:
        alias = tmp_path / "masked-alias"
        alias.symlink_to(raw_masked, target_is_directory=True)

        assert probe_module._verified_host_temp_base(alias) is None


def test_verified_host_temp_base_returns_canonical_candidate(tmp_path: Path):
    alias = tmp_path / "var-tmp-alias"
    alias.symlink_to("/var/tmp", target_is_directory=True)

    assert probe_module._verified_host_temp_base(alias) == Path("/var/tmp").resolve()


def test_probe_reports_clear_failure_when_verified_host_temp_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(probe_module, "_verified_host_temp_base", lambda: None)

    report = run_sandbox_probe(sandbox_mode="bwrap")

    assert report.ok is False
    assert [check.name for check in report.checks] == ["outside_write"]
    assert "/var/tmp" in report.checks[0].detail
    assert "unavailable or not writable" in report.checks[0].detail


def test_verified_host_temp_base_cleans_up_after_writability_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    def fail_write(_fd: int, _data: bytes) -> int:
        raise OSError("injected write failure")

    with tempfile.TemporaryDirectory(dir="/var/tmp") as raw_candidate:
        candidate = Path(raw_candidate)
        monkeypatch.setattr(probe_module.os, "write", fail_write)

        assert probe_module._verified_host_temp_base(candidate) is None
        assert list(candidate.iterdir()) == []


def test_combined_network_detail_respects_report_length_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    class VerboseNetworkExecutor(_CommandRoutingExecutor):
        def execute_bash(self, command: str) -> ToolResult:
            result = super().execute_bash(command)
            if "SOCK_DGRAM" in command or (
                "AF_INET" in command and "SOCK_STREAM" in command
            ):
                result.output = "network-detail-" * 100
            return result

    monkeypatch.setattr(probe_module, "BashExecutor", VerboseNetworkExecutor)

    report = run_sandbox_probe(sandbox_mode="docker")

    assert len(_check(report, "network").detail) <= 500


def test_detail_is_fully_redacted_before_final_length_limit():
    secret = "repeated-sensitive-token"
    raw_detail = (f"prefix:{secret}:suffix\n" * 100).strip()
    redactor = _ExpandingRedactor(secret)

    detail = probe_module._redact(_ExecutorWithRedactor(redactor), raw_detail)

    assert redactor.seen == [raw_detail]
    assert secret not in detail
    assert len(detail) <= 500
