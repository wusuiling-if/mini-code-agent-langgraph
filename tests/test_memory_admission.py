from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from memory_core.lifecycle import CapacityPolicy
from mini_code_agent import cli as cli_module
from mini_code_agent import transaction_cli
from mini_code_agent.memory_admission import (
    MemoryAdmissionError,
    MemoryAdmissionService,
    ProceduralMemoryCandidate,
    form_committed_transaction_memories,
)
from mini_code_agent.memory_store import SQLiteMemoryStore
from mini_code_agent.transaction import TransactionStore
from mini_code_agent.workspace import WorkspaceSnapshot


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _prepared_transaction(
    tmp_path: Path,
    *,
    memory_mode: str = "off",
    command_bound: bool = True,
    task: str = "change value",
) -> tuple[Path, TransactionStore, dict[str, Any]]:
    source = tmp_path / "repo"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "memory@example.invalid")
    _git(source, "config", "user.name", "Memory Test")
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(source, "add", "app.py")
    _git(source, "commit", "-m", "baseline")

    state_root = tmp_path / "state"
    transactions = TransactionStore(state_root)
    manifest = transactions.create(source, task=task, memory_mode=memory_mode)
    workspace = transactions.workspace(manifest["id"])
    (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    fingerprint = WorkspaceSnapshot.capture(workspace).fingerprint
    check_evidence = {
        "name": "tests",
        "returncode": 0,
        "duration_ms": 1,
        "blocked": False,
        "approved": True,
    }
    if command_bound:
        check_evidence["command_sha256"] = hashlib.sha256(b"pytest -q").hexdigest()
    trajectory = {
        "exit_status": "Submitted",
        "verification_status": "passed",
        "verified_fingerprint": fingerprint,
        "events": [
            {
                "type": "tool",
                "tool": "run_tests",
                "returncode": 0,
                "verification_checks": [check_evidence],
            }
        ],
    }
    prepared = transactions.prepare(manifest["id"], trajectory)
    assert prepared["status"] == "prepared"
    return state_root, transactions, prepared


def _prepare_next_transaction(
    transactions: TransactionStore,
    source: Path,
    *,
    value: int,
    command: str = "pytest -q",
) -> dict[str, Any]:
    manifest = transactions.create(
        source, task=f"change value to {value}", memory_mode="local"
    )
    workspace = transactions.workspace(manifest["id"])
    (workspace / "app.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
    fingerprint = WorkspaceSnapshot.capture(workspace).fingerprint
    trajectory = {
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
                        "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
                    }
                ],
            }
        ],
    }
    prepared = transactions.prepare(manifest["id"], trajectory)
    assert prepared["status"] == "prepared"
    return prepared


def _candidate() -> ProceduralMemoryCandidate:
    return ProceduralMemoryCandidate(
        value="Run the verified test command before submitting changes.",
        abstraction="The workspace uses its verified test command before submission.",
        cue_anchors=("verification", "test command"),
        confidence=0.9,
        importance=0.8,
    )


def test_candidate_schema_has_no_caller_controlled_trust_fields():
    candidate_fields = {field.name for field in fields(ProceduralMemoryCandidate)}

    assert candidate_fields.isdisjoint(
        {"origin", "authority", "scope", "scope_key", "kind", "sources"}
    )


def test_admission_derives_trust_scope_and_evidence_from_receipt(tmp_path: Path):
    state_root, transactions, manifest = _prepared_transaction(tmp_path)
    service = MemoryAdmissionService(state_root)

    card = service.admit_verified_procedure(manifest["id"], _candidate())
    receipt = transactions.validated_receipt(manifest["id"])
    source = service.store.sources(card.id)[0]

    assert card.kind == "procedural"
    assert card.origin == "agent"
    assert card.authority == "inform"
    assert card.scope == "workspace"
    assert card.scope_key == (
        "sha256:" + receipt["payload"]["workspace"]["identity_sha256"]
    )
    assert source.source_type == "transaction_receipt"
    assert source.origin == "trusted_tool"
    assert source.source_sha256 == receipt["receipt_id"]
    assert manifest["id"] in source.source_ref
    assert service.store.verify().ok is True


def test_deterministic_workflow_extraction_uses_only_bound_check_names(
    tmp_path: Path,
):
    state_root, _transactions, manifest = _prepared_transaction(tmp_path)
    service = MemoryAdmissionService(state_root)

    card = service.admit_verification_workflow(manifest["id"])

    assert card.subtype == "verified_workflow"
    assert "tests" in card.value
    assert "run_tests" in card.cue_anchors
    assert "pytest -q" not in card.value


def test_workflow_formation_is_idempotent_for_one_receipt(tmp_path: Path):
    state_root, _transactions, manifest = _prepared_transaction(tmp_path)
    service = MemoryAdmissionService(state_root)

    first = service.admit_verification_workflow(manifest["id"])
    second = service.admit_verification_workflow(manifest["id"])

    assert first.id == second.id
    assert len(service.store.list_cards()) == 1
    assert len(service.store.sources(first.id)) == 1


def test_concurrent_workflow_formation_is_idempotent(tmp_path: Path):
    state_root, _transactions, manifest = _prepared_transaction(tmp_path)
    workers = 8
    barrier = threading.Barrier(workers)

    def form_once(_index: int):
        barrier.wait()
        return MemoryAdmissionService(state_root).admit_verification_workflow(
            manifest["id"]
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        cards = list(pool.map(form_once, range(workers)))

    store = SQLiteMemoryStore(state_root / "memory")
    assert len({card.id for card in cards}) == 1
    assert len(store.list_cards()) == 1
    assert len(store.sources(cards[0].id)) == 1
    assert store.verify().ok is True


def test_same_verified_workflow_merges_evidence_across_commits(tmp_path: Path):
    state_root, transactions, first_manifest = _prepared_transaction(
        tmp_path, memory_mode="local"
    )
    service = MemoryAdmissionService(state_root)
    transactions.commit(first_manifest["id"])
    first_card = service.admit_verification_workflow(first_manifest["id"])
    source = Path(first_manifest["source"])
    _git(source, "add", "app.py")
    _git(source, "commit", "-m", "first transaction")

    second_manifest = _prepare_next_transaction(transactions, source, value=3)
    transactions.commit(second_manifest["id"])
    second_card = service.admit_verification_workflow(second_manifest["id"])

    assert first_card.id == second_card.id
    assert len(service.store.list_cards()) == 1
    assert len(service.store.sources(first_card.id)) == 2


def test_changed_verified_workflow_supersedes_previous_revision(tmp_path: Path):
    state_root, transactions, first_manifest = _prepared_transaction(
        tmp_path, memory_mode="local"
    )
    service = MemoryAdmissionService(state_root)
    transactions.commit(first_manifest["id"])
    first_card = service.admit_verification_workflow(first_manifest["id"])
    source = Path(first_manifest["source"])
    _git(source, "add", "app.py")
    _git(source, "commit", "-m", "first transaction")

    second_manifest = _prepare_next_transaction(
        transactions,
        source,
        value=3,
        command="pytest -q && ruff check .",
    )
    transactions.commit(second_manifest["id"])
    second_card = service.admit_verification_workflow(second_manifest["id"])

    assert second_card.id != first_card.id
    assert service.store.get_card(first_card.id).status == "superseded"
    assert service.store.get_card(second_card.id).status == "active"
    assert [card.id for card in service.store.list_cards()] == [second_card.id]
    assert len(service.store.list_cards(include_inactive=True)) == 2
    assert any(
        edge.relation == "supersedes"
        and edge.source_id == second_card.id
        and edge.target_id == first_card.id
        for edge in service.store.relations(second_card.id)
    )
    assert service.store.verify().ok is True


def test_late_replay_of_older_workflow_does_not_replace_newer_revision(
    tmp_path: Path,
):
    state_root, transactions, first_manifest = _prepared_transaction(
        tmp_path, memory_mode="local"
    )
    transactions.commit(first_manifest["id"])
    source = Path(first_manifest["source"])
    _git(source, "add", "app.py")
    _git(source, "commit", "-m", "first transaction")

    second_manifest = _prepare_next_transaction(
        transactions,
        source,
        value=3,
        command="pytest -q && ruff check .",
    )
    transactions.commit(second_manifest["id"])
    service = MemoryAdmissionService(state_root)
    newer = service.admit_verification_workflow(second_manifest["id"])
    delayed_older = service.admit_verification_workflow(first_manifest["id"])

    assert service.store.get_card(newer.id).status == "active"
    assert service.store.get_card(delayed_older.id).status == "superseded"
    assert [card.id for card in service.store.list_cards()] == [newer.id]
    assert len(service.store.list_cards(include_inactive=True)) == 2
    assert service.store.verify().ok is True


def test_deterministic_extraction_rejects_legacy_unbound_commands(tmp_path: Path):
    state_root, _transactions, manifest = _prepared_transaction(
        tmp_path, command_bound=False
    )
    service = MemoryAdmissionService(state_root)

    with pytest.raises(MemoryAdmissionError, match="command-bound"):
        service.admit_verification_workflow(manifest["id"])

    assert service.store.initialized is False


def test_opt_in_commit_forms_verified_memory_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    state_root, _transactions, manifest = _prepared_transaction(
        tmp_path, memory_mode="local"
    )
    monkeypatch.setenv("MCA_STATE_DIR", str(state_root))

    result = transaction_cli.state_command(
        cli_module.build_parser().parse_args(["tx", "commit", manifest["id"]])
    )

    assert result == 0
    assert "memory:" in capsys.readouterr().out
    store = SQLiteMemoryStore(state_root / "memory", read_only=True)
    cards = store.list_cards()
    assert {card.subtype for card in cards} == {
        "verified_workflow",
        "verified_repair",
    }
    repair = next(card for card in cards if card.subtype == "verified_repair")
    assert "Task: change value" in repair.value
    assert "-VALUE = 1" in repair.value
    assert "+VALUE = 2" in repair.value
    assert store.verify().ok is True


def test_verified_repair_replay_is_idempotent_and_has_automatic_cues(
    tmp_path: Path,
):
    state_root, transactions, manifest = _prepared_transaction(
        tmp_path, memory_mode="local"
    )
    transactions.commit(manifest["id"])
    service = MemoryAdmissionService(state_root)

    first = service.admit_verified_repair(manifest["id"])
    second = service.admit_verified_repair(manifest["id"])

    assert first is not None
    assert second is not None
    assert first.id == second.id
    assert {"change", "value"}.issubset(first.cue_anchors)
    assert len(service.store.list_cards()) == 1
    assert service.store.verify().ok is True


def test_verified_repair_generates_cjk_cues(tmp_path: Path):
    state_root, transactions, manifest = _prepared_transaction(
        tmp_path, memory_mode="local", task="修复用户登录超时问题"
    )
    transactions.commit(manifest["id"])

    card = MemoryAdmissionService(state_root).admit_verified_repair(manifest["id"])

    assert card is not None
    assert "登录" in card.cue_anchors
    assert "超时" in card.cue_anchors


def test_verified_repair_skips_secret_in_task_or_patch(tmp_path: Path):
    assert MemoryAdmissionService._contains_likely_secret(
        '+access_token="abcdefghijklmnop123456"'
    )
    assert not MemoryAdmissionService._contains_likely_secret(
        '+API_KEY = os.environ["API_KEY"]'
    )
    state_root, transactions, manifest = _prepared_transaction(
        tmp_path,
        memory_mode="local",
        task="set api_key=sk-proj-abcdefghijklmnop",
    )
    transactions.commit(manifest["id"])

    cards = form_committed_transaction_memories(state_root, manifest["id"])

    assert [card.subtype for card in cards] == ["verified_workflow"]
    assert all("sk-proj" not in card.value for card in cards)


def test_verified_repair_capacity_marks_oldest_stale(tmp_path: Path):
    state_root, transactions, first = _prepared_transaction(
        tmp_path, memory_mode="local"
    )
    service = MemoryAdmissionService(
        state_root,
        capacity_policy=CapacityPolicy(
            max_active_records_per_scope=2,
            max_active_chars_per_scope=100_000,
        ),
    )
    transactions.commit(first["id"])
    first_card = service.admit_verified_repair(first["id"])
    source = Path(first["source"])
    _git(source, "add", "app.py")
    _git(source, "commit", "-m", "first")

    second = _prepare_next_transaction(transactions, source, value=3)
    transactions.commit(second["id"])
    second_card = service.admit_verified_repair(second["id"])
    _git(source, "add", "app.py")
    _git(source, "commit", "-m", "second")

    third = _prepare_next_transaction(transactions, source, value=4)
    transactions.commit(third["id"])
    third_card = service.admit_verified_repair(third["id"])

    assert first_card is not None
    assert second_card is not None
    assert third_card is not None
    assert service.store.get_card(first_card.id).status == "stale"
    assert {card.id for card in service.store.list_cards()} == {
        second_card.id,
        third_card.id,
    }
    assert len(service.store.list_cards(include_inactive=True)) == 3
    assert service.store.verify().ok is True


def test_post_commit_memory_failure_does_not_misreport_transaction_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    state_root, transactions, manifest = _prepared_transaction(
        tmp_path, memory_mode="local"
    )
    monkeypatch.setenv("MCA_STATE_DIR", str(state_root))

    def fail_admission(*_args, **_kwargs):
        raise MemoryAdmissionError("simulated indexing failure")

    monkeypatch.setattr(
        MemoryAdmissionService, "admit_verification_workflow", fail_admission
    )

    result = transaction_cli.state_command(
        cli_module.build_parser().parse_args(["tx", "commit", manifest["id"]])
    )

    assert result == 0
    assert transactions.load(manifest["id"])["status"] == "committed"
    assert "memory: skipped (MemoryAdmissionError)" in capsys.readouterr().out


def test_admission_rejects_receipt_tampering_before_initializing_memory(
    tmp_path: Path,
):
    state_root, transactions, manifest = _prepared_transaction(tmp_path)
    receipt_path = transactions.root / manifest["id"] / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["payload"]["verification"]["status"] = "failed"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    service = MemoryAdmissionService(state_root)

    with pytest.raises(MemoryAdmissionError, match="digest|authentication"):
        service.admit_verified_procedure(manifest["id"], _candidate())

    assert service.store.initialized is False


def test_admission_rejects_receipt_repaired_around_a_changed_trajectory(
    tmp_path: Path,
):
    state_root, transactions, manifest = _prepared_transaction(tmp_path)
    trajectory_path = transactions.trajectory(manifest["id"])
    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    trajectory["verification_status"] = "failed"
    trajectory_path.write_text(json.dumps(trajectory), encoding="utf-8")
    service = MemoryAdmissionService(state_root)

    with pytest.raises(MemoryAdmissionError, match="does not match durable state"):
        service.admit_verified_procedure(manifest["id"], _candidate())

    assert service.store.initialized is False


def test_admission_rejects_aborted_transaction(tmp_path: Path):
    state_root, transactions, manifest = _prepared_transaction(tmp_path)
    transactions.abort(manifest["id"])
    service = MemoryAdmissionService(state_root)

    with pytest.raises(MemoryAdmissionError, match="not admissible in aborted state"):
        service.admit_verified_procedure(manifest["id"], _candidate())

    assert service.store.initialized is False


def test_transaction_run_injects_original_retrieval_pack_only_when_opted_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    source = tmp_path / "repo"
    source.mkdir()
    _git(source, "init")
    _git(source, "config", "user.email", "memory@example.invalid")
    _git(source, "config", "user.name", "Memory Test")
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(source, "add", "app.py")
    _git(source, "commit", "-m", "baseline")
    state_root = tmp_path / "state"
    observed: dict[str, Any] = {}

    class FakeRedactor:
        @staticmethod
        def redact_text(value: str) -> str:
            return value

    class FakeExecutor:
        def __init__(self, cwd: Path, **_kwargs: Any):
            self.cwd = Path(cwd)
            self.redactor = FakeRedactor()

    class FakePack:
        decision = SimpleNamespace(kind="use_memory", reason="selected")
        query = SimpleNamespace(text="change value")
        items = tuple(
            SimpleNamespace(
                content_sha256=hashlib.sha256(str(index).encode()).hexdigest(),
                value=f"ORIGINAL_RETRIEVER_CONTEXT_{index}",
                scope="workspace",
                scope_key="sha256:test",
                authority="inform",
                evidence_refs=(f"test:{index}",),
                score=1.0,
            )
            for index in range(2)
        )

    class FakeAgent:
        def __init__(self, _model: Any, executor: Any, **_kwargs: Any):
            self.executor = executor

        def run(
            self,
            task: str,
            *,
            resume_data: dict[str, Any] | None = None,
            advisory_context: str = "",
        ) -> dict[str, Any]:
            assert resume_data is None
            observed["task"] = task
            observed["advisory_context"] = advisory_context
            (self.executor.cwd / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
            fingerprint = WorkspaceSnapshot.capture(self.executor.cwd).fingerprint
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
                                "command_sha256": hashlib.sha256(
                                    b"pytest -q"
                                ).hexdigest(),
                            }
                        ],
                    }
                ],
            }

    def fake_retrieve(
        state: Path,
        workspace: Path,
        task: str,
        *,
        scope_key: str | None = None,
        semantic_provider: Any = None,
    ):
        assert semantic_provider is None
        observed["retrieval"] = (state, workspace, task, scope_key)
        return FakePack()

    from mini_code_agent import memory_retrieval

    monkeypatch.setenv("MCA_STATE_DIR", str(state_root))
    monkeypatch.setattr(cli_module, "_load_runtime_env", lambda _path: None)
    monkeypatch.setattr(cli_module, "_load_bash_executor", lambda: FakeExecutor)
    monkeypatch.setattr(cli_module, "_load_mini_code_agent", lambda: FakeAgent)
    monkeypatch.setattr(cli_module, "_model_from_args", lambda _args: object())
    monkeypatch.setattr(cli_module, "_require_working_sandbox", lambda _executor: None)
    monkeypatch.setattr(memory_retrieval, "retrieve_workspace_context", fake_retrieve)
    args = cli_module.build_parser().parse_args(
        [
            "tx",
            "run",
            "change value",
            "--cwd",
            str(source),
            "--model",
            "deepseek",
            "--memory",
            "local",
            "--test-command",
            "pytest -q",
            "--yes",
            "--sandbox",
            "none",
        ]
    )

    result = transaction_cli.agent_command(args, resume=False)

    assert result == 0
    assert observed["task"] == "change value"
    assert "ORIGINAL_RETRIEVER_CONTEXT_0" in observed["advisory_context"]
    assert len(observed["advisory_context"]) <= 16_000
    assert observed["retrieval"][:3] == (
        state_root,
        source.resolve(),
        "change value",
    )
    assert str(observed["retrieval"][3]).startswith("sha256:")
    assert "memory_retrieved: 2" in capsys.readouterr().out
    transaction_id = next(
        path.name for path in (state_root / "transactions").iterdir() if path.is_dir()
    )
    trajectory = json.loads(
        TransactionStore(state_root)
        .trajectory(transaction_id)
        .read_text(encoding="utf-8")
    )
    assert trajectory["memory"]["retrieval"]["decision"] == "use_memory"
    assert trajectory["memory"]["retrieval"]["context_chars"] > 0
    assert "ORIGINAL_RETRIEVER_CONTEXT" not in json.dumps(
        trajectory["memory"]["retrieval"]
    )


def test_transaction_failure_prefers_redacted_error_and_renders_resume_command():
    args = cli_module.build_parser().parse_args(
        [
            "tx",
            "run",
            "change value",
            "--model",
            "deepseek-flash",
            "--provider",
            "deepseek",
            "--memory",
            "local",
            "--test-command",
            "python3 -m unittest -v",
            "--sandbox",
            "none",
            "--yes",
        ]
    )
    manifest = {
        "id": "abc123",
        "status": "open",
        "failure": "agent did not submit",
    }
    trajectory = {
        "exit_status": "Error:APIConnectionError",
        "error": "APIConnectionError: Connection error.",
        "resumable": True,
    }

    assert (
        transaction_cli._failure_message(manifest, trajectory)
        == "APIConnectionError: Connection error."
    )
    command = transaction_cli._resume_command(args, "abc123", trajectory)
    assert command.startswith(
        "mca tx resume abc123 --model deepseek-flash --provider deepseek"
    )
    assert "--memory local" in command
    assert "--test-command 'python3 -m unittest -v'" in command
    assert "--sandbox none" in command
    assert command.endswith("--yes")
