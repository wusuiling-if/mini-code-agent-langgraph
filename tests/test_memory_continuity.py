from __future__ import annotations

from memory_core.continuity import (
    ContinuityMemory,
    ContinuityRecallPolicy,
    LongTermRetentionPolicy,
    plan_long_term_retention,
    select_continuity_context,
)

DAY_NS = 86_400_000_000_000


def _memory(
    record_id: str,
    retention_class: str,
    *,
    text: str | None = None,
    age_days: int = 0,
    anchor: str = "",
    pinned: bool = False,
    source_valid: bool = True,
    importance: float = 0.5,
    use_count: int = 0,
) -> ContinuityMemory:
    now = 1_000 * DAY_NS
    return ContinuityMemory(
        record_id=record_id,
        text=text or f"memory {record_id}",
        retention_class=retention_class,
        recorded_at_ns=now - age_days * DAY_NS,
        last_used_at_ns=now - age_days * DAY_NS,
        importance=importance,
        confidence=0.9,
        identity_anchor=anchor,
        pinned=pinned,
        source_valid=source_valid,
        use_count=use_count,
        authority="inform",
        evidence_refs=(f"chat:{record_id}",),
    )


def test_capacity_pressure_retires_noise_without_forgetting_core_identity():
    now = 1_000 * DAY_NS
    core = _memory(
        "preferred-name",
        "core",
        text="用户希望被称为 Eddy。",
        age_days=900,
        anchor="user:identity",
    )
    records = (
        core,
        _memory("old-scene", "transient", age_days=20),
        _memory("episode", "episodic", age_days=10),
    )
    incoming = _memory("new-scene", "transient")

    decision = plan_long_term_retention(
        records,
        incoming,
        policy=LongTermRetentionPolicy(max_active_records=3),
        now_ns=now,
    )

    assert decision.admit is True
    assert decision.retire_record_ids == ("old-scene",)
    assert "preferred-name" in decision.protected_record_ids


def test_last_durable_identity_anchor_is_protected_even_when_very_old():
    now = 1_000 * DAY_NS
    records = (
        _memory(
            "relationship-state",
            "durable",
            age_days=999,
            anchor="relationship:alice",
        ),
        _memory("recent-scene", "transient", age_days=1, importance=1.0),
    )

    decision = plan_long_term_retention(
        records,
        _memory("incoming", "episodic"),
        policy=LongTermRetentionPolicy(max_active_records=2),
        now_ns=now,
    )

    assert decision.retire_record_ids == ("recent-scene",)
    assert decision.protected_record_ids == ("relationship-state",)


def test_episodic_memory_requires_checkpoint_before_capacity_retirement():
    now = 1_000 * DAY_NS
    records = (
        _memory("episode-1", "episodic", age_days=100),
        _memory("episode-2", "episodic", age_days=10),
    )

    decision = plan_long_term_retention(
        records,
        _memory("incoming", "episodic"),
        policy=LongTermRetentionPolicy(max_active_records=2),
        now_ns=now,
    )

    assert decision.admit is False
    assert decision.reason == "checkpoint_compaction_required"
    assert decision.compact_record_ids == ("episode-1", "episode-2")
    assert decision.retire_record_ids == ()


def test_store_refuses_admission_instead_of_silently_evicting_protected_memory():
    now = 1_000 * DAY_NS
    records = (
        _memory("identity", "core", anchor="user"),
        _memory("safety", "core", anchor="safety", pinned=True),
    )

    decision = plan_long_term_retention(
        records,
        _memory("incoming", "durable", anchor="relationship"),
        policy=LongTermRetentionPolicy(max_active_records=2),
        now_ns=now,
    )

    assert decision.admit is False
    assert decision.reason == "protected_capacity_exhausted"
    assert decision.retire_record_ids == ()
    assert set(decision.protected_record_ids) == {"identity", "safety"}


def test_invalid_source_is_not_protected_or_recalled_even_for_core_memory():
    now = 1_000 * DAY_NS
    invalid = _memory(
        "edited-away-name",
        "core",
        anchor="user",
        source_valid=False,
    )
    decision = plan_long_term_retention(
        (invalid, _memory("episode", "episodic")),
        _memory("incoming", "episodic"),
        policy=LongTermRetentionPolicy(max_active_records=2),
        now_ns=now,
    )
    selected = select_continuity_context((invalid,))

    assert decision.retire_record_ids == ("edited-away-name",)
    assert selected.items == ()


def test_invalid_source_is_retired_even_when_capacity_is_not_full():
    now = 1_000 * DAY_NS
    invalid = _memory("invalid", "core", source_valid=False)

    decision = plan_long_term_retention(
        (invalid,),
        _memory("incoming", "transient"),
        policy=LongTermRetentionPolicy(max_active_records=100),
        now_ns=now,
    )

    assert decision.admit is True
    assert decision.retire_record_ids == ("invalid",)
    assert decision.projected_records == 1


def test_new_session_recall_reserves_continuity_independent_of_query_relevance():
    records = (
        _memory(
            "name",
            "core",
            text="用户希望被称为 Eddy。",
            anchor="user",
        ),
        _memory(
            "relationship",
            "durable",
            text="Alice 与用户已经建立长期合作关系。",
            anchor="relationship:alice",
        ),
        _memory("weather", "episodic", text="昨天下雨。"),
    )

    selected = select_continuity_context(records)

    assert selected.selected_record_ids == ("name", "relationship")
    assert [item.kind for item in selected.items] == [
        "continuity:core",
        "continuity:durable",
    ]
    assert all(item.evidence_refs for item in selected.items)


def test_core_prompt_overflow_is_reported_instead_of_hidden():
    records = (
        _memory("first", "core", text="a" * 30),
        _memory("second", "core", text="b" * 30),
    )

    selected = select_continuity_context(
        records, policy=ContinuityRecallPolicy(max_items=2, max_chars=35)
    )

    assert len(selected.selected_record_ids) == 1
    assert len(selected.omitted_core_record_ids) == 1
    assert selected.requires_compaction is True


def test_core_identity_survives_one_thousand_capacity_cycles():
    now = 1_000 * DAY_NS
    policy = LongTermRetentionPolicy(max_active_records=32, max_active_chars=10_000)
    records = {
        "identity": _memory(
            "identity",
            "core",
            text="用户希望被称为 Eddy。",
            age_days=900,
            anchor="user",
        )
    }
    for index in range(1_000):
        incoming = _memory(
            f"turn-{index}",
            "transient" if index % 2 else "episodic",
            text=f"turn detail {index}",
        )
        compaction_round = 0
        while True:
            decision = plan_long_term_retention(
                tuple(records.values()), incoming, policy=policy, now_ns=now + index
            )
            if decision.reason != "checkpoint_compaction_required":
                break
            compacted = [records.pop(item) for item in decision.compact_record_ids]
            records[f"checkpoint-{index}-{compaction_round}"] = _memory(
                f"checkpoint-{index}-{compaction_round}",
                "episodic",
                text=f"checkpoint of {len(compacted)} source-bound episodes",
            )
            compaction_round += 1
        assert decision.admit is True
        for record_id in decision.retire_record_ids:
            records.pop(record_id)
        records[incoming.record_id] = incoming

    selected = select_continuity_context(tuple(records.values()))

    assert "identity" in records
    assert "identity" in selected.selected_record_ids
    assert len(records) <= policy.max_active_records
