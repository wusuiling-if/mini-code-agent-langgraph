from __future__ import annotations

import json
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import mini_code_agent.cli as cli_module
import mini_code_agent.executor as executor_module
import mini_code_agent.trajectory as trajectory_module
import mini_code_agent.utils as utils_module
import mini_code_agent.verification as verification_module
from mini_code_agent.agent import (
    MiniCodeAgent,
    VerificationGate,
    _message_size,
    audit_tool_args,
    capture_workspace_fingerprint,
    compact_messages,
    execute_tool_batch,
)
from mini_code_agent.chat import MAX_PERSISTED_EVENTS, ConversationalCodeAgent, TurnResult
from mini_code_agent.checks import (
    VerificationCheck,
    VerificationCheckEvidence,
)
from mini_code_agent.cli import (
    ChatAccessController,
    _git_dirty,
    _load_runtime_env,
    build_parser,
)
from mini_code_agent.contracts import ToolResult
from mini_code_agent.executor import BashExecutor
from mini_code_agent.model import create_model
from mini_code_agent.trajectory import (
    load_trajectory,
    undo_trajectory,
    write_undo_journal,
)
from mini_code_agent.utils import write_json


def tool_call(name: str, call_id: str, args: dict | None = None) -> dict:
    return {
        "name": name,
        "args": args or {},
        "id": call_id,
        "type": "tool_call",
    }


def test_bwrap_uses_private_runtime_namespaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = BashExecutor(tmp_path, sandbox_mode="bwrap")
    monkeypatch.setattr(
        executor,
        "_trusted_executable",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else "",
    )

    argv = executor._sandboxed_argv(["/bin/sh", "-c", ":"])

    assert "--unshare-all" in argv
    assert argv[argv.index("--tmpfs", argv.index("--unshare-all")) + 1] == "/run"


def test_macos_profile_does_not_allow_shared_tmp_writes(tmp_path: Path) -> None:
    profile = BashExecutor(tmp_path, sandbox_mode="sandbox-exec")._sandbox_profile()

    assert '(allow file-write* (subpath "/tmp"))' not in profile
    assert '(allow file-write* (subpath "/private/tmp"))' not in profile


def test_macos_sandbox_allows_private_runtime_tmp_writes(tmp_path: Path) -> None:
    executor = BashExecutor(tmp_path, sandbox_mode="sandbox-exec")
    if not executor._trusted_executable("sandbox-exec"):
        pytest.skip("sandbox-exec is unavailable")

    result = executor._run_argv(
        ["/bin/sh", "-c", 'printf ok > "$TMPDIR/sandbox-write"']
    )

    assert result.returncode == 0, result.stdout
    assert (executor._runtime_root / "tmp" / "sandbox-write").read_text() == "ok"


def test_verification_fingerprint_covers_modes_symlinks_and_dependency_dirs(
    tmp_path: Path,
):
    script = tmp_path / "script.sh"
    script.write_text("echo ok\n", encoding="utf-8")
    executor = BashExecutor(tmp_path, approval_mode="yolo", sandbox_mode="none")
    baseline = capture_workspace_fingerprint(executor)

    script.chmod(0o755)
    assert capture_workspace_fingerprint(executor) != baseline
    script.chmod(0o644)

    try:
        (tmp_path / "outside-link").symlink_to("/etc/passwd")
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    with_link = capture_workspace_fingerprint(executor)
    assert with_link != baseline
    (tmp_path / "outside-link").unlink()

    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "agent.txt").write_text("changed", encoding="utf-8")
    assert capture_workspace_fingerprint(executor) != baseline


def test_run_mode_cannot_submit_without_authoritative_verification(tmp_path: Path):
    executor = BashExecutor(tmp_path, approval_mode="yolo", sandbox_mode="none")
    fingerprint = capture_workspace_fingerprint(executor)
    gate = VerificationGate.create(fingerprint, require_verification=True)

    outcome = execute_tool_batch(
        executor, [tool_call("submit", "submit-1")], gate
    )

    assert not outcome.submitted
    assert outcome.calls[0].result.exception_info == "VerificationRequired"


def test_matrix_pass_then_edit_in_one_batch_blocks_submit(tmp_path: Path):
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    executor = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        verification_checks=(
            VerificationCheck(
                "tests",
                f"{shlex.quote(sys.executable)} -c 'print(\"Ran 1 test\")'",
            ),
        ),
    )
    baseline = capture_workspace_fingerprint(executor)
    gate = VerificationGate.create(baseline, require_verification=True)

    outcome = execute_tool_batch(
        executor,
        [
            {"name": "run_tests", "args": {}, "id": "checks"},
            {
                "name": "apply_patch",
                "args": {
                    "path": "value.py",
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                },
                "id": "edit",
            },
            {
                "name": "submit",
                "args": {"summary": "too early"},
                "id": "submit",
            },
        ],
        gate,
    )
    results = {call.tool_call_id: call.result for call in outcome.calls}

    assert results["checks"].returncode == 0
    assert results["edit"].returncode == 0
    assert results["submit"].blocked is True
    assert results["submit"].exception_info == "VerificationRequired"


class FailedTestThenProseModel:
    def __init__(self):
        self.calls = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="run tests",
                tool_calls=[tool_call("run_tests", "test-1")],
            )
        return AIMessage(content="done")


def test_chat_failed_test_cannot_be_completed_by_plain_prose(tmp_path: Path):
    session = ConversationalCodeAgent(
        FailedTestThenProseModel(),
        BashExecutor(
            tmp_path,
            approval_mode="yolo",
            sandbox_mode="none",
            default_test_command="false",
        ),
        max_steps_per_turn=3,
        quiet=True,
    )

    result = session.respond_turn("fix it", coding_mode=True)

    assert not result.completed
    assert result.status == "turn_step_limit_unverified"
    assert session.verification_status == "failed"


class ProseOnlyModel:
    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        return AIMessage(content="Here is the explanation; no edit is needed.")


def test_code_mode_permission_does_not_require_verification_without_tools(
    tmp_path: Path,
):
    session = ConversationalCodeAgent(
        ProseOnlyModel(),
        BashExecutor(tmp_path, approval_mode="yolo", sandbox_mode="none"),
        max_steps_per_turn=2,
        quiet=True,
    )

    result = session.respond_turn("Explain the design", coding_mode=True)

    assert result.completed
    assert result.status == "answered"
    assert result.steps == 1
    assert session.verification_status == "not_required"


class FailedEditThenProseModel:
    def __init__(self):
        self.calls = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="try edit",
                tool_calls=[
                    tool_call(
                        "apply_patch",
                        "missing-edit",
                        {"path": "missing.py", "old": "before", "new": "after"},
                    )
                ],
            )
        return AIMessage(content="The requested file does not exist, so nothing changed.")


def test_failed_mutating_tool_without_workspace_change_does_not_require_verification(
    tmp_path: Path,
):
    session = ConversationalCodeAgent(
        FailedEditThenProseModel(),
        BashExecutor(tmp_path, approval_mode="yolo", sandbox_mode="none"),
        max_steps_per_turn=2,
        quiet=True,
    )

    result = session.respond_turn("Try the edit", coding_mode=True)

    assert result.completed
    assert result.status == "answered"
    assert session.verification_status == "not_required"


def test_chat_cli_prints_structured_turn_and_returns_to_ask_after_submit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    created: dict[str, object] = {}

    class FakeSession:
        def __init__(self, model, executor, **kwargs):
            created["access"] = executor

        def respond(self, *args, **kwargs):
            raise AssertionError("chat CLI must use respond_turn")

        def respond_turn(self, user_text: str, *, coding_mode: bool):
            created["user_text"] = user_text
            created["coding_mode"] = coding_mode
            return TurnResult(
                text="updated note",
                status="submitted",
                completed=True,
                verified=True,
                steps=3,
            )

        def close(self):
            created["closed"] = True

    class TtyInput:
        @staticmethod
        def isatty() -> bool:
            return True

    inputs = iter(["/code update the note", "/exit"])
    monkeypatch.setattr(cli_module.sys, "stdin", TtyInput())
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))
    monkeypatch.setattr(cli_module, "_load_runtime_env", lambda _path: None)
    monkeypatch.setattr(cli_module, "_model_from_args", lambda _args: object())
    monkeypatch.setattr(cli_module, "_require_working_sandbox", lambda _executor: None)
    monkeypatch.setattr(
        cli_module,
        "_resume_output_path",
        lambda _resume, _output, _kind: tmp_path / "session.chat.json",
    )
    monkeypatch.setattr(cli_module, "ConversationalCodeAgent", FakeSession)

    args = build_parser().parse_args(
        [
            "chat",
            "--cwd",
            str(tmp_path),
            "--model",
            "deepseek",
            "--test-command",
            "pytest -q",
            "--sandbox",
            "none",
            "--allow-dirty",
        ]
    )

    assert cli_module.chat_command(args) == 0

    output = capsys.readouterr().out
    assert "turn: status=submitted completed=true verified=true steps=3 error=none" in output
    assert "mode=/ask (coding turn submitted; use /code for another coding task)" in output
    assert created["coding_mode"] is True
    assert created["access"].mode == "ask"
    assert created["closed"] is True


def test_run_agent_passes_named_checks_to_the_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    created: dict[str, object] = {}

    class FakeExecutor:
        def __init__(self, cwd: Path, **kwargs):
            self.cwd = Path(cwd)
            created["executor_kwargs"] = kwargs

    class FakeAgent:
        def __init__(self, model, executor, **kwargs):
            created["agent_executor"] = executor

        def run(self, task: str, *, resume_data=None):
            return {
                "exit_status": "Submitted",
                "workspace_changes": {
                    "created": [],
                    "deleted": [],
                    "modified": [],
                },
                "sandbox": "disabled",
                "submission": "",
                "events": [],
            }

    monkeypatch.setattr(cli_module, "_load_runtime_env", lambda _path: None)
    monkeypatch.setattr(cli_module, "_model_from_args", lambda _args: object())
    monkeypatch.setattr(
        cli_module, "_require_working_sandbox", lambda _executor: None
    )
    monkeypatch.setattr(
        cli_module,
        "_resume_output_path",
        lambda _resume, _output, _kind: tmp_path / "run.json",
    )
    monkeypatch.setattr(cli_module, "_load_bash_executor", lambda: FakeExecutor)
    monkeypatch.setattr(cli_module, "_load_mini_code_agent", lambda: FakeAgent)

    args = build_parser().parse_args(
        [
            "run",
            "task",
            "--cwd",
            str(tmp_path),
            "--model",
            "deepseek",
            "--check",
            "tests",
            "pytest -q",
            "--check",
            "lint",
            "ruff check .",
            "--yes",
            "--sandbox",
            "none",
            "--allow-dirty",
        ]
    )

    assert cli_module.run_agent(args) == 0
    kwargs = created["executor_kwargs"]
    assert kwargs["default_test_command"] is None
    assert [item.name for item in kwargs["verification_checks"]] == [
        "tests",
        "lint",
    ]


def test_chat_command_named_check_enables_code_and_sandbox_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    created: dict[str, object] = {}
    sandbox_probes: list[object] = []

    class FakeExecutor:
        def __init__(self, cwd: Path, **kwargs):
            self.cwd = Path(cwd)
            created["executor"] = self
            created["executor_kwargs"] = kwargs

    class FakeSession:
        def __init__(self, model, executor, **kwargs):
            created["access"] = executor

        def respond_turn(self, user_text: str, *, coding_mode: bool):
            created["turn"] = (user_text, coding_mode)
            return TurnResult(
                text="done",
                status="submitted",
                completed=True,
                verified=True,
                steps=1,
            )

        def close(self):
            created["closed"] = True

    class TtyInput:
        @staticmethod
        def isatty() -> bool:
            return True

    inputs = iter(["/code fix it", "/exit"])
    monkeypatch.setattr(cli_module.sys, "stdin", TtyInput())
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))
    monkeypatch.setattr(cli_module, "_load_runtime_env", lambda _path: None)
    monkeypatch.setattr(cli_module, "_model_from_args", lambda _args: object())
    monkeypatch.setattr(cli_module, "_load_bash_executor", lambda: FakeExecutor)
    monkeypatch.setattr(
        cli_module, "_load_conversational_code_agent", lambda: FakeSession
    )
    monkeypatch.setattr(
        cli_module,
        "_require_working_sandbox",
        sandbox_probes.append,
    )
    monkeypatch.setattr(
        cli_module,
        "_resume_output_path",
        lambda _resume, _output, _kind: tmp_path / "session.chat.json",
    )
    args = build_parser().parse_args(
        [
            "chat",
            "--cwd",
            str(tmp_path),
            "--model",
            "deepseek",
            "--check",
            "tests",
            "pytest -q",
            "--yes",
            "--sandbox",
            "none",
            "--allow-dirty",
        ]
    )

    assert cli_module.chat_command(args) == 0
    kwargs = created["executor_kwargs"]
    assert kwargs["default_test_command"] is None
    assert [item.name for item in kwargs["verification_checks"]] == ["tests"]
    assert sandbox_probes == [created["executor"]]
    assert created["turn"][1] is True
    assert created["access"]._coding_enabled is True
    assert created["closed"] is True


def test_chat_code_command_without_test_command_stays_in_ask_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    created: dict[str, object] = {}

    class FakeSession:
        def __init__(self, model, executor, **kwargs):
            created["access"] = executor

        def respond_turn(self, user_text: str, *, coding_mode: bool):
            calls = created.setdefault("turns", [])
            calls.append((user_text, coding_mode))
            return TurnResult(
                text="ordinary answer",
                status="answered",
                completed=True,
                verified=False,
                steps=1,
            )

        def close(self):
            created["closed"] = True

    class TtyInput:
        @staticmethod
        def isatty() -> bool:
            return True

    inputs = iter(["What does this project do?", "/code edit", "/exit"])

    def fail_sandbox_probe(_executor):
        raise RuntimeError("no sandbox backend is available")

    monkeypatch.setattr(cli_module.sys, "stdin", TtyInput())
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))
    monkeypatch.setattr(cli_module, "_load_runtime_env", lambda _path: None)
    monkeypatch.setattr(cli_module, "_model_from_args", lambda _args: object())
    monkeypatch.setattr(
        cli_module,
        "_require_working_sandbox",
        fail_sandbox_probe,
    )
    monkeypatch.setattr(
        cli_module,
        "_resume_output_path",
        lambda _resume, _output, _kind: tmp_path / "session.chat.json",
    )
    monkeypatch.setattr(cli_module, "ConversationalCodeAgent", FakeSession)

    args = build_parser().parse_args(
        [
            "chat",
            "--cwd",
            str(tmp_path),
            "--model",
            "deepseek",
            "--allow-dirty",
        ]
    )

    assert cli_module.chat_command(args) == 0

    output = capsys.readouterr().out
    assert "ordinary answer" in output
    assert "restart with --test-command" in output
    assert created["turns"] == [
        (
            "[Read-only /ask mode is enforced: explain or inspect only; do not request "
            "write, shell, or test tools.]\n\nWhat does this project do?",
            False,
        )
    ]
    assert created["access"].mode == "ask"
    assert created["closed"] is True


class TwoToolModel:
    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        return AIMessage(
            content="inspect",
            tool_calls=[
                tool_call("list_files", "list-1"),
                tool_call("read_file", "read-1", {"path": "missing.txt"}),
            ],
        )


def test_batch_fingerprint_error_still_pairs_every_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    original = verification_module.capture_workspace_fingerprint
    calls = 0

    def flaky_capture(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected fingerprint failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        verification_module, "capture_workspace_fingerprint", flaky_capture
    )
    session = ConversationalCodeAgent(
        TwoToolModel(),
        BashExecutor(tmp_path, approval_mode="yolo", sandbox_mode="none"),
        max_steps_per_turn=1,
        quiet=True,
    )

    result = session.respond_turn("inspect")
    tool_messages = [message for message in session.messages if message.type == "tool"]
    observations = [json.loads(str(message.content)) for message in tool_messages]

    assert not result.completed
    assert len(tool_messages) == 2
    assert {message.tool_call_id for message in tool_messages} == {"list-1", "read-1"}
    assert [observation["exception_info"] for observation in observations] == [
        "WorkspaceFingerprintError",
        "WorkspaceFingerprintError",
    ]


def test_ask_mode_is_a_read_only_allowlist(tmp_path: Path):
    access = ChatAccessController(
        BashExecutor(tmp_path, approval_mode="yolo", sandbox_mode="none")
    )

    assert access.execute_tool("list_files", {}).returncode == 0
    for name in ["write_file", "run_tests", "submit", "future_mutator"]:
        result = access.execute_tool(name, {})
        assert result.blocked
        assert result.exception_info == "ReadOnlyChatMode"


def test_chat_controller_blocks_coding_when_test_command_is_unavailable(
    tmp_path: Path,
):
    access = ChatAccessController(
        BashExecutor(tmp_path, approval_mode="yolo", sandbox_mode="none"),
        coding_enabled=False,
    )
    access.mode = "code"

    result = access.execute_tool("write_file", {"path": "value.txt", "content": "x"})

    assert result.blocked
    assert result.exception_info == "TestCommandRequired"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _init_git_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Tests")


def test_git_inspection_is_read_only_and_disables_repo_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / ".gitattributes").write_text("*.foo diff=evil\n", encoding="utf-8")
    (repo / "value.foo").write_text("before\n", encoding="utf-8")
    helper_marker = tmp_path / "textconv-ran"
    helper = tmp_path / "textconv.sh"
    helper.write_text(
        f"#!/bin/sh\ntouch '{helper_marker}'\ncat \"$1\"\n", encoding="utf-8"
    )
    helper.chmod(0o700)
    _git(repo, "config", "diff.evil.textconv", str(helper))

    fsmonitor_marker = tmp_path / "fsmonitor-saw-key"
    fsmonitor = tmp_path / "fsmonitor.sh"
    fsmonitor.write_text(
        f"#!/bin/sh\n[ -n \"$DEEPSEEK_API_KEY\" ] && touch '{fsmonitor_marker}'\nexit 0\n",
        encoding="utf-8",
    )
    fsmonitor.chmod(0o700)
    _git(repo, "config", "core.fsmonitor", str(fsmonitor))
    _git(repo, "add", ".gitattributes", "value.foo")
    _git(repo, "commit", "-qm", "initial")
    (repo / "value.foo").write_text("after\n", encoding="utf-8")

    index = repo / ".git" / "index"
    before_index = (index.stat().st_mtime_ns, index.read_bytes())
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-parent-secret-123456")

    assert _git_dirty(repo)
    result = BashExecutor(repo, approval_mode="yolo", sandbox_mode="none").git_diff()

    assert result.returncode == 0
    assert not helper_marker.exists()
    assert not fsmonitor_marker.exists()
    assert (index.stat().st_mtime_ns, index.read_bytes()) == before_index


def test_shell_child_gets_no_parent_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-parent-secret-123456")
    monkeypatch.setenv("INTERNAL_TOKEN", "token-parent-secret")
    executor = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        allow_shell=True,
        sandbox_mode="none",
    )

    result = executor.execute_bash(
        "python3 -c 'import os; print(bool(os.getenv(\"DEEPSEEK_API_KEY\")), "
        "bool(os.getenv(\"INTERNAL_TOKEN\")))'"
    )

    assert result.returncode == 0
    assert "False False" in result.output


def test_search_rejects_catastrophic_regex_without_hanging(tmp_path: Path):
    (tmp_path / "large.txt").write_text("a" * 10_000 + "!\n", encoding="utf-8")
    executor = BashExecutor(tmp_path, approval_mode="yolo")

    result = executor.execute_tool(
        "search_files", {"pattern": "(a+)+$", "path": "."}
    )

    assert result.returncode == -1
    assert "time limit" in result.exception_info


def test_search_does_not_follow_workspace_symlinks(tmp_path: Path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-secret.txt"
    outside.write_text("outside-only-secret", encoding="utf-8")
    try:
        (tmp_path / "linked-secret.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")

    result = BashExecutor(tmp_path, approval_mode="yolo").search_files("outside-only")

    assert result.returncode == 0
    assert "outside-only-secret" not in result.output


def test_context_budget_includes_tool_args_and_reasoning_without_system_escalation():
    messages = [SystemMessage(content="system")]
    messages.extend(HumanMessage(content="old user text " * 100) for _ in range(8))
    messages.extend(
        [
            AIMessage(
                content="",
                tool_calls=[
                    tool_call(
                        "write_file",
                        "huge-1",
                        {"path": "x.txt", "content": "x" * 100_000},
                    )
                ],
                additional_kwargs={"reasoning_content": "r" * 100_000},
            ),
            ToolMessage(content="result" * 20_000, tool_call_id="huge-1"),
        ]
    )

    compacted = compact_messages(messages, max_chars=1_000)

    assert sum(_message_size(message) for message in compacted) <= 1_000
    assert sum(message.type == "system" for message in compacted) == 1
    assert all(
        not getattr(message, "tool_calls", [])
        or message.additional_kwargs.get("reasoning_content") == "r" * 100_000
        for message in compacted
    )


def test_authenticated_undo_is_deterministic_and_tamper_evident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state = tmp_path / "state"
    monkeypatch.setenv("MCA_STATE_DIR", str(state))
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "value.txt"
    target.write_text("before\n", encoding="utf-8")
    trajectory = tmp_path / "run.json"
    edit = BashExecutor(repo, approval_mode="yolo").write_file("value.txt", "after\n")
    records = [
        {
            "path": edit.file_path,
            "existed_before": edit.file_existed_before,
            "before_content": edit.before_content,
            "before_hash": edit.before_hash,
            "after_hash": edit.after_hash,
        }
    ]

    first = write_undo_journal(trajectory, repo, records)
    second = write_undo_journal(trajectory, repo, records)
    assert first == second
    journals = list((state / "undo").glob("undo-*.json"))
    assert len(journals) == 1
    assert stat.S_IMODE(journals[0].stat().st_mode) == 0o600
    assert stat.S_IMODE((state / "undo" / "journal.key").stat().st_mode) == 0o600

    trajectory.write_text(
        json.dumps({"cwd": str(repo), "undo_journal": first, "events": []}),
        encoding="utf-8",
    )
    envelope = json.loads(journals[0].read_text(encoding="utf-8"))
    envelope["payload"]["records"][0]["before_content"] = "tampered\n"
    journals[0].write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(ValueError, match="integrity check failed"):
        undo_trajectory(load_trajectory(trajectory))


def test_unsigned_legacy_undo_requires_explicit_opt_in(tmp_path: Path):
    target = tmp_path / "value.txt"
    target.write_text("after\n", encoding="utf-8")
    data = {
        "cwd": str(tmp_path),
        "events": [
            {
                "type": "tool",
                "tool": "write_file",
                "args": {"path": "value.txt"},
                "before_content": "before\n",
            }
        ],
    }

    with pytest.raises(ValueError, match="authenticated undo journal"):
        undo_trajectory(data)
    undo_trajectory(data, allow_legacy_unsafe=True)
    assert target.read_text(encoding="utf-8") == "before\n"


class InspectOnceModel:
    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        return AIMessage(
            content="inspect", tool_calls=[tool_call("list_files", "inspect-1")]
        )


class VerifyAndSubmitModel:
    def __init__(self):
        self.calls = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="verify", tool_calls=[tool_call("run_tests", "verify-1")]
            )
        return AIMessage(
            content="submit", tool_calls=[tool_call("submit", "submit-1")]
        )


class ConnectionFailureModel:
    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        try:
            raise ConnectionError("provider detail")
        except ConnectionError as exc:
            outer = RuntimeError("connection wrapper")
            outer.request_id = "raw-provider-request-id"
            raise outer from exc


def test_model_failures_are_counted_and_repeated_resume_notice_is_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MCA_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    trajectory_path = tmp_path / "run.json"

    first = MiniCodeAgent(
        ConnectionFailureModel(),
        BashExecutor(repo, approval_mode="yolo", sandbox_mode="none"),
        trajectory_path=trajectory_path,
        quiet=True,
    ).run("inspect")
    second = MiniCodeAgent(
        ConnectionFailureModel(),
        BashExecutor(repo, approval_mode="yolo", sandbox_mode="none"),
        trajectory_path=trajectory_path,
        quiet=True,
    ).run(resume_data=first)
    third = MiniCodeAgent(
        ConnectionFailureModel(),
        BashExecutor(repo, approval_mode="yolo", sandbox_mode="none"),
        trajectory_path=trajectory_path,
        quiet=True,
    ).run(resume_data=second)

    assert third["model_usage"]["model_calls"] == 0
    assert third["model_usage"]["model_attempts"] == 3
    assert third["model_usage"]["model_failures"] == 3
    error = third["events"][-1]
    assert error["operation"] == "model.invoke"
    assert error["model_attempt"] == 3
    assert error["duration_ms"] >= 0
    assert error["cause_types"] == ["RuntimeError", "ConnectionError"]
    assert len(error["request_id_sha256"]) == 64
    assert "raw-provider-request-id" not in json.dumps(third)
    resume_notices = [
        message
        for message in third["messages"]
        if message.get("type") == "human"
        and message.get("data", {}).get("content") == MiniCodeAgent.RESUME_NOTICE
    ]
    assert len(resume_notices) == 1


def test_run_checkpoint_can_resume_from_last_safe_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MCA_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    trajectory = tmp_path / "run.json"
    first = MiniCodeAgent(
        InspectOnceModel(),
        BashExecutor(repo, approval_mode="yolo", sandbox_mode="none"),
        max_steps=1,
        trajectory_path=trajectory,
        quiet=True,
    ).run("inspect and verify")
    assert first["exit_status"] == "StepLimitExceeded"
    assert first["resumable"] is True
    assert first["model_usage"]["model_calls"] == 1

    resume_data = load_trajectory(trajectory)
    resume_data["duration_ms"] = 100_000
    resumed = MiniCodeAgent(
        VerifyAndSubmitModel(),
        BashExecutor(
            repo,
            approval_mode="yolo",
            sandbox_mode="none",
            default_test_command="true",
        ),
        max_steps=5,
        trajectory_path=trajectory,
        quiet=True,
    ).run(resume_data=resume_data)

    assert resumed["exit_status"] == "Submitted"
    assert resumed["resumable"] is False
    assert resumed["steps"] == 3
    assert resumed["model_usage"]["model_calls"] == 3
    assert resumed["duration_ms"] >= 100_000


def test_resume_discards_prior_matrix_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MCA_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    trajectory_path = tmp_path / "resume.json"
    check = VerificationCheck(
        "tests",
        f"{shlex.quote(sys.executable)} -c 'print(\"Ran 1 test\")'",
    )

    class CheckOnceModel:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            return AIMessage(
                content="verify",
                tool_calls=[tool_call("run_tests", "initial-check")],
            )

    class SubmitThenCheckAndSubmitModel:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(
                    content="try stale submit",
                    tool_calls=[tool_call("submit", "stale-submit")],
                )
            if self.calls == 2:
                return AIMessage(
                    content="verify again",
                    tool_calls=[tool_call("run_tests", "fresh-check")],
                )
            return AIMessage(
                content="submit",
                tool_calls=[tool_call("submit", "submit")],
            )

    first = MiniCodeAgent(
        CheckOnceModel(),
        BashExecutor(
            repo,
            approval_mode="yolo",
            sandbox_mode="none",
            verification_checks=(check,),
        ),
        max_steps=1,
        trajectory_path=trajectory_path,
        quiet=True,
    ).run("verify")
    checkpoint_steps = first["steps"]
    assert first["verification_status"] == "passed"

    resumed = MiniCodeAgent(
        SubmitThenCheckAndSubmitModel(),
        BashExecutor(
            repo,
            approval_mode="yolo",
            sandbox_mode="none",
            verification_checks=(check,),
        ),
        max_steps=4,
        trajectory_path=trajectory_path,
        quiet=True,
    ).run(resume_data=load_trajectory(trajectory_path))
    resumed_events = [
        event
        for event in resumed["events"]
        if int(event.get("step", 0)) > checkpoint_steps
    ]
    tool_events = [
        event
        for event in resumed_events
        if event.get("type") == "tool"
    ]
    assert [event["tool"] for event in tool_events] == [
        "submit",
        "run_tests",
        "submit",
    ]
    stale_submit, fresh_check, accepted_submit = tool_events

    assert stale_submit["blocked"] is True
    assert stale_submit["exception_info"] == "VerificationRequired"
    assert fresh_check["verification_checks"][0]["name"] == "tests"
    assert accepted_submit["submitted"] is True
    assert resumed["exit_status"] == "Submitted"


class CountingChatModel:
    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        human_count = sum(message.type == "human" for message in messages)
        return AIMessage(content=f"humans={human_count}")


def test_chat_trajectory_can_resume_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MCA_STATE_DIR", str(tmp_path / "state"))
    trajectory = tmp_path / "chat.json"
    first = ConversationalCodeAgent(
        CountingChatModel(),
        BashExecutor(tmp_path, approval_mode="yolo", sandbox_mode="none"),
        trajectory_path=trajectory,
        quiet=True,
    )
    assert first.respond("one") == "humans=1"
    first.close()

    resumed = ConversationalCodeAgent(
        CountingChatModel(),
        BashExecutor(tmp_path, approval_mode="yolo", sandbox_mode="none"),
        trajectory_path=trajectory,
        quiet=True,
        resume_data=load_trajectory(trajectory),
    )

    assert resumed.respond("two") == "humans=2"


def test_deepseek_reasoning_round_trips_and_timeout_must_be_finite():
    model = create_model(
        "deepseek", api_key="ds-test-key", deepseek_thinking=True
    )
    messages = [
        HumanMessage(content="inspect"),
        AIMessage(
            content="",
            tool_calls=[tool_call("list_files", "deepseek-1")],
            additional_kwargs={"reasoning_content": "private reasoning"},
        ),
        ToolMessage(content="ok", tool_call_id="deepseek-1"),
    ]

    payload = model._get_request_payload(messages)

    assert payload["messages"][1]["reasoning_content"] == "private reasoning"
    assert payload["messages"][1]["content"] == ""
    assert model.extra_body == {"thinking": {"type": "enabled"}}
    with pytest.raises(ValueError, match="greater than zero"):
        create_model("deepseek", api_key="x", request_timeout=float("nan"))


def test_openai_streaming_reasoning_stays_on_chat_completions():
    model = create_model(
        "gpt-compatible",
        provider="openai",
        api_key="test-key",
        streaming=True,
        reasoning_effort="low",
    )

    assert model.streaming is True
    assert model.stream_usage is True
    assert model.reasoning_effort == "low"
    assert model.use_responses_api is False


def test_reasoning_effort_rejects_non_openai_provider():
    with pytest.raises(ValueError, match="OpenAI provider"):
        create_model(
            "deepseek",
            api_key="test-key",
            reasoning_effort="low",
        )


def test_auto_sandbox_never_silently_degrades_for_direct_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(executor_module.shutil, "which", lambda name: None)
    executor = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="auto",
        default_test_command="true",
    )

    assert executor.sandbox_probe() == (False, "unavailable")
    result = executor.execute_tool("run_tests", {})
    assert result.returncode == -1
    assert "no sandbox backend is available" in result.exception_info


def test_auto_sandbox_falls_back_after_an_unusable_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executor = BashExecutor(tmp_path, approval_mode="yolo", sandbox_mode="auto")
    monkeypatch.setattr(
        executor,
        "_trusted_executable",
        lambda name: f"/usr/bin/{name}" if name in {"sandbox-exec", "docker"} else "",
    )
    attempts: list[str] = []

    def fake_run(argv, *, sandbox=True, timeout_seconds=None):
        attempts.append(executor._resolved_sandbox_mode or "")
        return subprocess.CompletedProcess(
            argv,
            0 if executor._resolved_sandbox_mode == "docker" else 71,
            "denied",
        )

    monkeypatch.setattr(executor, "_run_argv", fake_run)

    assert executor.sandbox_probe() == (True, "docker")
    assert attempts == ["sandbox-exec", "docker"]
    assert executor.sandbox_status() == "docker"


def test_apply_patch_requires_a_real_json_boolean(tmp_path: Path):
    target = tmp_path / "value.txt"
    target.write_text("A\nA\n", encoding="utf-8")
    executor = BashExecutor(tmp_path, approval_mode="yolo", sandbox_mode="none")

    result = executor.execute_tool(
        "apply_patch",
        {"path": "value.txt", "old": "A", "new": "B", "replace_all": "false"},
    )

    assert result.returncode == -1
    assert "JSON boolean" in result.exception_info
    assert target.read_text(encoding="utf-8") == "A\nA\n"


def test_blank_authoritative_test_command_is_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="must not be blank"):
        BashExecutor(tmp_path, default_test_command="   ")
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "task", "--test-command", "   "])


def test_git_dirty_check_fails_closed_when_status_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, "true\n", ""),
            subprocess.CompletedProcess([], 0, f"{tmp_path}\n", ""),
            subprocess.CompletedProcess([], 128, "", "broken index"),
        ]
    )
    monkeypatch.setattr(cli_module.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(cli_module.subprocess, "run", lambda *args, **kwargs: next(responses))

    with pytest.raises(RuntimeError, match="git status failed"):
        _git_dirty(tmp_path)


def test_state_file_read_and_write_limits_are_symmetric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(utils_module, "MAX_STATE_FILE_BYTES", 1024)
    monkeypatch.setattr(trajectory_module, "MAX_STATE_FILE_BYTES", 1024)
    valid = tmp_path / "valid.json"
    write_json(valid, {"value": "ok"})
    assert load_trajectory(valid)["value"] == "ok"

    oversized = tmp_path / "oversized.json"
    with pytest.raises(ValueError, match="safety limit"):
        write_json(oversized, {"value": "x" * 2048})
    oversized.write_bytes(b"{" + b" " * 2048 + b"}")
    with pytest.raises(ValueError, match="safety limit"):
        load_trajectory(oversized)


def test_audit_events_omit_full_structured_edit_payloads():
    marker = "private-source-marker"
    audited = audit_tool_args(
        "write_file", {"path": "value.py", "content": marker}
    )

    assert marker not in json.dumps(audited)
    assert audited["content"]["chars"] == len(marker)
    assert len(audited["content"]["sha256"]) == 64


def test_chat_persistent_audit_history_is_bounded(tmp_path: Path):
    session = ConversationalCodeAgent(
        CountingChatModel(),
        BashExecutor(tmp_path, approval_mode="yolo", sandbox_mode="none"),
        quiet=True,
    )
    session.events = [{"type": "model", "step": index} for index in range(2100)]

    session._save("chatting")

    assert len(session.events) == MAX_PERSISTED_EVENTS
    assert session.events_omitted == 100


def test_default_init_env_file_is_loaded_automatically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = tmp_path / "config"
    config.mkdir()
    env_file = config / "env"
    env_file.write_text("DEEPSEEK_API_KEY=default-private-key\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setenv("MCA_CONFIG_DIR", str(config))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    loaded = _load_runtime_env(None)

    assert loaded == env_file
    assert cli_module.os.environ["DEEPSEEK_API_KEY"] == "default-private-key"


def test_docker_sandbox_uses_the_configured_prepulled_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executor = BashExecutor(
        tmp_path,
        sandbox_mode="docker",
        docker_image="local/mca-node:20",
    )
    monkeypatch.setattr(
        executor,
        "_trusted_executable",
        lambda name: "/usr/local/bin/docker" if name == "docker" else "",
    )

    argv = executor._sandboxed_argv(["node", "--version"])

    assert "--pull=never" in argv
    assert "local/mca-node:20" in argv
    assert argv[-2:] == ["node", "--version"]


def test_matrix_uses_the_same_trusted_ignore_paths_inside_executor_and_gate(
    tmp_path: Path,
):
    artifact = tmp_path / "run.json"
    command = (
        f"{shlex.quote(sys.executable)} -c "
        + shlex.quote(
            "from pathlib import Path; "
            "Path('run.json').write_text('runtime artifact'); "
            "print('check passed')"
        )
    )
    executor = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        verification_checks=(VerificationCheck("tests", command),),
    )
    baseline = capture_workspace_fingerprint(
        executor, ignore_paths={artifact}
    )
    gate = VerificationGate.create(baseline, require_verification=True)

    outcome = execute_tool_batch(
        executor,
        [{"name": "run_tests", "args": {}, "id": "checks"}],
        gate,
        ignore_paths={artifact},
    )
    result = outcome.calls[0].result

    assert artifact.read_text(encoding="utf-8") == "runtime artifact"
    assert result.returncode == 0
    assert result.verification_boundary_checked is True
    assert result.verification_fingerprint == baseline
    assert gate.status == "passed"


@pytest.mark.parametrize(
    ("matrix_evidence", "internal_fingerprint", "post_fingerprint"),
    [(True, "f0", "f1"), (True, "", "f0"), (False, "", "f0")],
    ids=("matrix-different", "matrix-missing", "legacy-missing"),
)
def test_verification_handoff_requires_matching_internal_fingerprint(
    tmp_path: Path,
    matrix_evidence: bool,
    internal_fingerprint: str,
    post_fingerprint: str,
):
    artifact = tmp_path / "run.json"

    class IdentityRedactor:
        def redact_text(self, value: str) -> str:
            return value

        def redact_data(self, value):
            return value

    class HandoffExecutor:
        cwd = tmp_path
        redactor = IdentityRedactor()

        def __init__(self):
            self.captures = 0
            self.received_args = None

        def workspace_fingerprint(self, *, ignore_paths=None):
            self.captures += 1
            return "f0" if self.captures == 1 else post_fingerprint

        def execute_tool(self, name: str, args: dict) -> ToolResult:
            self.received_args = args
            return ToolResult(
                tool=name,
                output="passed",
                returncode=0,
                duration_ms=1,
                verification_checks=(
                    (
                        VerificationCheckEvidence(
                            name="tests", returncode=0, duration_ms=1
                        ),
                    )
                    if matrix_evidence
                    else ()
                ),
                verification_boundary_checked=not matrix_evidence,
                verification_fingerprint=internal_fingerprint,
            )

        def sandbox_status(self) -> str:
            return "fake"

    executor = HandoffExecutor()
    gate = VerificationGate.create("f0", require_verification=True)
    outcome = execute_tool_batch(
        executor,
        [
            {
                "name": "run_tests",
                "args": {"_verification_ignore_paths": ["malicious"]},
                "id": "checks",
            },
            {
                "name": "submit",
                "args": {"summary": "must fail"},
                "id": "submit",
            },
        ],
        gate,
        ignore_paths={artifact},
    )
    results = {call.tool_call_id: call.result for call in outcome.calls}

    assert executor.received_args == {
        "_verification_ignore_paths": {artifact}
    }
    assert results["checks"].exception_info == (
        "WorkspaceChangedDuringVerification"
    )
    assert results["submit"].blocked is True
    assert gate.status == "failed"
