from __future__ import annotations

import ast
import shlex
import socket
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
                ToolResult("bash", "sensitive write denied", 1, 0),
                ToolResult("bash", "sensitive socket denied", 73, 0),
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
                ToolResult("bash", "unexpectedly allowed", 0, 0),
                ToolResult("bash", "socket denied", 73, 0),
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
        if self._command_count == 3:
            source = shlex.split(command)[2]
            paths_source = source.split("paths=", 1)[1].split("; errors=", 1)[0]
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


def test_detail_is_fully_redacted_before_final_length_limit():
    secret = "repeated-sensitive-token"
    raw_detail = (f"prefix:{secret}:suffix\n" * 100).strip()
    redactor = _ExpandingRedactor(secret)

    detail = probe_module._redact(_ExecutorWithRedactor(redactor), raw_detail)

    assert redactor.seen == [raw_detail]
    assert secret not in detail
    assert len(detail) <= 500
