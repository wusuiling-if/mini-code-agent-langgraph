"""Evaluate deterministic receipt-to-memory formation without model calls."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from mini_code_agent.checks import verification_command_sha256
from mini_code_agent.memory_admission import (
    MemoryAdmissionError,
    form_committed_transaction_memories,
)
from mini_code_agent.memory_retrieval import (
    EvidenceTemporalRetriever,
    MemoryQuery,
    MemoryScope,
    retrieve_workspace_context,
)
from mini_code_agent.memory_store import SQLiteMemoryStore
from mini_code_agent.transaction import TransactionStore
from mini_code_agent.workspace import WorkspaceSnapshot

SUITE_NAME = "memory-formation-v2"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repository(root: Path) -> Path:
    source = root / "repo"
    source.mkdir(parents=True)
    _git(source, "init")
    _git(source, "config", "user.email", "formation@example.invalid")
    _git(source, "config", "user.name", "Memory Formation")
    (source / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(source, "add", "app.py")
    _git(source, "commit", "-m", "baseline")
    return source


def _trajectory(
    workspace: Path,
    *,
    command: str = "pytest -q",
    command_bound: bool = True,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "name": "tests",
        "returncode": 0,
        "duration_ms": 1,
        "blocked": False,
        "approved": True,
    }
    if command_bound:
        evidence["command_sha256"] = verification_command_sha256(command)
    return {
        "exit_status": "Submitted",
        "verification_status": "passed",
        "verified_fingerprint": WorkspaceSnapshot.capture(workspace).fingerprint,
        "events": [
            {
                "type": "tool",
                "tool": "run_tests",
                "returncode": 0,
                "verification_checks": [evidence],
            }
        ],
    }


def _prepare(
    transactions: TransactionStore,
    source: Path,
    *,
    value: int,
    memory_mode: str,
    command: str = "pytest -q",
    command_bound: bool = True,
) -> dict[str, Any]:
    manifest = transactions.create(
        source, task=f"change value to {value}", memory_mode=memory_mode
    )
    workspace = transactions.workspace(manifest["id"])
    (workspace / "app.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
    prepared = transactions.prepare(
        manifest["id"],
        _trajectory(workspace, command=command, command_bound=command_bound),
    )
    if prepared["status"] != "prepared":
        raise RuntimeError(f"formation fixture did not prepare: {prepared['failure']}")
    return prepared


def _commit_source(source: Path, label: str) -> None:
    _git(source, "add", "app.py")
    _git(source, "commit", "-m", label)


def run_formation() -> dict[str, Any]:
    started = time.monotonic()
    cases: list[dict[str, Any]] = []

    def record(name: str, category: str, passed: bool) -> None:
        cases.append({"name": name, "category": category, "passed": passed})

    with tempfile.TemporaryDirectory(prefix="mca-memory-formation-") as temporary:
        root = Path(temporary)

        off_root = root / "off"
        off_source = _repository(off_root)
        off_state = off_root / "state"
        off_transactions = TransactionStore(off_state)
        off = _prepare(
            off_transactions,
            off_source,
            value=2,
            memory_mode="off",
        )
        off_transactions.commit(off["id"])
        off_cards = form_committed_transaction_memories(off_state, off["id"])
        record(
            "default-off-does-not-initialize",
            "opt_in",
            not off_cards and not (off_state / "memory").exists(),
        )

        live_root = root / "live"
        source = _repository(live_root)
        state = live_root / "state"
        transactions = TransactionStore(state)
        first = _prepare(transactions, source, value=2, memory_mode="local")
        try:
            form_committed_transaction_memories(state, first["id"])
        except MemoryAdmissionError:
            rejected_before_commit = not (state / "memory").exists()
        else:
            rejected_before_commit = False
        record(
            "precommit-formation-is-rejected",
            "lifecycle",
            rejected_before_commit,
        )

        transactions.commit(first["id"])
        first_cards = form_committed_transaction_memories(state, first["id"])
        store = SQLiteMemoryStore(state / "memory")
        first_workflow = next(
            (card for card in first_cards if card.subtype == "verified_workflow"),
            None,
        )
        first_repair = next(
            (card for card in first_cards if card.subtype == "verified_repair"),
            None,
        )
        record(
            "committed-receipt-forms-procedure",
            "formation",
            first_workflow is not None
            and first_workflow.kind == "procedural"
            and first_workflow.authority == "inform"
            and store.verify().ok,
        )
        record(
            "committed-receipt-forms-verified-repair",
            "formation",
            first_repair is not None
            and first_repair.kind == "episodic"
            and first_repair.origin == "trusted_tool"
            and "-VALUE = 1" in first_repair.value
            and "+VALUE = 2" in first_repair.value,
        )

        replayed = form_committed_transaction_memories(state, first["id"])
        record(
            "same-receipt-replay-is-idempotent",
            "idempotency",
            first_workflow is not None
            and first_repair is not None
            and {card.id for card in replayed} == {first_workflow.id, first_repair.id}
            and len(store.list_cards()) == 2
            and len(store.sources(first_workflow.id)) == 1
            and len(store.sources(first_repair.id)) == 1,
        )

        if first_workflow is None or first_repair is None:
            raise RuntimeError("formation fixture produced no memory")
        pack = retrieve_workspace_context(
            state,
            source,
            "change value to 2",
            scope_key=first_repair.scope_key,
        )
        wrong_scope = EvidenceTemporalRetriever(store).retrieve(
            MemoryQuery(
                "run_tests verification workflow",
                scopes=(MemoryScope("workspace", "sha256:" + "0" * 64),),
            )
        )
        record(
            "formed-memory-is-retrievable-in-scope",
            "retrieval",
            pack is not None
            and bool(pack.items)
            and first_repair.id in {item.card_id for item in pack.items},
        )
        record(
            "formed-memory-does-not-cross-scope",
            "scope",
            wrong_scope.decision.kind == "no_memory" and not wrong_scope.items,
        )

        _commit_source(source, "first transaction")
        second = _prepare(transactions, source, value=3, memory_mode="local")
        transactions.commit(second["id"])
        second_cards = form_committed_transaction_memories(state, second["id"])
        second_workflow = next(
            card for card in second_cards if card.subtype == "verified_workflow"
        )
        record(
            "same-workflow-merges-cross-commit-evidence",
            "consolidation",
            second_workflow.id == first_workflow.id
            and len(
                [
                    card
                    for card in store.list_cards()
                    if card.subtype == "verified_workflow"
                ]
            )
            == 1
            and len(store.sources(first_workflow.id)) == 2,
        )

        _commit_source(source, "second transaction")
        changed = _prepare(
            transactions,
            source,
            value=4,
            memory_mode="local",
            command="pytest -q && ruff check .",
        )
        transactions.commit(changed["id"])
        changed_cards = form_committed_transaction_memories(state, changed["id"])
        changed_workflow = next(
            card for card in changed_cards if card.subtype == "verified_workflow"
        )
        record(
            "changed-command-fingerprint-creates-revision-candidate",
            "change_detection",
            changed_workflow.id != first_workflow.id
            and store.get_card(first_workflow.id).status == "superseded"
            and len(
                [
                    card
                    for card in store.list_cards()
                    if card.subtype == "verified_workflow"
                ]
            )
            == 1
            and len(
                [
                    card
                    for card in store.list_cards(include_inactive=True)
                    if card.subtype == "verified_workflow"
                ]
            )
            == 2,
        )

        unbound_root = root / "unbound"
        unbound_source = _repository(unbound_root)
        unbound_state = unbound_root / "state"
        unbound_transactions = TransactionStore(unbound_state)
        unbound = _prepare(
            unbound_transactions,
            unbound_source,
            value=2,
            memory_mode="local",
            command_bound=False,
        )
        unbound_transactions.commit(unbound["id"])
        try:
            form_committed_transaction_memories(unbound_state, unbound["id"])
        except MemoryAdmissionError:
            unbound_rejected = not (unbound_state / "memory").exists()
        else:
            unbound_rejected = False
        record(
            "unbound-verification-command-is-rejected",
            "provenance",
            unbound_rejected,
        )

        passed = sum(int(case["passed"]) for case in cases)
        return {
            "suite": SUITE_NAME,
            "scope": {
                "offline": True,
                "deterministic": True,
                "model_calls": 0,
                "free_text_extraction": False,
            },
            "aggregate": {
                "cases": len(cases),
                "passed": passed,
                "pass_rate": round(passed / len(cases), 4),
            },
            "metrics": {
                "duplicate_card_rate_same_workflow": (
                    0.0
                    if len(
                        [
                            card
                            for card in store.list_cards()
                            if card.subtype == "verified_workflow"
                        ]
                    )
                    == 1
                    else 1.0
                ),
                "evidence_sources_on_stable_workflow": len(
                    store.sources(first_workflow.id)
                ),
                "store_integrity": store.verify().ok,
            },
            "cases": cases,
            "acceptance": {"passed": passed == len(cases)},
            "elapsed_seconds": round(time.monotonic() - started, 4),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run_formation()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"suite: {report['suite']}")
        print(f"cases: {report['aggregate']['passed']}/{report['aggregate']['cases']}")
        for case in report["cases"]:
            print(f"{'PASS' if case['passed'] else 'FAIL'} {case['name']}")
    return 0 if report["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
