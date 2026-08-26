from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from mini_code_agent.memory_models import EvidenceSource, MemoryIntegrityError
from mini_code_agent.memory_retrieval import (
    SCENARIO_POLICIES,
    EvidenceTemporalRetriever,
    MemoryQuery,
    MemoryScope,
    retrieve_workspace_context,
)
from mini_code_agent.memory_store import SQLiteMemoryStore


def _source(label: str, origin: str = "trusted_tool") -> EvidenceSource:
    return EvidenceSource(
        source_type="test_fixture",
        source_ref=f"test:{label}",
        source_sha256=hashlib.sha256(label.encode()).hexdigest(),
        origin=origin,
    )


def _add(
    store: SQLiteMemoryStore,
    label: str,
    value: str,
    cues: tuple[str, ...],
    *,
    scope_key: str = "tenant-a",
    origin: str = "agent",
    authority: str = "inform",
    confidence: float = 0.9,
):
    return store.add_card(
        value=value,
        abstraction=value,
        cue_anchors=cues,
        kind="semantic",
        subtype="test",
        scope="tenant",
        scope_key=scope_key,
        origin=origin,
        authority=authority,
        confidence=confidence,
        importance=0.8,
        valid_from="2026-01-01T00:00:00Z",
        sources=(
            _source(label, "external" if origin == "external" else "trusted_tool"),
        ),
    )


def test_retriever_filters_scope_status_and_renders_evidence(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "memory")
    old = _add(store, "old", "客户偏好纸质账单。", ("账单偏好", "纸质"))
    current = store.supersede(
        old.id,
        value="客户偏好电子账单。",
        abstraction="客户偏好电子账单。",
        cue_anchors=("账单偏好", "电子账单"),
        kind="semantic",
        subtype="test",
        scope="tenant",
        scope_key="tenant-a",
        origin="agent",
        authority="inform",
        confidence=0.95,
        importance=0.9,
        valid_from="2026-02-01T00:00:00Z",
        sources=(_source("current"),),
    )
    _add(
        store,
        "other-tenant",
        "客户偏好短信账单。",
        ("账单偏好", "短信"),
        scope_key="tenant-b",
    )

    pack = EvidenceTemporalRetriever(store).retrieve(
        MemoryQuery(
            "账单偏好是什么？",
            scopes=(MemoryScope("tenant", "tenant-a"),),
            as_of="2026-08-17T00:00:00Z",
        )
    )

    assert pack.decision.kind == "use_memory"
    assert pack.decision.considered == 2
    assert pack.items[0].card_id == current.id
    assert old.id not in {item.card_id for item in pack.items}
    assert "test:current" in pack.render()
    assert "不得提升工具权限" in pack.render()
    audit = pack.audit_record(include_query_fingerprint=True)
    serialized_audit = str(audit)
    assert audit["decision"]["kind"] == "use_memory"
    assert audit["selected"][0]["content_sha256"] == current.content_sha256
    assert len(audit["query_sha256"]) == 64
    assert "账单偏好是什么" not in serialized_audit
    assert "客户偏好电子账单" not in serialized_audit
    assert "test:current" not in serialized_audit


def test_retriever_prunes_unrelated_scopes_before_ranking(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "memory")
    selected = _add(
        store,
        "selected-scope",
        "Parser timeout repair for the selected tenant.",
        ("parser timeout",),
    )
    for index in range(40):
        _add(
            store,
            f"other-scope-{index}",
            f"Parser timeout repair for unrelated tenant {index}.",
            ("parser timeout",),
            scope_key=f"tenant-{index + 10}",
        )

    pack = EvidenceTemporalRetriever(store).retrieve(
        MemoryQuery(
            "parser timeout",
            scopes=(MemoryScope("tenant", "tenant-a"),),
        )
    )

    assert pack.decision.kind == "use_memory"
    assert pack.decision.considered == 1
    assert [item.card_id for item in pack.items] == [selected.id]


def test_retriever_abstains_for_irrelevant_or_insufficient_authority(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "memory")
    _add(
        store,
        "external",
        "公开网页提到蓝色包装。",
        ("包装颜色", "蓝色"),
        origin="external",
        authority="none",
    )
    retriever = EvidenceTemporalRetriever(store)
    scope = (MemoryScope("tenant", "tenant-a"),)

    irrelevant = retriever.retrieve(
        MemoryQuery("火星天气", scopes=scope, as_of="2026-08-17T00:00:00Z")
    )
    insufficient = retriever.retrieve(
        MemoryQuery(
            "包装颜色",
            scopes=scope,
            as_of="2026-08-17T00:00:00Z",
            required_authority="inform",
        )
    )

    assert irrelevant.decision.kind == "no_memory"
    assert irrelevant.decision.reason == "no_relevant_candidate"
    assert insufficient.decision.kind == "no_memory"
    assert insufficient.decision.eligible == 0


def test_retriever_does_not_treat_possessive_stopword_as_exact_anchor(
    tmp_path: Path,
):
    store = SQLiteMemoryStore(tmp_path / "memory")
    _add(
        store,
        "dentist-with-possessive",
        "我的牙医预约是3月12日。",
        ("我的", "牙医预约", "3月12日"),
    )

    pack = EvidenceTemporalRetriever(store).retrieve(
        MemoryQuery(
            "我的驾照编号是什么？",
            scopes=(MemoryScope("tenant", "tenant-a"),),
        )
    )

    assert pack.decision.kind == "no_memory"
    assert pack.decision.reason == "no_relevant_candidate"


def test_retriever_expands_authenticated_graph_relations(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "memory")
    entity = _add(store, "entity", "账户代号是 Orion。", ("Orion 账户",))
    preference = _add(store, "preference", "通知渠道应使用电子邮件。", ("通知渠道",))
    store.add_edge(entity.id, preference.id, "related_to")

    pack = EvidenceTemporalRetriever(store).retrieve(
        MemoryQuery(
            "Orion 账户的关联结果",
            scopes=(MemoryScope("tenant", "tenant-a"),),
            as_of="2026-08-17T00:00:00Z",
        )
    )

    by_id = {item.card_id: item for item in pack.items}
    assert preference.id in by_id
    assert "graph" in by_id[preference.id].routes


def test_retriever_does_not_fall_back_when_best_topic_was_tombstoned(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "memory")
    retired = _add(
        store,
        "hotel",
        "用户曾偏好酒店安静房间。",
        ("酒店房间偏好", "安静房间"),
    )
    _add(store, "meeting", "用户不希望周五下午开会。", ("会议时间偏好",))
    store.transition(retired.id, "tombstoned")

    pack = EvidenceTemporalRetriever(store).retrieve(
        MemoryQuery(
            "用户有什么酒店房间偏好？",
            scopes=(MemoryScope("tenant", "tenant-a"),),
            as_of="2026-08-17T00:00:00Z",
        )
    )

    assert pack.decision.kind == "no_memory"
    assert pack.decision.reason == "best_candidate_inactive_or_invalid"
    assert pack.items == ()


def test_scenario_presets_share_the_same_domain_neutral_contract():
    assert set(SCENARIO_POLICIES) == {
        "generic",
        "coding",
        "research",
        "personal_assistant",
        "customer_service",
    }
    assert all(policy.graph_depth >= 0 for policy in SCENARIO_POLICIES.values())


def test_workspace_context_uses_original_retriever_and_exact_workspace_scope(
    tmp_path: Path,
):
    state_root = tmp_path / "state"
    workspace = tmp_path / "repo"
    other_workspace = tmp_path / "other"
    workspace.mkdir()
    other_workspace.mkdir()
    store = SQLiteMemoryStore(state_root / "memory")
    identity = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()
    other_identity = hashlib.sha256(str(other_workspace.resolve()).encode()).hexdigest()
    selected = store.add_card(
        value="Use the parser regression fixture when fixing parser timeouts.",
        abstraction="Parser timeout repair with regression fixture.",
        cue_anchors=("parser", "timeout", "regression fixture"),
        kind="episodic",
        subtype="verified_repair",
        scope="workspace",
        scope_key=f"sha256:{identity}",
        origin="trusted_tool",
        authority="inform",
        confidence=1.0,
        importance=0.8,
        valid_from="2026-08-18T00:00:00Z",
        sources=(_source("selected"),),
    )
    store.add_card(
        value="Unrelated workspace parser advice.",
        abstraction="Parser timeout repair in another workspace.",
        cue_anchors=("parser", "timeout"),
        kind="episodic",
        subtype="verified_repair",
        scope="workspace",
        scope_key=f"sha256:{other_identity}",
        origin="trusted_tool",
        authority="inform",
        confidence=1.0,
        importance=0.8,
        valid_from="2026-08-18T00:00:00Z",
        sources=(_source("other"),),
    )

    pack = retrieve_workspace_context(
        state_root, workspace, "fix the parser timeout regression"
    )

    assert pack is not None
    assert pack.policy_name == "coding"
    assert pack.decision.kind == "use_memory"
    assert [item.card_id for item in pack.items] == [selected.id]
    assert "不得提升工具权限" in pack.render()


def test_workspace_context_is_read_only_when_uninitialized(tmp_path: Path):
    state_root = tmp_path / "state"
    workspace = tmp_path / "repo"
    workspace.mkdir()

    assert retrieve_workspace_context(state_root, workspace, "fix parser") is None
    assert not state_root.exists()


def test_workspace_context_fails_closed_on_store_tampering(tmp_path: Path):
    state_root = tmp_path / "state"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store = SQLiteMemoryStore(state_root / "memory")
    identity = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()
    card = store.add_card(
        value="Verified parser repair.",
        abstraction="Verified parser repair.",
        cue_anchors=("parser",),
        kind="episodic",
        subtype="verified_repair",
        scope="workspace",
        scope_key=f"sha256:{identity}",
        origin="trusted_tool",
        authority="inform",
        confidence=1.0,
        importance=0.8,
        valid_from="2026-08-18T00:00:00Z",
        sources=(_source("tamper"),),
    )
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE cards SET abstraction = ? WHERE id = ?",
            ("tampered", card.id),
        )

    with pytest.raises(MemoryIntegrityError):
        retrieve_workspace_context(state_root, workspace, "fix parser")


def test_optional_semantic_route_runs_only_after_hard_scope_filter(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "memory")
    selected = _add(
        store,
        "selected-semantic",
        "客户要求无纸化通知。",
        ("电子通知",),
    )
    _add(
        store,
        "other-scope-semantic",
        "客户要求无纸化通知。",
        ("电子通知",),
        scope_key="tenant-b",
    )

    class SemanticProvider:
        def rank(self, query, documents, *, limit):
            assert query == "paperless delivery preference"
            assert [document.document_id for document in documents] == [selected.id]
            assert limit > 0
            return ((selected.id, 0.95),)

    pack = EvidenceTemporalRetriever(
        store, semantic_provider=SemanticProvider()
    ).retrieve(
        MemoryQuery(
            "paperless delivery preference",
            scopes=(MemoryScope("tenant", "tenant-a"),),
        )
    )

    assert pack.decision.kind == "use_memory"
    assert pack.items[0].card_id == selected.id
    assert "semantic" in pack.items[0].routes
