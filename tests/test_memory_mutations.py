from __future__ import annotations

import hashlib
import json

from memory_core.continuity import select_continuity_context
from memory_core.mutations import MemoryFieldSchema, MemoryMutation, MutationLedger


def _source(index: int = 1) -> tuple[tuple[str, str], ...]:
    return ((f"event-{index}", hashlib.sha256(f"event-{index}".encode()).hexdigest()),)


def _mutation(
    base_revision: int,
    operation: str = "assert",
    value: object | None = "Eddy",
    *,
    source_index: int = 1,
) -> MemoryMutation:
    return MemoryMutation.create(
        stream_id="chat:42",
        base_revision=base_revision,
        operation=operation,
        entity_id="user:current",
        predicate="preferred_name",
        value=value,
        schema_id="identity",
        schema_version=1,
        source_event_revisions=_source(source_index),
        authority="inform",
    )


def test_mutation_ledger_replays_semantic_changes_and_rejects_stale_writers():
    ledger = MutationLedger(
        "chat:42", (MemoryFieldSchema("identity", 1, "preferred_name", "text"),)
    )
    first = _mutation(0)
    stale = _mutation(0, value="Lin", source_index=2)

    committed = ledger.append(first)
    conflict = ledger.append(stale)

    assert committed.outcome == "committed"
    assert all(check.passed for check in committed.checks)
    assert conflict.outcome == "conflict"
    assert json.loads(ledger.state[0].value_json or "null") == "Eddy"


def test_canonical_snapshot_round_trip_continues_from_exact_revision():
    schema = MemoryFieldSchema(
        "identity", 1, "preferred_name", "text", retention_class="core"
    )
    ledger = MutationLedger("chat:42", (schema,))
    first = _mutation(0)
    ledger.append(first)
    snapshot = ledger.compact()

    restored = MutationLedger("chat:42", (schema,), snapshot=snapshot)
    disputed = _mutation(1, "dispute", None, source_index=2)
    report = restored.append(disputed)

    assert restored.revision == 2
    assert report.outcome == "committed"
    assert restored.state[0].status == "disputed"
    assert json.loads(restored.state[0].value_json or "null") == "Eddy"
    assert (
        snapshot.materialize()[("user:current", "preferred_name")].mutation_id
        == first.mutation_id
    )
    assert restored.state[0].retention_class == "core"
    continuity = restored.state[0].to_continuity_memory(
        text="用户希望被称为 Eddy。", importance=1.0
    )
    assert continuity.record_id == "user:current:preferred_name"
    assert continuity.retention_class == "core"
    assert continuity.status == "disputed"


def test_source_revision_check_invalidates_derived_state_after_message_edit():
    schema = MemoryFieldSchema("identity", 1, "preferred_name", "text")
    ledger = MutationLedger("chat:42", (schema,))
    mutation = _mutation(0)
    ledger.append(mutation)

    assert ledger.invalid_source_bindings(dict(_source())) == ()
    assert ledger.invalid_source_bindings(
        {"event-1": hashlib.sha256(b"edited").hexdigest()}
    ) == (mutation.mutation_id,)


def test_schema_type_and_authority_fail_closed_with_reports():
    schema = MemoryFieldSchema(
        "identity", 1, "preferred_name", "text", max_authority="inform"
    )
    wrong_type = MemoryMutation.create(
        stream_id="chat:42",
        base_revision=0,
        operation="assert",
        entity_id="user:current",
        predicate="preferred_name",
        value=42,
        schema_id="identity",
        schema_version=1,
        source_event_revisions=_source(),
        authority="inform",
    )
    excessive_authority = MemoryMutation.create(
        stream_id="chat:42",
        base_revision=0,
        operation="assert",
        entity_id="user:current",
        predicate="preferred_name",
        value="Eddy",
        schema_id="identity",
        schema_version=1,
        source_event_revisions=_source(),
        authority="act",
    )

    type_ledger = MutationLedger("chat:42", (schema,))
    authority_ledger = MutationLedger("chat:42", (schema,))

    type_report = type_ledger.append(wrong_type)
    authority_report = authority_ledger.append(excessive_authority)

    assert type_report.outcome == "rejected"
    assert not type_report.successful
    assert type_report.checks[-1].name == "schema_value_valid"
    assert authority_report.outcome == "rejected"
    assert authority_report.checks[-1].name == "authority_within_schema_limit"
    assert type_ledger.revision == authority_ledger.revision == 0


def test_core_memory_updates_and_explicit_forget_override_retention_protection():
    schema = MemoryFieldSchema(
        "identity", 1, "preferred_name", "text", retention_class="core"
    )
    ledger = MutationLedger("chat:42", (schema,))
    ledger.append(_mutation(0, value="Eddy", source_index=1))
    ledger.append(_mutation(1, value="Lin", source_index=2))

    current_value = json.loads(ledger.state[0].value_json or "null")
    active = ledger.state[0].to_continuity_memory(
        text=f"用户希望被称为 {current_value}。"
    )
    selected = select_continuity_context((active,))

    assert current_value == "Lin"
    assert selected.items[0].text == "用户希望被称为 Lin。"
    assert "Eddy" not in selected.items[0].text

    ledger.append(_mutation(2, "withdraw", None, source_index=3))
    withdrawn = ledger.state[0].to_continuity_memory(text="用户希望被称为 Lin。")

    assert withdrawn.status == "withdrawn"
    assert select_continuity_context((withdrawn,)).items == ()
