from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import mini_code_agent.transaction as transaction_module
from mini_code_agent import cli as cli_module
from mini_code_agent import transaction_cli
from mini_code_agent.contracts import ToolResult
from mini_code_agent.memory_adapters.project import GitProjectIdentityProvider
from mini_code_agent.receipt import ReceiptError
from mini_code_agent.transaction import TransactionError, TransactionStore
from mini_code_agent.transaction_adapter import TransactionExecutor
from mini_code_agent.transaction_cli import _next_resume_max_steps
from mini_code_agent.utils import command_from_argv
from mini_code_agent.workspace import WorkspaceSnapshot


def test_generated_resume_step_limit_can_advance_past_checkpoint():
    assert _next_resume_max_steps(2, {"steps": 2}) == 12
    assert _next_resume_max_steps(50, {"steps": 50}) == 100
    assert _next_resume_max_steps(50, {"steps": 12}) == 50


def test_open_transaction_persists_transaction_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = _repository(tmp_path)
    state_root = tmp_path / "state"

    class FakeExecutor:
        def __init__(self, cwd: Path, **_kwargs: Any):
            self.cwd = Path(cwd)
            self.redactor = object()

    class StepLimitedAgent:
        def __init__(self, _model: Any, _executor: Any, **_kwargs: Any):
            pass

        def run(self, _task: str, **_kwargs: Any) -> dict[str, Any]:
            return {
                "steps": 1,
                "exit_status": "StepLimitExceeded",
                "resumable": True,
                "workspace_changes": {
                    "created": [],
                    "modified": [],
                    "deleted": [],
                },
            }

    monkeypatch.setenv("MCA_STATE_DIR", str(state_root))
    monkeypatch.setattr(cli_module, "_load_runtime_env", lambda _path: None)
    monkeypatch.setattr(cli_module, "_load_bash_executor", lambda: FakeExecutor)
    monkeypatch.setattr(
        cli_module, "_load_mini_code_agent", lambda: StepLimitedAgent
    )
    monkeypatch.setattr(cli_module, "_model_from_args", lambda _args: object())
    monkeypatch.setattr(
        cli_module, "_require_working_sandbox", lambda _executor: None
    )
    args = cli_module.build_parser().parse_args(
        [
            "tx",
            "run",
            "inspect",
            "--cwd",
            str(source),
            "--model",
            "deepseek",
            "--test-command",
            "true",
            "--sandbox",
            "none",
            "--yes",
        ]
    )

    assert transaction_cli.agent_command(args, resume=False) == 2
    transaction_id = next(
        path.name for path in (state_root / "transactions").iterdir()
        if path.is_dir()
    )
    trajectory = json.loads(
        TransactionStore(state_root)
        .trajectory(transaction_id)
        .read_text(encoding="utf-8")
    )
    assert trajectory["memory"] == {"mode": "off", "retrieval": None}


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "transaction@example.invalid")
    _git(root, "config", "user.name", "Transaction Test")
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    return root


def _verified(workspace: Path) -> dict[str, Any]:
    fingerprint = WorkspaceSnapshot.capture(workspace).fingerprint
    return {
        "exit_status": "Submitted",
        "verification_status": "passed",
        "verified_fingerprint": fingerprint,
        "events": [
            {
                "type": "tool",
                "tool": "run_tests",
                "returncode": 0,
                "verification_checks": [
                    {
                        "name": "tests",
                        "returncode": 0,
                        "duration_ms": 1,
                        "blocked": False,
                        "approved": True,
                    }
                ],
            }
        ],
    }


def test_transaction_isolates_then_commits_exact_prepared_state(tmp_path: Path):
    source = _repository(tmp_path)
    (source / "ignored.txt").write_text("local cache\n", encoding="utf-8")
    store = TransactionStore(tmp_path / "state")
    manifest = store.create(source, task="change values")
    workspace = store.workspace(manifest["id"])

    (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    (workspace / "new.py").write_text("NEW = True\n", encoding="utf-8")
    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    prepared = store.prepare(manifest["id"], _verified(workspace))
    assert prepared["status"] == "prepared"
    receipt = store.receipt(manifest["id"])
    assert receipt["receipt_id"] == prepared["receipt_id"]
    assert receipt["payload"]["prepared"]["patch_sha256"]
    assert len(receipt["payload"]["workspace"]["identity_sha256"]) == 64
    assert receipt["payload"]["memory"]["mode"] == "off"
    assert receipt["payload"]["verification"]["checks"][0]["name"] == "tests"
    committed = store.commit(manifest["id"])

    assert committed["status"] == "committed"
    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (source / "new.py").read_text(encoding="utf-8") == "NEW = True\n"
    assert (source / "ignored.txt").read_text(encoding="utf-8") == "local cache\n"
    assert not workspace.exists()


def test_local_memory_project_identity_survives_checkout_move(tmp_path: Path):
    source = _repository(tmp_path)
    provider = GitProjectIdentityProvider()

    first = provider.identity_sha256(source, create=True)
    moved = tmp_path / "moved-repo"
    source.rename(moved)
    second = provider.identity_sha256(moved, create=False)

    assert first == second
    assert len(first) == 64
    assert first != hashlib.sha256(str(source.resolve()).encode()).hexdigest()


def test_windows_crlf_worktree_is_clean_when_index_is_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "repo"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "transaction@example.invalid")
    _git(source, "config", "user.name", "Transaction Test")
    (source / "app.py").write_bytes(b"VALUE = 1\r\n")
    _git(source, "-c", "core.autocrlf=true", "add", "app.py")
    _git(source, "commit", "-m", "baseline")
    monkeypatch.setattr(transaction_module, "_is_windows_platform", lambda: True)

    store = TransactionStore(tmp_path / "state")
    manifest = store.create(source, task="verify Windows line endings")

    assert manifest["status"] == "open"
    assert (source / "app.py").read_bytes() == b"VALUE = 1\r\n"
    store.abort(manifest["id"])


def test_commit_refuses_concurrent_source_change(tmp_path: Path):
    source = _repository(tmp_path)
    store = TransactionStore(tmp_path / "state")
    manifest = store.create(source, task="change value")
    workspace = store.workspace(manifest["id"])
    (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert store.prepare(manifest["id"], _verified(workspace))["status"] == "prepared"

    (source / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
    with pytest.raises(TransactionError, match="changed since transaction begin"):
        store.commit(manifest["id"])

    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 3\n"
    assert store.load(manifest["id"])["status"] == "prepared"
    store.abort(manifest["id"])


def test_commit_refuses_tampered_prepared_patch_before_writing_source(tmp_path: Path):
    source = _repository(tmp_path)
    store = TransactionStore(tmp_path / "state")
    manifest = store.create(source, task="change value")
    workspace = store.workspace(manifest["id"])
    (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert store.prepare(manifest["id"], _verified(workspace))["status"] == "prepared"
    patch = store.root / manifest["id"] / "prepared.patch"
    patch.write_text("", encoding="utf-8")

    with pytest.raises(TransactionError, match="integrity"):
        store.commit(manifest["id"])

    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    store.abort(manifest["id"])


def test_validated_patch_is_bound_to_receipt_and_rejects_tampering(tmp_path: Path):
    source = _repository(tmp_path)
    store = TransactionStore(tmp_path / "state")
    manifest = store.create(source, task="change value")
    workspace = store.workspace(manifest["id"])
    (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    prepared = store.prepare(manifest["id"], _verified(workspace))

    patch = store.validated_patch(prepared["id"])
    assert b"-VALUE = 1" in patch
    assert b"+VALUE = 2" in patch

    patch_path = store.root / prepared["id"] / "prepared.patch"
    patch_path.write_bytes(patch + b"\n")
    with pytest.raises(TransactionError, match="authenticated state"):
        store.validated_patch(prepared["id"])
    store.abort(prepared["id"])


def test_receipt_authentication_rejects_tampering(tmp_path: Path):
    source = _repository(tmp_path)
    store = TransactionStore(tmp_path / "state")
    manifest = store.create(source, task="change value")
    workspace = store.workspace(manifest["id"])
    (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    prepared = store.prepare(manifest["id"], _verified(workspace))
    receipt_path = store.root / manifest["id"] / "receipt.json"
    envelope = json.loads(receipt_path.read_text(encoding="utf-8"))
    envelope["payload"]["prepared"]["patch_sha256"] = "0" * 64
    receipt_path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(ReceiptError, match="digest|authentication"):
        store.receipt(manifest["id"])
    with pytest.raises(ReceiptError, match="digest|authentication"):
        store.commit(prepared["id"])

    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    store.abort(manifest["id"])


def test_receipt_cli_verifies_and_renders_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    import mini_code_agent.cli as cli_module
    from mini_code_agent import transaction_cli

    source = _repository(tmp_path)
    state = tmp_path / "state"
    store = TransactionStore(state)
    manifest = store.create(source, task="change value")
    workspace = store.workspace(manifest["id"])
    (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    prepared = store.prepare(manifest["id"], _verified(workspace))
    monkeypatch.setenv("MCA_STATE_DIR", str(state))

    result = transaction_cli.state_command(
        cli_module.build_parser().parse_args(["tx", "receipt", prepared["id"]])
    )

    output = capsys.readouterr().out
    assert result == 0
    assert f"receipt: {prepared['receipt_id']}" in output
    assert "verification: passed" in output
    assert "check: tests returncode=0" in output
    store.abort(manifest["id"])


def test_prepare_rejects_stale_verification_and_unrepresentable_change(tmp_path: Path):
    source = _repository(tmp_path)
    store = TransactionStore(tmp_path / "state")
    stale = store.create(source, task="stale")
    stale_workspace = store.workspace(stale["id"])
    (stale_workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    trajectory = _verified(stale_workspace)
    (stale_workspace / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
    result = store.prepare(stale["id"], trajectory)
    assert result["status"] == "open"
    assert "not bound" in result["failure"]
    store.abort(stale["id"])

    ignored = store.create(source, task="ignored")
    ignored_workspace = store.workspace(ignored["id"])
    (ignored_workspace / "ignored.txt").write_text("secret\n", encoding="utf-8")
    result = store.prepare(ignored["id"], _verified(ignored_workspace))
    assert result["status"] == "open"
    assert "cannot represent" in result["failure"]
    store.abort(ignored["id"])


def test_abort_and_restart_recover_durable_open_state(tmp_path: Path):
    source = _repository(tmp_path)
    state = tmp_path / "state"
    first_store = TransactionStore(state)
    manifest = first_store.create(source, task="recover me")
    workspace = first_store.workspace(manifest["id"])
    (workspace / "app.py").write_text("VALUE = 9\n", encoding="utf-8")

    restarted_store = TransactionStore(state)
    recovered = restarted_store.load(manifest["id"])
    assert recovered["status"] == "open"
    assert recovered["task"] == "recover me"
    aborted = restarted_store.abort(manifest["id"])
    assert aborted["status"] == "aborted"
    assert not workspace.exists()
    assert (source / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_transaction_state_must_be_outside_source_workspace(tmp_path: Path):
    source = _repository(tmp_path)
    store = TransactionStore(source / ".mca-state")

    with pytest.raises(TransactionError, match="outside"):
        store.create(source, task="unsafe state placement")
    assert not (source / ".mca-state").exists()


def test_dirty_source_error_suggests_the_diagnostic_command(tmp_path: Path):
    source = _repository(tmp_path)
    store = TransactionStore(tmp_path / "state")
    (source / "untracked.txt").write_text("local artifact\n", encoding="utf-8")

    with pytest.raises(TransactionError, match="git status --short"):
        store.create(source, task="change value")


class _Redactor:
    def redact_text(self, text: str) -> str:
        return text

    def redact_data(self, value: Any) -> Any:
        return value


class _Executor:
    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.redactor = _Redactor()

    def execute_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(
            tool=name,
            output="ok",
            returncode=0,
            duration_ms=1,
            file_path=str(self.cwd / str(args.get("path", "")))
            if args.get("path")
            else "",
        )

    def workspace_fingerprint(self, *, ignore_paths=None):
        return WorkspaceSnapshot.capture(self.cwd, ignore_paths=ignore_paths)

    def sandbox_status(self) -> str:
        return "test"


def test_executor_persists_read_write_sets_and_started_completion_wal(tmp_path: Path):
    source = _repository(tmp_path)
    store = TransactionStore(tmp_path / "state")
    manifest = store.create(source, task="record access")
    executor = TransactionExecutor(
        _Executor(store.workspace(manifest["id"])), store, manifest
    )

    executor.execute_tool("read_file", {"path": "app.py"})
    executor.execute_tool("write_file", {"path": "app.py", "content": "x"})
    executor.execute_tool("search_files", {"pattern": "VALUE", "path": "."})
    executor.execute_tool("run_tests", {})
    recovered = store.load(manifest["id"])

    assert recovered["read_set"] == ["app.py"]
    assert recovered["write_set"] == ["app.py"]
    assert recovered["broad_read"] is True
    assert recovered["broad_write"] is True
    assert [event["phase"] for event in recovered["access_log"]] == [
        "completed",
        "completed",
        "completed",
        "completed",
    ]
    store.abort(manifest["id"])


def test_mock_agent_prepares_and_commits_through_transaction_runtime(tmp_path: Path):
    from mini_code_agent.agent import MiniCodeAgent
    from mini_code_agent.executor import BashExecutor
    from mini_code_agent.model import create_model

    source = _repository(tmp_path)
    (source / "calculator.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a - b\n",
        encoding="utf-8",
    )
    _git(source, "add", "calculator.py")
    _git(source, "commit", "-m", "add calculator")
    store = TransactionStore(tmp_path / "state")
    manifest = store.create(source, task="Fix the calculator")
    workspace = store.workspace(manifest["id"])
    executor = BashExecutor(
        workspace,
        approval_mode="yolo",
        default_test_command=(
            command_from_argv(
                [
                    sys.executable,
                    "-c",
                    "from calculator import add; assert add(2, 3) == 5",
                ]
            )
        ),
        sandbox_mode="none",
    )
    transactional = TransactionExecutor(executor, store, manifest)
    agent = MiniCodeAgent(
        create_model("mock"),
        transactional,
        max_steps=10,
        trajectory_path=store.trajectory(manifest["id"]),
        quiet=True,
    )

    trajectory = agent.run(manifest["task"])
    assert trajectory["exit_status"] == "Submitted", json.dumps(trajectory, indent=2)
    assert "return a - b" in (source / "calculator.py").read_text(encoding="utf-8")
    assert store.prepare(manifest["id"], trajectory)["status"] == "prepared"
    assert store.commit(manifest["id"])["status"] == "committed"
    assert "return a + b" in (source / "calculator.py").read_text(encoding="utf-8")
