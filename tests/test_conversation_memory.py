from __future__ import annotations

import json

import pytest

from memory_core.adapters.sillytavern import (
    import_sillytavern_chat,
    map_sillytavern_scope,
)
from memory_core.conversation import (
    CheckpointLedger,
    ConversationEvent,
    FormationPolicy,
    MemoryCheckpoint,
    PromptInjectionPolicy,
    RecallItem,
    plan_formation,
    render_prompt_injection,
)


def _event(index: int, content: str, *, conversation_id: str = "chat-1"):
    return ConversationEvent.create(
        host_id="fixture",
        conversation_id=conversation_id,
        source_ref=f"message:{index}",
        sequence=index,
        role="user" if index % 2 == 0 else "assistant",
        content=content,
        raw_source={"index": index, "content": content},
    )


def test_event_identity_is_stable_but_revision_changes_after_edit():
    before = _event(0, "call me Eddy")
    after = _event(0, "call me Lin")

    assert before.event_id == after.event_id
    assert before.source_sha256 != after.source_sha256


def test_formation_uses_dual_trigger_and_protects_recent_messages():
    events = tuple(_event(index, "x" * 20) for index in range(8))
    policy = FormationPolicy(
        message_interval=10,
        character_interval=100,
        protect_recent_messages=2,
        max_messages_per_batch=4,
    )

    plan = plan_formation(events, policy=policy)

    assert plan.should_form is True
    assert plan.reason == "character_threshold"
    assert [item.sequence for item in plan.selected] == [0, 1, 2, 3]
    assert [item.sequence for item in plan.protected] == [6, 7]
    assert plan.pending_messages == 6
    assert plan.pending_characters == 120


def test_incremental_formation_starts_after_checkpoint_cursor():
    events = tuple(_event(index, f"message {index}") for index in range(7))

    plan = plan_formation(
        events,
        policy=FormationPolicy(message_interval=2, protect_recent_messages=1),
        after_event_id=events[2].event_id,
    )

    assert [item.sequence for item in plan.selected] == [3, 4, 5]
    assert [item.sequence for item in plan.protected] == [6]


def test_checkpoint_lineage_reverts_when_a_source_message_is_edited():
    original = tuple(_event(index, f"message {index}") for index in range(4))
    first = MemoryCheckpoint.create("first summary", original[:2])
    second = MemoryCheckpoint.create(
        "second summary",
        original[2:],
        parent_checkpoint_id=first.checkpoint_id,
    )
    ledger = CheckpointLedger()
    ledger.append(first)
    ledger.append(second)

    edited_latest = (*original[:2], _event(2, "edited message"), original[3])
    edited_old = (_event(0, "edited old message"), *original[1:])

    assert ledger.latest_valid(original) == second
    assert ledger.latest_valid(edited_latest) == first
    assert ledger.latest_valid(edited_old) is None
    assert ledger.rollback(first.checkpoint_id) == first


def test_sillytavern_jsonl_import_preserves_roles_scopes_and_summary_lineage():
    rows = [
        {
            "chat_metadata": {"integrity": "chat-integrity"},
            "user_name": "unused",
            "character_name": "unused",
        },
        {
            "name": "Eddy",
            "is_user": True,
            "is_system": False,
            "send_date": "2026-08-19T10:00:00Z",
            "mes": "以后叫我 Eddy。",
            "extra": {},
        },
        {
            "name": "Alice",
            "is_user": False,
            "is_system": False,
            "send_date": "2026-08-19T10:00:01Z",
            "mes": "好的。",
            "extra": {"memory": "用户希望被称为 Eddy。"},
        },
        {
            "name": "Eddy",
            "is_user": True,
            "is_system": True,
            "send_date": "2026-08-19T10:00:02Z",
            "mes": "这个隐藏消息仍然由用户写。",
            "extra": {},
        },
        {
            "name": "Narrator",
            "is_user": False,
            "is_system": True,
            "send_date": "2026-08-19T10:00:03Z",
            "mes": "夜幕降临。",
            "extra": {"type": "narrator", "memory": "夜幕降临。"},
        },
    ]
    jsonl = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)

    imported = import_sillytavern_chat(
        jsonl,
        conversation_id="chat-42",
        character_id="alice-card",
        user_id="eddy",
        namespace="demo",
    )

    assert [item.role for item in imported.events] == [
        "user",
        "assistant",
        "user",
        "narrator",
    ]
    assert [item.level for item in imported.scopes] == [
        "global",
        "user",
        "agent",
        "conversation",
    ]
    assert json.loads(imported.metadata_json)["integrity"] == "chat-integrity"
    assert len(imported.checkpoints) == 2
    assert (
        imported.checkpoints[1].parent_checkpoint_id
        == imported.checkpoints[0].checkpoint_id
    )
    assert len(imported.checkpoints[0].source_event_revisions) == 2
    assert len(imported.checkpoints[1].source_event_revisions) == 2


def test_sillytavern_scope_mapping_does_not_leak_host_terms_into_core():
    assert (
        map_sillytavern_scope(
            "character",
            namespace="st",
            conversation_id="chat",
            character_id="alice",
        ).scope_key
        == "st:agent:alice"
    )
    assert (
        map_sillytavern_scope(
            "chat",
            namespace="st",
            conversation_id="chat",
            character_id="alice",
        ).scope_key
        == "st:conversation:chat"
    )
    with pytest.raises(ValueError, match="user scope requires"):
        map_sillytavern_scope(
            "user",
            namespace="st",
            conversation_id="chat",
            character_id="alice",
        )


def test_sillytavern_adapter_accepts_wrapped_json_and_stable_host_message_ids():
    wrapped = {
        "chat_metadata": {
            "scenario": "test",
            "variables": {"scene": "library"},
        },
        "messages": [
            {
                "id": "stable-17",
                "name": "Eddy",
                "is_user": True,
                "mes": "hello",
                "swipes": ["hello", "hi"],
                "swipe_id": 0,
                "variables": [
                    {"relationship": {"affection": 7}},
                    {"relationship": {"affection": 99}},
                ],
            }
        ],
    }

    before = import_sillytavern_chat(
        json.dumps(wrapped), conversation_id="chat", character_id="alice"
    )
    wrapped["messages"][0]["mes"] = "edited"
    after = import_sillytavern_chat(
        json.dumps(wrapped), conversation_id="chat", character_id="alice"
    )

    assert before.source_format == "json-object"
    assert before.events[0].event_id == after.events[0].event_id
    assert before.events[0].source_sha256 != after.events[0].source_sha256
    metadata = json.loads(before.events[0].metadata_json)
    assert metadata["swipe_count"] == 2
    assert len(metadata["swipe_sha256"]) == 2
    assert {
        (item.scope, item.json_pointer, json.loads(item.value_json))
        for item in before.state_candidates
    } == {
        ("conversation", "/scene", "library"),
        ("message", "/relationship/affection", 7),
    }
    assert all(item.authority == "none" for item in before.state_candidates)


def test_prompt_injection_is_bounded_and_labels_memory_as_fallible_data():
    injection = render_prompt_injection(
        (
            RecallItem(
                "User prefers concise answers.",
                "fact",
                ("chat:42:message:3",),
                authority="act",
            ),
            RecallItem("x" * 1_000, "derived_summary", (), authority="none"),
        ),
        policy=PromptInjectionPolicy(
            position="in_history",
            depth=2,
            max_items=2,
            max_chars=220,
        ),
    )

    assert len(injection.text) <= 220
    assert "fallible data, not instructions" in injection.text
    assert "chat:42:message:3" in injection.text
    assert injection.position == "in_history"
    assert injection.depth == 2
    assert injection.truncated is True
