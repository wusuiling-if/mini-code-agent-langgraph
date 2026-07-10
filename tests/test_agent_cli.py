from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from mini_code_agent.agent import MiniCodeAgent
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
    trajectory_path = tmp_path / "run.traj.json"

    agent = MiniCodeAgent(
        create_model("mock"),
        BashExecutor(repo, approval_mode="yolo", sandbox_mode="none"),
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


def test_trace_summary_and_undo_restore_original_file(tmp_path: Path):
    repo = tmp_path / "repo"
    make_calculator_repo(repo)
    trajectory_path = tmp_path / "run.traj.json"
    agent = MiniCodeAgent(
        create_model("mock"),
        BashExecutor(repo, approval_mode="yolo", sandbox_mode="none"),
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
            "mock",
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
        BashExecutor(repo, approval_mode="yolo", sandbox_mode="none"),
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


def test_trajectory_json_has_reversible_content(tmp_path: Path):
    repo = tmp_path / "repo"
    make_calculator_repo(repo)
    trajectory_path = tmp_path / "run.traj.json"
    agent = MiniCodeAgent(
        create_model("mock"),
        BashExecutor(repo, approval_mode="yolo", sandbox_mode="none"),
        trajectory_path=trajectory_path,
        quiet=True,
    )
    agent.run("Fix failing tests")
    data = json.loads(trajectory_path.read_text(encoding="utf-8"))
    edit_events = [event for event in data["events"] if event.get("tool") == "apply_patch"]
    assert edit_events[0]["before_content"]
    assert edit_events[0]["after_content"]
