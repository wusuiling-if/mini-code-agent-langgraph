from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

from langchain_core.messages import AIMessage

from mini_code_agent.agent import MiniCodeAgent
from mini_code_agent.chat import ConversationalCodeAgent
from mini_code_agent.checks import VerificationCheck
from mini_code_agent.executor import BashExecutor
from mini_code_agent.model import create_model
from mini_code_agent.trajectory import load_trajectory, summarize_trajectory, undo_trajectory


def make_calculator_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "calculator.py").write_text(
        "def add(a: int, b: int) -> int:\n"
        "    return a - b\n\n\n"
        "def multiply(a: int, b: int) -> int:\n"
        "    return a * b\n",
        encoding="utf-8",
    )
    (root / "test_calculator.py").write_text(
        "import unittest\n\n"
        "from calculator import add, multiply\n\n\n"
        "class CalculatorTest(unittest.TestCase):\n"
        "    def test_add_positive_numbers(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n\n"
        "    def test_add_negative_numbers(self):\n"
        "        self.assertEqual(add(-2, -3), -5)\n\n"
        "    def test_multiply(self):\n"
        "        self.assertEqual(multiply(4, 5), 20)\n\n\n"
        "if __name__ == \"__main__\":\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )


def test_mock_agent_fixes_bug_and_records_trajectory(tmp_path: Path):
    repo = tmp_path / "repo"
    make_calculator_repo(repo)
    trajectory_path = repo / "runs" / "run.traj.json"

    agent = MiniCodeAgent(
        create_model("mock"),
        BashExecutor(
            repo,
            approval_mode="yolo",
            sandbox_mode="none",
            default_test_command=(
                f"{shlex.quote(sys.executable)} -m unittest discover -v"
            ),
        ),
        trajectory_path=trajectory_path,
        quiet=True,
    )
    trajectory = agent.run("Fix failing tests")

    assert trajectory["exit_status"] == "Submitted"
    assert trajectory["workspace_changes"] == {"created": [], "deleted": [], "modified": ["calculator.py"]}
    assert "return a + b" in (repo / "calculator.py").read_text(encoding="utf-8")
    assert trajectory_path.exists()
    assert [event.get("tool") for event in trajectory["events"] if event["type"] == "tool"] == [
        "list_files",
        "run_tests",
        "read_file",
        "apply_patch",
        "run_tests",
        "git_diff",
        "submit",
    ]
    test_events = [
        event for event in trajectory["events"] if event.get("tool") == "run_tests"
    ]
    assert [event["tests_run"] for event in test_events] == [3, 3]
    assert all(
        "tests_run" not in event
        for event in trajectory["events"]
        if event.get("tool") != "run_tests"
    )


def test_trace_summary_and_undo_restore_original_file(tmp_path: Path):
    repo = tmp_path / "repo"
    make_calculator_repo(repo)
    trajectory_path = tmp_path / "run.traj.json"
    agent = MiniCodeAgent(
        create_model("mock"),
        BashExecutor(
            repo,
            approval_mode="yolo",
            sandbox_mode="none",
            default_test_command=(
                f"{shlex.quote(sys.executable)} -m unittest discover -v"
            ),
        ),
        trajectory_path=trajectory_path,
        quiet=True,
    )
    agent.run("Fix failing tests")

    data = load_trajectory(trajectory_path)
    summary = summarize_trajectory(data)
    assert "exit_status: Submitted" in summary
    assert "apply_patch" in summary

    actions = undo_trajectory(data)
    assert actions == ["restored calculator.py"]
    assert "return a - b" in (repo / "calculator.py").read_text(encoding="utf-8")


def test_cli_noninteractive_requires_yes(tmp_path: Path):
    repo = tmp_path / "repo"
    make_calculator_repo(repo)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mini_code_agent",
            "run",
            "Fix tests",
            "--cwd",
            str(repo),
            "--model",
            "deepseek",
            "--test-command",
            f"{shlex.quote(sys.executable)} -m unittest discover -v",
            "--quiet",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 1
    assert "Confirmation mode needs an interactive terminal" in result.stderr


def test_cli_trace_command(tmp_path: Path):
    repo = tmp_path / "repo"
    make_calculator_repo(repo)
    trajectory_path = tmp_path / "run.traj.json"
    agent = MiniCodeAgent(
        create_model("mock"),
        BashExecutor(
            repo,
            approval_mode="yolo",
            sandbox_mode="none",
            default_test_command=(
                f"{shlex.quote(sys.executable)} -m unittest discover -v"
            ),
        ),
        trajectory_path=trajectory_path,
        quiet=True,
    )
    agent.run("Fix failing tests")

    result = subprocess.run(
        [sys.executable, "-m", "mini_code_agent", "trace", str(trajectory_path), "--diff"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0
    assert "file_diff:" in result.stdout
    assert "return a + b" in result.stdout


def test_trajectory_keeps_reversible_source_only_in_private_journal(tmp_path: Path):
    repo = tmp_path / "repo"
    make_calculator_repo(repo)
    trajectory_path = tmp_path / "run.traj.json"
    agent = MiniCodeAgent(
        create_model("mock"),
        BashExecutor(
            repo,
            approval_mode="yolo",
            sandbox_mode="none",
            default_test_command=(
                f"{shlex.quote(sys.executable)} -m unittest discover -v"
            ),
        ),
        trajectory_path=trajectory_path,
        quiet=True,
    )
    agent.run("Fix failing tests")
    data = json.loads(trajectory_path.read_text(encoding="utf-8"))
    edit_events = [event for event in data["events"] if event.get("tool") == "apply_patch"]
    assert "before_content" not in edit_events[0]
    assert "after_content" not in edit_events[0]
    assert data["undo_journal"].startswith("state:")


class VerificationGateModel:
    def __init__(self):
        self.call = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.call += 1
        calls = {
            1: ("apply_patch", {"path": "value.py", "old": "VALUE = 1", "new": "VALUE = 2"}),
            2: ("submit", {"summary": "too early"}),
            3: ("run_tests", {}),
            4: ("submit", {"summary": "verified"}),
        }
        name, args = calls[self.call]
        return AIMessage(
            content=name,
            tool_calls=[{"name": name, "args": args, "id": f"gate-{self.call}", "type": "tool_call"}],
        )


class MatrixModel:
    def __init__(self):
        self.step = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.step += 1
        name, args = {
            1: ("run_tests", {}),
            2: ("submit", {"summary": "matrix verified"}),
        }[self.step]
        return AIMessage(
            content=name,
            tool_calls=[
                {
                    "name": name,
                    "args": args,
                    "id": f"matrix-{self.step}",
                    "type": "tool_call",
                }
            ],
        )


def test_agent_persists_redacted_matrix_evidence_and_submits(tmp_path: Path):
    trajectory_path = tmp_path / "run.json"
    tests_command = (
        f"{shlex.quote(sys.executable)} -c 'print(\"Ran 2 tests\")'"
    )
    lint_command = (
        f"{shlex.quote(sys.executable)} -c 'print(\"clean\")'"
    )
    agent = MiniCodeAgent(
        MatrixModel(),
        BashExecutor(
            tmp_path,
            approval_mode="yolo",
            sandbox_mode="none",
            verification_checks=(
                VerificationCheck("tests", tests_command),
                VerificationCheck("lint", lint_command),
            ),
        ),
        trajectory_path=trajectory_path,
        quiet=True,
    )

    trajectory = agent.run("verify")
    event = next(
        item for item in trajectory["events"] if item.get("tool") == "run_tests"
    )

    assert trajectory["exit_status"] == "Submitted"
    assert [item["name"] for item in event["verification_checks"]] == [
        "tests",
        "lint",
    ]
    rendered = trajectory_path.read_text(encoding="utf-8")
    assert tests_command not in rendered
    assert lint_command not in rendered
    assert "_verification_ignore_paths" not in rendered
    assert "verification_fingerprint" not in rendered


def test_chat_event_contains_only_redacted_matrix_evidence(tmp_path: Path):
    tests_command = (
        f"{shlex.quote(sys.executable)} -c 'print(\"Ran 2 tests\")'"
    )
    lint_command = (
        f"{shlex.quote(sys.executable)} -c 'print(\"clean\")'"
    )
    session = ConversationalCodeAgent(
        MatrixModel(),
        BashExecutor(
            tmp_path,
            approval_mode="yolo",
            sandbox_mode="none",
            verification_checks=(
                VerificationCheck("tests", tests_command),
                VerificationCheck("lint", lint_command),
            ),
        ),
        quiet=True,
    )

    result = session.respond_turn("verify", coding_mode=True)
    event = next(
        item for item in session.events if item.get("tool") == "run_tests"
    )

    assert result.status == "submitted"
    assert [item["name"] for item in event["verification_checks"]] == [
        "tests",
        "lint",
    ]
    assert tests_command not in str(event)
    assert lint_command not in str(event)


def test_agent_blocks_submit_until_latest_edit_is_verified(tmp_path: Path):
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    agent = MiniCodeAgent(
        VerificationGateModel(),
        BashExecutor(
            tmp_path,
            approval_mode="yolo",
            sandbox_mode="none",
            default_test_command="python3 -c 'print(\"ok\")'",
        ),
        quiet=True,
    )
    trajectory = agent.run("change value")
    submit_events = [event for event in trajectory["events"] if event.get("tool") == "submit"]
    assert submit_events[0]["blocked"] is True
    assert submit_events[0]["exception_info"] == "VerificationRequired"
    assert submit_events[1]["submitted"] is True
    assert trajectory["verification_status"] == "passed"


def test_private_undo_journal_restores_secret_and_preserves_existing_empty_file(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    secret = "sk-testsecret123456"
    (repo / "secret.txt").write_text(secret + "\n", encoding="utf-8")
    (repo / "empty.txt").write_text("", encoding="utf-8")
    trajectory_path = tmp_path / "secret.traj.json"
    executor = BashExecutor(repo, approval_mode="yolo", sandbox_mode="none")
    secret_result = executor.apply_patch("secret.txt", secret, "replaced")
    empty_result = executor.write_file("empty.txt", "now populated\n")

    from mini_code_agent.trajectory import write_undo_journal

    records = [
        {
            "path": result.file_path,
            "existed_before": result.file_existed_before,
            "before_content": result.before_content,
            "before_hash": result.before_hash,
            "after_hash": result.after_hash,
        }
        for result in [secret_result, empty_result]
    ]
    journal = write_undo_journal(trajectory_path, repo, records)
    trajectory_path.write_text(
        json.dumps({"cwd": str(repo), "undo_journal": journal, "events": []}), encoding="utf-8"
    )
    data = load_trajectory(trajectory_path)
    undo_trajectory(data)
    assert (repo / "secret.txt").read_text(encoding="utf-8") == secret + "\n"
    assert (repo / "empty.txt").exists()
    assert (repo / "empty.txt").read_text(encoding="utf-8") == ""


class ChatAndCodeModel:
    def __init__(self):
        self.code_step = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        last_human = next(message for message in reversed(messages) if message.type == "human")
        if last_human.content == "hello":
            return AIMessage(content="hello back")
        self.code_step += 1
        calls = {
            1: ("apply_patch", {"path": "note.txt", "old": "old", "new": "new"}),
            2: ("run_tests", {}),
            3: ("submit", {"summary": "updated note"}),
        }
        name, args = calls[self.code_step]
        return AIMessage(
            content=name,
            tool_calls=[{"name": name, "args": args, "id": f"chat-{self.code_step}", "type": "tool_call"}],
        )


def test_chat_session_can_answer_normally_and_complete_a_coding_turn(tmp_path: Path):
    (tmp_path / "note.txt").write_text("old\n", encoding="utf-8")
    session = ConversationalCodeAgent(
        ChatAndCodeModel(),
        BashExecutor(
            tmp_path,
            approval_mode="yolo",
            sandbox_mode="none",
            default_test_command="python3 -c 'print(\"Ran 2 tests in 0.01s\")'",
        ),
        quiet=True,
    )
    assert session.respond("hello") == "hello back"
    assert session.respond("update the note") == "updated note"
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "new\n"
    test_events = [event for event in session.events if event.get("tool") == "run_tests"]
    assert [event["tests_run"] for event in test_events] == [2]
    assert all(
        "tests_run" not in event
        for event in session.events
        if event.get("tool") != "run_tests"
    )


class MatrixMutationRecoveryModel:
    def __init__(self):
        self.step = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.step += 1
        name, args = {
            1: ("run_tests", {}),
            2: (
                "apply_patch",
                {"path": "value.py", "old": "VALUE = 2", "new": "VALUE = 1"},
            ),
            3: ("run_tests", {}),
            4: ("submit", {"summary": "recovered and verified"}),
        }[self.step]
        return AIMessage(
            content=name,
            tool_calls=[
                {
                    "name": name,
                    "args": args,
                    "id": f"recover-{self.step}",
                    "type": "tool_call",
                }
            ],
        )


def test_matrix_mutation_is_refused_then_repaired_and_rerun(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    marker = tmp_path / "mutated-once"
    command = (
        f"{shlex.quote(sys.executable)} -c "
        + shlex.quote(
            "from pathlib import Path; "
            f"marker=Path({str(marker)!r}); "
            "value=Path('value.py'); "
            "first=not marker.exists(); "
            "value.write_text('VALUE = 2\\n') if first else None; "
            "marker.write_text('done') if first else None; "
            "print('Ran 1 test')"
        )
    )
    trajectory = MiniCodeAgent(
        MatrixMutationRecoveryModel(),
        BashExecutor(
            repo,
            approval_mode="yolo",
            sandbox_mode="none",
            verification_checks=(VerificationCheck("tests", command),),
        ),
        quiet=True,
    ).run("verify without accepting check mutations")

    test_events = [
        event
        for event in trajectory["events"]
        if event.get("tool") == "run_tests"
    ]
    assert test_events[0]["exception_info"] == (
        "WorkspaceChangedDuringVerification"
    )
    assert test_events[1]["returncode"] == 0
    assert trajectory["exit_status"] == "Submitted"
    assert (repo / "value.py").read_text(encoding="utf-8") == "VALUE = 1\n"
