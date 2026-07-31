from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from mini_code_agent.checks import VerificationCheck
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


@pytest.mark.parametrize(
    "output",
    [
        "Ran 0 tests in 0.001s\n\nOK\n",
        "collected 0 items\n",
    ],
    ids=["unittest", "pytest"],
)
def test_run_tests_rejects_recognized_zero_test_exit_five(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str
):
    executor = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        default_test_command="test command",
    )
    monkeypatch.setattr(
        executor,
        "_run",
        lambda command: subprocess.CompletedProcess(command, 5, output),
    )

    result = executor.execute_tool("run_tests", {})

    assert result.returncode != 0
    assert result.exception_info == "NoTestsCollected"
    assert result.tests_run == 0


@pytest.mark.parametrize(
    "output",
    [
        "Ran 0 tests in 0.001s\n\nOK\n",
        "no tests ran in 0.01s\n",
    ],
    ids=["unittest", "pytest"],
)
def test_run_tests_allows_recognized_zero_test_exit_five_when_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str
):
    executor = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        default_test_command="test command",
        allow_zero_tests=True,
    )
    monkeypatch.setattr(
        executor,
        "_run",
        lambda command: subprocess.CompletedProcess(command, 5, output),
    )

    result = executor.execute_tool("run_tests", {})

    assert result.returncode == 0
    assert result.exception_info == ""
    assert result.tests_run == 0


def test_run_tests_does_not_promote_unrelated_failure_with_zero_test_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executor = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        default_test_command="test command",
        allow_zero_tests=True,
    )
    monkeypatch.setattr(
        executor,
        "_run",
        lambda command: subprocess.CompletedProcess(
            command, 2, "Ran 0 tests in 0.001s\nconfiguration failed\n"
        ),
    )

    result = executor.execute_tool("run_tests", {})

    assert result.returncode == 2
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


def test_docker_argv_contains_exact_network_none_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executor = BashExecutor(tmp_path, sandbox_mode="docker")
    monkeypatch.setattr(
        executor,
        "_trusted_executable",
        lambda name: "/usr/bin/docker" if name == "docker" else "",
    )

    argv = executor._sandboxed_argv(["/bin/sh", "-c", ":"])

    network_index = argv.index("--network")
    assert argv[network_index : network_index + 2] == ["--network", "none"]


def test_docker_argv_contains_read_only_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executor = BashExecutor(tmp_path, sandbox_mode="docker")
    monkeypatch.setattr(
        executor,
        "_trusted_executable",
        lambda name: "/usr/bin/docker" if name == "docker" else "",
    )

    argv = executor._sandboxed_argv(["/bin/sh", "-c", ":"])

    assert argv.count("--read-only") == 1


def test_matrix_runs_in_order_and_collects_ordinary_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executor = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        verification_checks=(
            VerificationCheck("tests", "tests command"),
            VerificationCheck("lint", "lint command"),
            VerificationCheck("types", "types command"),
        ),
    )
    calls: list[str] = []

    def fake_run(command: str) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 1 if command == "lint command" else 0, command
        )

    monkeypatch.setattr(executor, "_run", fake_run)

    result = executor.execute_tool("run_tests", {})

    assert calls == ["tests command", "lint command", "types command"]
    assert result.returncode == 1
    assert result.exception_info == "VerificationCheckFailed"
    assert result.command == "<verification matrix>"
    assert [item.name for item in result.verification_checks] == [
        "tests",
        "lint",
        "types",
    ]


def test_matrix_preflights_every_command_before_running_any(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executor = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        verification_checks=(
            VerificationCheck("tests", "safe command"),
            VerificationCheck("lint", "blocked command"),
        ),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        executor,
        "_blocked_command_reason",
        lambda command: "BlockedForTest" if command == "blocked command" else "",
    )
    monkeypatch.setattr(
        executor,
        "_run",
        lambda command: calls.append(command)
        or subprocess.CompletedProcess(command, 0, ""),
    )

    result = executor.execute_tool("run_tests", {})

    assert calls == []
    assert result.blocked is True
    assert result.exception_info == "BlockedForTest"


def test_matrix_uses_one_approval_for_all_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executor = BashExecutor(
        tmp_path,
        approval_mode="confirm",
        sandbox_mode="none",
        verification_checks=(
            VerificationCheck("tests", "tests command"),
            VerificationCheck("lint", "lint command"),
        ),
    )
    approvals: list[tuple[str, str]] = []
    monkeypatch.setattr(
        executor,
        "_confirm",
        lambda tool, detail: approvals.append((tool, detail)) or False,
    )

    result = executor.execute_tool("run_tests", {})

    assert len(approvals) == 1
    assert "tests:" in approvals[0][1]
    assert "lint:" in approvals[0][1]
    assert result.approved is False


def test_matrix_rejects_a_check_that_mutates_fingerprinted_workspace(
    tmp_path: Path,
):
    result = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        verification_checks=(
            VerificationCheck(
                "tests",
                (
                    f"{shlex.quote(sys.executable)} -c "
                    "\"from pathlib import Path; Path('generated.txt').write_text('x')\""
                ),
            ),
        ),
    ).execute_tool("run_tests", {})

    assert result.returncode == -1
    assert result.exception_info == "WorkspaceChangedDuringVerification"
    assert (tmp_path / "generated.txt").read_text(encoding="utf-8") == "x"


def test_legacy_test_command_rejects_a_persisted_mutation(tmp_path: Path):
    command = (
        f"{shlex.quote(sys.executable)} -c "
        + shlex.quote(
            "from pathlib import Path; "
            "Path('generated.txt').write_text('x'); "
            "print('legacy passed')"
        )
    )
    result = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        default_test_command=command,
    ).execute_tool("run_tests", {})

    assert result.returncode == -1
    assert result.exception_info == "WorkspaceChangedDuringVerification"
    assert result.verification_boundary_checked is True
    assert result.verification_checks == ()
    assert (tmp_path / "generated.txt").read_text(encoding="utf-8") == "x"


def test_matrix_observation_omits_commands_and_redacts_recognized_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    secret = "sk-testsecret123456"
    executor = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        verification_checks=(VerificationCheck("lint", f"echo {secret}"),),
    )
    monkeypatch.setattr(
        executor,
        "_run",
        lambda command: subprocess.CompletedProcess(command, 1, secret),
    )

    result = executor.execute_tool("run_tests", {})
    rendered = str(result.to_observation())

    assert secret not in rendered
    assert "echo" not in rendered
    assert "[REDACTED_SECRET]" in rendered
