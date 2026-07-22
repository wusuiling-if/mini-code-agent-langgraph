from __future__ import annotations

from dataclasses import dataclass

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
