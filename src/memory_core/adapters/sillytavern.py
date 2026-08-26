"""Lossless boundary adapter for SillyTavern chat JSON/JSONL records."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from memory_core.conversation import (
    CheckpointLedger,
    ConversationEvent,
    MemoryCheckpoint,
    ScopeAddress,
)
from memory_core.security import SecretDetector


@dataclass(frozen=True)
class SillyTavernStateCandidate:
    source_ref: str
    source_sha256: str
    scope: str
    json_pointer: str
    value_json: str
    origin: str = "tavern_helper_variables"
    authority: str = "none"


@dataclass(frozen=True)
class SillyTavernImport:
    conversation_id: str
    metadata_json: str
    events: tuple[ConversationEvent, ...]
    checkpoints: tuple[MemoryCheckpoint, ...]
    scopes: tuple[ScopeAddress, ...]
    source_format: str = "jsonl"
    warnings: tuple[str, ...] = ()
    state_candidates: tuple[SillyTavernStateCandidate, ...] = ()
    secret_values_skipped: int = 0


def map_sillytavern_scope(
    scope: str,
    *,
    namespace: str,
    conversation_id: str,
    character_id: str,
    user_id: str = "",
) -> ScopeAddress:
    """Map ST's Data Bank scopes onto portable scope levels."""

    if scope == "global":
        return ScopeAddress("global", namespace, namespace)
    if scope == "character":
        if not character_id.strip():
            raise ValueError("character scope requires a character id")
        return ScopeAddress("agent", namespace, character_id)
    if scope == "chat":
        return ScopeAddress("conversation", namespace, conversation_id)
    if scope == "user":
        if not user_id.strip():
            raise ValueError("user scope requires a user id")
        return ScopeAddress("user", namespace, user_id)
    raise ValueError(f"unsupported SillyTavern scope: {scope}")


def import_sillytavern_chat(
    records: str | Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    conversation_id: str,
    character_id: str,
    user_id: str = "",
    namespace: str = "sillytavern",
    max_input_bytes: int = 64 * 1024 * 1024,
    max_messages: int = 250_000,
    max_message_chars: int = 2_000_000,
    max_state_candidates: int = 100_000,
    max_state_depth: int = 32,
) -> SillyTavernImport:
    """Convert a saved ST chat while preserving every raw record digest.

    The first JSONL row may be a ``chat_metadata`` header.  Official summary
    values stored in ``message.extra.memory`` become derived checkpoints with
    parent lineage; they are not admitted as authoritative facts.
    """

    parsed, source_format = _parse_records(records, max_input_bytes=max_input_bytes)
    if len(parsed) > max_messages + 1:
        raise ValueError("SillyTavern chat exceeds the message limit")
    metadata: Mapping[str, Any] = {}
    if parsed and "chat_metadata" in parsed[0] and "mes" not in parsed[0]:
        header = parsed.pop(0)
        candidate = header.get("chat_metadata", {})
        if isinstance(candidate, Mapping):
            metadata = candidate

    events: list[ConversationEvent] = []
    checkpoints: list[MemoryCheckpoint] = []
    ledger = CheckpointLedger()
    checkpoint_start = 0
    warnings: list[str] = []
    source_refs: set[str] = set()
    state_candidates: list[SillyTavernStateCandidate] = []
    secret_values_skipped = 0
    detector = SecretDetector()

    chat_variables = metadata.get("variables")
    if isinstance(chat_variables, Mapping):
        metadata_sha256 = hashlib.sha256(
            _canonical_json(metadata).encode("utf-8")
        ).hexdigest()
        secret_values_skipped += _append_state_candidates(
            state_candidates,
            chat_variables,
            source_ref=f"chat:{conversation_id}:metadata",
            source_sha256=metadata_sha256,
            scope="conversation",
            detector=detector,
            max_candidates=max_state_candidates,
            max_depth=max_state_depth,
        )
    for index, record in enumerate(parsed):
        if not isinstance(record.get("mes", ""), str):
            raise TypeError(f"SillyTavern message {index} has non-text mes")
        if len(str(record.get("mes", ""))) > max_message_chars:
            raise ValueError(f"SillyTavern message {index} exceeds the character limit")
        extra = record.get("extra")
        extra = extra if isinstance(extra, Mapping) else {}
        role = _role_for(record, extra)
        source_ref = _source_ref(record, extra, conversation_id, index)
        if source_ref in source_refs:
            warnings.append(
                f"duplicate host message id at index {index}; used positional identity"
            )
            source_ref = f"chat:{conversation_id}:message:{index}"
        source_refs.add(source_ref)
        swipes = record.get("swipes")
        swipe_digests = (
            tuple(
                hashlib.sha256(item.encode("utf-8")).hexdigest()
                for item in swipes
                if isinstance(item, str)
            )
            if isinstance(swipes, list)
            else ()
        )
        event = ConversationEvent.create(
            host_id="sillytavern",
            conversation_id=conversation_id,
            source_ref=source_ref,
            sequence=index,
            role=role,
            content=str(record.get("mes", "")),
            participant_id=str(record.get("name", "")),
            created_at=str(record.get("send_date", "")),
            metadata={
                "is_system": bool(record.get("is_system", False)),
                "is_user": bool(record.get("is_user", False)),
                "swipe_id": record.get("swipe_id"),
                "swipe_count": len(swipe_digests),
                "swipe_sha256": swipe_digests,
                "type": extra.get("type"),
            },
            raw_source=record,
        )
        events.append(event)

        message_variables = _active_message_variables(record)
        if message_variables is not None:
            secret_values_skipped += _append_state_candidates(
                state_candidates,
                message_variables,
                source_ref=event.source_ref,
                source_sha256=event.source_sha256,
                scope="message",
                detector=detector,
                max_candidates=max_state_candidates,
                max_depth=max_state_depth,
            )

        summary = extra.get("memory")
        if isinstance(summary, str) and summary.strip():
            source_events = events[checkpoint_start:]
            checkpoint = MemoryCheckpoint.create(
                summary,
                source_events,
                parent_checkpoint_id=(
                    ledger.active.checkpoint_id if ledger.active else None
                ),
                producer="sillytavern:summarize",
                created_at=event.created_at,
            )
            ledger.append(checkpoint)
            checkpoints.append(checkpoint)
            checkpoint_start = len(events)

    scopes = [
        map_sillytavern_scope(
            "global",
            namespace=namespace,
            conversation_id=conversation_id,
            character_id=character_id,
            user_id=user_id,
        ),
        map_sillytavern_scope(
            "character",
            namespace=namespace,
            conversation_id=conversation_id,
            character_id=character_id,
            user_id=user_id,
        ),
        map_sillytavern_scope(
            "chat",
            namespace=namespace,
            conversation_id=conversation_id,
            character_id=character_id,
            user_id=user_id,
        ),
    ]
    if user_id.strip():
        scopes.insert(
            1,
            map_sillytavern_scope(
                "user",
                namespace=namespace,
                conversation_id=conversation_id,
                character_id=character_id,
                user_id=user_id,
            ),
        )
    if secret_values_skipped:
        warnings.append(
            f"skipped {secret_values_skipped} suspected secret state value(s)"
        )
    return SillyTavernImport(
        conversation_id=conversation_id,
        metadata_json=json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        events=tuple(events),
        checkpoints=tuple(checkpoints),
        scopes=tuple(scopes),
        source_format=source_format,
        warnings=tuple(warnings),
        state_candidates=tuple(state_candidates),
        secret_values_skipped=secret_values_skipped,
    )


def _parse_records(
    records: str | Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    max_input_bytes: int,
) -> tuple[list[dict[str, Any]], str]:
    if isinstance(records, str):
        if len(records.encode("utf-8")) > max_input_bytes:
            raise ValueError("SillyTavern chat exceeds the input byte limit")
        stripped = records.strip()
        if not stripped:
            return [], "empty"
        try:
            whole = json.loads(stripped)
        except json.JSONDecodeError:
            whole = None
        if isinstance(whole, list):
            return _object_records(whole), "json-array"
        if isinstance(whole, dict):
            return _records_from_mapping(whole), "json-object"
        parsed: list[dict[str, Any]] = []
        for line_number, line in enumerate(records.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid SillyTavern JSONL at line {line_number}"
                ) from exc
            if not isinstance(value, dict):
                raise TypeError(
                    f"SillyTavern JSONL line {line_number} is not an object"
                )
            parsed.append(value)
        return parsed, "jsonl"
    if isinstance(records, Mapping):
        return _records_from_mapping(records), "mapping"
    return _object_records(records), "records"


def _object_records(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise TypeError(f"SillyTavern record {index} is not an object")
        parsed.append(dict(value))
    return parsed


def _records_from_mapping(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    for key in ("messages", "chat"):
        candidate = value.get(key)
        if isinstance(candidate, list):
            records = _object_records(candidate)
            metadata = value.get("chat_metadata")
            if isinstance(metadata, Mapping):
                records.insert(0, {"chat_metadata": dict(metadata)})
            return records
    return [dict(value)]


def _source_ref(
    record: Mapping[str, Any],
    extra: Mapping[str, Any],
    conversation_id: str,
    index: int,
) -> str:
    for candidate in (
        extra.get("message_id"),
        record.get("message_id"),
        record.get("id"),
    ):
        if isinstance(candidate, (str, int)) and str(candidate).strip():
            digest = hashlib.sha256(str(candidate).encode("utf-8")).hexdigest()[:24]
            return f"chat:{conversation_id}:host-message:{digest}"
    return f"chat:{conversation_id}:message:{index}"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _active_message_variables(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    variables = record.get("variables")
    if isinstance(variables, Mapping):
        return variables
    if isinstance(variables, list):
        swipe_id = record.get("swipe_id", 0)
        index = swipe_id if isinstance(swipe_id, int) and swipe_id >= 0 else 0
        if index < len(variables) and isinstance(variables[index], Mapping):
            return variables[index]
    return None


def _append_state_candidates(
    target: list[SillyTavernStateCandidate],
    value: Mapping[str, Any],
    *,
    source_ref: str,
    source_sha256: str,
    scope: str,
    detector: SecretDetector,
    max_candidates: int,
    max_depth: int,
) -> int:
    skipped = 0
    for pointer, leaf in _state_leaves(value, max_depth=max_depth):
        if len(target) >= max_candidates:
            raise ValueError("SillyTavern state exceeds the candidate limit")
        value_json = _canonical_json(leaf)
        if detector.contains_secret(f"{pointer}={value_json}"):
            skipped += 1
            continue
        target.append(
            SillyTavernStateCandidate(
                source_ref=source_ref,
                source_sha256=source_sha256,
                scope=scope,
                json_pointer=pointer,
                value_json=value_json,
            )
        )
    return skipped


def _state_leaves(
    value: object, *, max_depth: int, pointer: str = "", depth: int = 0
) -> list[tuple[str, object]]:
    if depth > max_depth:
        raise ValueError("SillyTavern state exceeds the nesting limit")
    leaves: list[tuple[str, object]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            token = str(key).replace("~", "~0").replace("/", "~1")
            leaves.extend(
                _state_leaves(
                    child,
                    max_depth=max_depth,
                    pointer=f"{pointer}/{token}",
                    depth=depth + 1,
                )
            )
        return leaves
    if isinstance(value, list):
        for index, child in enumerate(value):
            leaves.extend(
                _state_leaves(
                    child,
                    max_depth=max_depth,
                    pointer=f"{pointer}/{index}",
                    depth=depth + 1,
                )
            )
        return leaves
    return [(pointer or "/", value)]


def _role_for(record: Mapping[str, Any], extra: Mapping[str, Any]) -> str:
    # ST also uses is_system as a visibility flag.  Preserve explicit authorship
    # first so hiding a user message cannot silently turn it into system evidence.
    if bool(record.get("is_user", False)):
        return "user"
    if str(extra.get("type", "")).lower() == "narrator":
        return "narrator"
    if bool(record.get("is_system", False)):
        return "system"
    return "assistant"
