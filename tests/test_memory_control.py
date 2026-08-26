from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from mini_code_agent.memory_control import (
    EvidenceGroundedMemoryController,
    MemoryControlContext,
)
from mini_code_agent.memory_models import EvidenceSource
from mini_code_agent.memory_retrieval import MemoryQuery, MemoryScope
from mini_code_agent.memory_store import SQLiteMemoryStore


def _source(label: str) -> EvidenceSource:
    return EvidenceSource(
        source_type="test_fixture",
        source_ref=f"test:{label}",
        source_sha256=hashlib.sha256(label.encode()).hexdigest(),
        origin="trusted_tool",
    )


def _card(
    store: SQLiteMemoryStore,
    label: str,
    *,
    subtype: str = "workflow",
    value: str | None = None,
):
    return store.add_card(
        value=value or f"Use the {label} verification procedure.",
        abstraction=f"{label} verification procedure",
        cue_anchors=("verification procedure", label),
        kind="procedural",
        subtype=subtype,
        scope="workspace",
        scope_key="workspace-a",
        origin="agent",
        authority="inform",
        confidence=0.9,
        importance=0.8,
        valid_from="2026-01-01T00:00:00Z",
        sources=(_source(label),),
    )


def _query() -> MemoryQuery:
    return MemoryQuery(
        "verification procedure",
        scopes=(MemoryScope("workspace", "workspace-a"),),
        as_of="2026-08-18T00:00:00Z",
    )


def test_controller_noops_or_requeries_when_retrieval_has_no_candidate(
    tmp_path: Path,
):
    store = SQLiteMemoryStore(tmp_path / "memory")
    _card(store, "unrelated")
    controller = EvidenceGroundedMemoryController(store)
    query = MemoryQuery(
        "Mars weather",
        scopes=(MemoryScope("workspace", "workspace-a"),),
    )

    ordinary = controller.decide(MemoryControlContext(query=query))
    stuck = controller.decide(
        MemoryControlContext(query=query, stage="stuck", recent_failures=2)
    )

    assert ordinary.operation == "no_memory"
    assert stuck.operation == "requery"
    assert ordinary.render() == ""
    assert stuck.render() == ""


def test_outcome_feedback_promotes_helpful_memory_and_suppresses_harmful_memory(
    tmp_path: Path,
):
    store = SQLiteMemoryStore(tmp_path / "memory")
    helpful = _card(store, "helpful")
    harmful = _card(store, "harmful")
    controller = EvidenceGroundedMemoryController(store)

    for index in range(3):
        good_decision = store.record_memory_decision(
            query_sha256=hashlib.sha256(f"good-{index}".encode()).hexdigest(),
            stage="working",
            operation="retrieve",
            selected_card_ids=(helpful.id,),
            expected_utility=0.8,
            reason="fixture",
            shadow=False,
        )
        store.record_memory_outcome(
            good_decision.id,
            success=True,
            reward=1.0,
            harmful=False,
            token_cost=20,
            evidence=_source(f"good-outcome-{index}"),
        )
        bad_decision = store.record_memory_decision(
            query_sha256=hashlib.sha256(f"bad-{index}".encode()).hexdigest(),
            stage="working",
            operation="retrieve",
            selected_card_ids=(harmful.id,),
            expected_utility=0.8,
            reason="fixture",
            shadow=False,
        )
        store.record_memory_outcome(
            bad_decision.id,
            success=False,
            reward=-1.0,
            harmful=True,
            token_cost=20,
            evidence=_source(f"bad-outcome-{index}"),
        )

    decision = controller.decide(MemoryControlContext(query=_query()))

    assert decision.operation == "retrieve"
    assert [item.card_id for item in decision.items] == [helpful.id]
    assert decision.items[0].expected_utility > 0.6
    stats = {item.card_id: item for item in store.memory_utility_stats()}
    assert stats[helpful.id].successes == 3
    assert stats[harmful.id].harmful_uses == 3
    assert store.verify().ok is True


def test_contraindication_is_rendered_as_warning_not_normal_advice(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "memory")
    _card(store, "normal")
    contraindication = _card(
        store,
        "unsafe",
        subtype="contraindication",
        value="Do not rely on the fast check when lockfiles change.",
    )
    controller = EvidenceGroundedMemoryController(store)

    decision = controller.decide(
        MemoryControlContext(
            query=_query(),
            stage="stuck",
            recent_failures=2,
        )
    )

    assert decision.operation == "retrieve_with_warning"
    by_id = {item.card_id: item for item in decision.items}
    assert by_id[contraindication.id].role == "contraindication"
    rendered = decision.render()
    assert "role: contraindication" in rendered
    assert "Do not rely on the fast check" in rendered


def test_shadow_decision_is_authenticated_but_never_injected(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "memory")
    card = _card(store, "shadow")
    controller = EvidenceGroundedMemoryController(store)

    decision = controller.decide(
        MemoryControlContext(query=_query(), shadow=True, record=True)
    )

    assert decision.operation == "retrieve"
    assert decision.shadow is True
    assert decision.render() == ""
    assert decision.decision_id
    records = store.memory_decisions()
    assert len(records) == 1
    assert records[0].selected_card_ids == (card.id,)
    assert records[0].shadow is True
    assert store.verify().ok is True


def test_shadow_outcome_does_not_train_active_policy(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "memory")
    card = _card(store, "shadow-learning")
    controller = EvidenceGroundedMemoryController(store)
    decision = controller.decide(
        MemoryControlContext(query=_query(), shadow=True, record=True)
    )

    controller.record_outcome(
        decision.decision_id,
        success=True,
        reward=1.0,
        token_cost=10,
        evidence=_source("shadow-outcome"),
    )

    stats = store.memory_utility_stats((card.id,))[0]
    assert stats.uses == 0
    assert stats.successes == 0


def test_feedback_tampering_is_detected(tmp_path: Path):
    directory = tmp_path / "memory"
    store = SQLiteMemoryStore(directory)
    card = _card(store, "tamper")
    decision = store.record_memory_decision(
        query_sha256="a" * 64,
        stage="working",
        operation="retrieve",
        selected_card_ids=(card.id,),
        expected_utility=0.7,
        reason="fixture",
        shadow=False,
    )
    store.record_memory_outcome(
        decision.id,
        success=True,
        reward=1.0,
        harmful=False,
        token_cost=10,
        evidence=_source("tamper-outcome"),
    )
    with sqlite3.connect(directory / "memory.sqlite3") as connection:
        connection.execute(
            "UPDATE memory_outcomes SET reward = -1 WHERE decision_id = ?",
            (decision.id,),
        )

    verification = SQLiteMemoryStore(directory, read_only=True).verify()

    assert verification.ok is False
    assert any("outcome authentication failed" in error for error in verification.errors)


def test_feedback_rejects_untrusted_self_report(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "memory")
    card = _card(store, "untrusted-feedback")
    decision = store.record_memory_decision(
        query_sha256="b" * 64,
        stage="working",
        operation="retrieve",
        selected_card_ids=(card.id,),
        expected_utility=0.7,
        reason="fixture",
        shadow=False,
    )

    with pytest.raises(ValueError, match="trusted runtime or user evidence"):
        store.record_memory_outcome(
            decision.id,
            success=True,
            reward=1.0,
            harmful=False,
            token_cost=10,
            evidence=EvidenceSource(
                source_type="agent_claim",
                source_ref="self-reported-success",
                source_sha256="c" * 64,
                origin="agent",
            ),
        )


def test_v1_store_remains_readable_and_upgrades_on_first_control_write(
    tmp_path: Path,
):
    directory = tmp_path / "memory"
    store = SQLiteMemoryStore(directory)
    card = _card(store, "legacy")
    with store._connect(write=True) as connection:
        connection.execute("DROP TABLE memory_outcomes")
        connection.execute("DROP TABLE memory_decisions")
        store._set_meta(connection, "schema_version", "1")

    legacy = SQLiteMemoryStore(directory, read_only=True)
    assert legacy.verify().ok is True
    assert legacy.memory_utility_stats((card.id,))[0].uses == 0
    decision = EvidenceGroundedMemoryController(legacy).decide(
        MemoryControlContext(query=_query())
    )
    assert decision.operation == "retrieve"

    upgraded = SQLiteMemoryStore(directory)
    upgraded.record_memory_decision(
        query_sha256="d" * 64,
        stage="working",
        operation="retrieve",
        selected_card_ids=(card.id,),
        expected_utility=0.7,
        reason="migration-test",
        shadow=False,
    )
    assert upgraded.status().schema_version == 2
    assert upgraded.verify().ok is True
