from __future__ import annotations

import shlex
import sys
from pathlib import Path

from mini_code_agent.executor import BashExecutor


def test_structured_tools_are_confined_and_redact_secrets(tmp_path: Path):
    (tmp_path / "a.py").write_text('SECRET = "sk-testsecret123456"\nneedle = 1\n', encoding="utf-8")
    executor = BashExecutor(tmp_path, approval_mode="yolo")

    assert executor.execute_tool("list_files", {}).output == "a.py"
    assert "[REDACTED_SECRET]" in executor.execute_tool("read_file", {"path": "a.py"}).output
    assert "[REDACTED_SECRET]" in executor.execute_tool("search_files", {"pattern": "SECRET"}).output

    escaped = executor.execute_tool("read_file", {"path": "../outside.txt"})
    assert escaped.returncode == -1
    assert "Path escapes workspace" in escaped.exception_info


def test_bash_disabled_by_default_and_dangerous_shell_blocked_when_enabled(tmp_path: Path):
    safe_executor = BashExecutor(tmp_path, approval_mode="yolo")
    blocked = safe_executor.execute_tool("bash", {"command": "pwd"})
    assert blocked.blocked
    assert blocked.exception_info == "ShellDisabled"

    shell_executor = BashExecutor(tmp_path, approval_mode="yolo", allow_shell=True, sandbox_mode="none")
    dangerous = shell_executor.execute_tool("bash", {"command": "rm -rf /"})
    assert dangerous.blocked


def test_run_tests_uses_configured_command_only_without_shell_permission(tmp_path: Path):
    executor = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        default_test_command="python3 -c 'print(123)'",
        sandbox_mode="none",
    )

    custom = executor.execute_tool("run_tests", {"command": "echo custom"})
    assert custom.blocked
    assert custom.exception_info == "CustomTestCommandDisabled"

    default = executor.execute_tool("run_tests", {})
    assert default.returncode == 0
    assert "123" in default.output


def test_run_tests_rejects_recognized_zero_test_success(tmp_path: Path):
    result = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        default_test_command=f"{shlex.quote(sys.executable)} -m unittest discover -v",
    ).execute_tool("run_tests", {})

    assert result.returncode != 0
    assert result.exception_info == "NoTestsCollected"
    assert result.tests_run == 0
    assert result.to_observation()["tests_run"] == 0


def test_run_tests_requires_an_authoritative_command(tmp_path: Path):
    result = BashExecutor(
        tmp_path, approval_mode="yolo", sandbox_mode="none"
    ).execute_tool("run_tests", {})

    assert result.blocked
    assert result.exception_info == "TestCommandRequired"


def test_run_tests_can_explicitly_allow_recognized_zero_test_success(tmp_path: Path):
    result = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        default_test_command=f"{shlex.quote(sys.executable)} -m unittest discover -v",
        allow_zero_tests=True,
    ).execute_tool("run_tests", {})

    assert result.returncode == 0
    assert result.exception_info == ""
    assert result.tests_run == 0


def test_apply_patch_replace_lines_and_write_file_return_diffs(tmp_path: Path):
    (tmp_path / "sample.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    executor = BashExecutor(tmp_path, approval_mode="yolo")

    patch = executor.execute_tool("apply_patch", {"path": "sample.txt", "old": "two", "new": "TWO"})
    assert patch.returncode == 0
    assert "-two" in patch.output
    assert "+TWO" in patch.output
    assert patch.before_content == "one\ntwo\nthree\n"

    replace = executor.execute_tool(
        "replace_lines",
        {"path": "sample.txt", "start_line": 3, "end_line": 3, "new_text": "THREE"},
    )
    assert replace.returncode == 0
    assert "+THREE" in replace.output

    write = executor.execute_tool("write_file", {"path": "new.txt", "content": "hello\n"})
    assert write.returncode == 0
    assert "+hello" in write.output


def test_sandbox_probe_reports_explicitly_disabled_mode_as_usable(tmp_path: Path):
    executor = BashExecutor(tmp_path, approval_mode="yolo", sandbox_mode="none")
    assert executor.sandbox_probe() == (True, "disabled")
