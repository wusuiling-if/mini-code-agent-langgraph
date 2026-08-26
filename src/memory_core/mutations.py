"""Replayable, evidence-bound memory mutations and canonical snapshots.

This is deliberately a memory protocol rather than a generic table/SQL patch
format.  A mutation names a semantic entity and predicate, keeps exact source
revisions, and can be replayed without trusting a model-generated diff.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from memory_core.continuity import RETENTION_CLASSES, ContinuityMemory

MUTATION_OPERATIONS = frozenset({"assert", "dispute", "withdraw"})
VALUE_TYPES = frozenset({"text", "number", "boolean", "object", "array", "any"})
AUTHORITIES = frozenset({"none", "inform", "act"})
_AUTHORITY_RANK = {"none": 0, "inform": 1, "act": 2}


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")


@dataclass(frozen=True)
class MemoryFieldSchema:
    """Versioned meaning for one predicate in the canonical memory state."""

    schema_id: str
    version: int
    predicate: str
    value_type: str = "any"
    description: str = ""
    max_authority: str = "inform"
    retention_class: str = "durable"

    def __post_init__(self) -> None:
        _require_text("schema id", self.schema_id)
        _require_text("schema predicate", self.predicate)
        if self.version < 1:
            raise ValueError("schema version must be positive")
        if self.value_type not in VALUE_TYPES:
            raise ValueError(f"unsupported memory value type: {self.value_type}")
        if self.max_authority not in AUTHORITIES:
            raise ValueError(f"unsupported maximum authority: {self.max_authority}")
        if self.retention_class not in RETENTION_CLASSES:
            raise ValueError(f"unsupported retention class: {self.retention_class}")

    def validate(self, value: object) -> None:
        matches = {
            "text": isinstance(value, str),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "object": isinstance(value, Mapping),
            "array": isinstance(value, list),
            "any": True,
        }
        if not matches[self.value_type]:
            raise TypeError(
                f"predicate {self.predicate!r} requires {self.value_type} values"
            )


@dataclass(frozen=True)
class MemoryMutation:
    """One append-only semantic change bound to source event revisions."""

    mutation_id: str
    stream_id: str
    revision: int
    base_revision: int
    operation: str
    entity_id: str
    predicate: str
    value_json: str | None
    schema_id: str
    schema_version: int
    source_event_revisions: tuple[tuple[str, str], ...]
    authority: str = "none"
    recorded_at: str = ""

    def __post_init__(self) -> None:
        for name, value in (
            ("mutation id", self.mutation_id),
            ("stream id", self.stream_id),
            ("entity id", self.entity_id),
            ("predicate", self.predicate),
            ("schema id", self.schema_id),
        ):
            _require_text(name, value)
        if self.revision < 1 or self.base_revision != self.revision - 1:
            raise ValueError("mutation revision must immediately follow base revision")
        if self.operation not in MUTATION_OPERATIONS:
            raise ValueError(f"unsupported memory mutation: {self.operation}")
        if self.schema_version < 1:
            raise ValueError("schema version must be positive")
        if self.authority not in AUTHORITIES:
            raise ValueError(f"unsupported memory authority: {self.authority}")
        if not self.source_event_revisions:
            raise ValueError("mutation requires source event revisions")
        for event_id, source_sha256 in self.source_event_revisions:
            _require_text("source event id", event_id)
            if len(source_sha256) != 64 or any(
                character not in "0123456789abcdef" for character in source_sha256
            ):
                raise ValueError("source event revision must be a lowercase SHA-256")
        if self.operation == "assert" and self.value_json is None:
            raise ValueError("assert mutation requires a value")
        if self.value_json is not None:
            try:
                json.loads(self.value_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("mutation value must be valid JSON") from exc

    @classmethod
    def create(
        cls,
        *,
        stream_id: str,
        base_revision: int,
        operation: str,
        entity_id: str,
        predicate: str,
        source_event_revisions: Sequence[tuple[str, str]],
        schema_id: str,
        schema_version: int,
        value: object | None = None,
        authority: str = "none",
        recorded_at: str = "",
    ) -> MemoryMutation:
        value_json = None if value is None else _canonical_json(value)
        payload = {
            "stream_id": stream_id,
            "revision": base_revision + 1,
            "base_revision": base_revision,
            "operation": operation,
            "entity_id": entity_id,
            "predicate": predicate,
            "value_json": value_json,
            "schema_id": schema_id,
            "schema_version": schema_version,
            "source_event_revisions": tuple(source_event_revisions),
            "authority": authority,
            "recorded_at": recorded_at,
        }
        return cls(mutation_id=_digest(payload), **payload)


@dataclass(frozen=True)
class MaterializedMemory:
    entity_id: str
    predicate: str
    status: str
    value_json: str | None
    authority: str
    mutation_id: str
    source_event_revisions: tuple[tuple[str, str], ...]
    retention_class: str = "durable"

    def to_continuity_memory(
        self,
        *,
        text: str,
        recorded_at_ns: int = 0,
        last_used_at_ns: int = 0,
        use_count: int = 0,
        importance: float = 0.5,
        confidence: float = 0.5,
        identity_anchor: str | None = None,
        pinned: bool = False,
        source_valid: bool = True,
    ) -> ContinuityMemory:
        """Build an explicit continuity view without guessing how JSON is worded."""

        return ContinuityMemory(
            record_id=f"{self.entity_id}:{self.predicate}",
            text=text,
            retention_class=self.retention_class,
            recorded_at_ns=recorded_at_ns,
            last_used_at_ns=last_used_at_ns,
            use_count=use_count,
            importance=importance,
            confidence=confidence,
            identity_anchor=identity_anchor or self.entity_id,
            pinned=pinned,
            source_valid=source_valid,
            status=self.status,
            authority=self.authority,
            evidence_refs=tuple(item[0] for item in self.source_event_revisions),
        )


@dataclass(frozen=True)
class CanonicalMemorySnapshot:
    snapshot_id: str
    stream_id: str
    through_revision: int
    state_json: str
    mutation_chain_sha256: str
    parent_snapshot_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        stream_id: str,
        through_revision: int,
        state: Mapping[tuple[str, str], MaterializedMemory],
        mutation_ids: Sequence[str],
        parent_snapshot_id: str | None = None,
    ) -> CanonicalMemorySnapshot:
        rows = [
            {
                "entity_id": item.entity_id,
                "predicate": item.predicate,
                "status": item.status,
                "value_json": item.value_json,
                "authority": item.authority,
                "mutation_id": item.mutation_id,
                "source_event_revisions": item.source_event_revisions,
                "retention_class": item.retention_class,
            }
            for _, item in sorted(state.items())
        ]
        state_json = _canonical_json(rows)
        chain_sha256 = _digest(list(mutation_ids))
        payload = {
            "stream_id": stream_id,
            "through_revision": through_revision,
            "state_json": state_json,
            "mutation_chain_sha256": chain_sha256,
            "parent_snapshot_id": parent_snapshot_id,
        }
        return cls(snapshot_id=_digest(payload), **payload)

    def materialize(self) -> dict[tuple[str, str], MaterializedMemory]:
        try:
            rows = json.loads(self.state_json)
        except json.JSONDecodeError as exc:
            raise ValueError("snapshot state is invalid JSON") from exc
        if not isinstance(rows, list):
            raise TypeError("snapshot state must be a list")
        state: dict[tuple[str, str], MaterializedMemory] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise TypeError("snapshot state rows must be objects")
            revisions = tuple(tuple(item) for item in row["source_event_revisions"])
            item = MaterializedMemory(
                entity_id=row["entity_id"],
                predicate=row["predicate"],
                status=row["status"],
                value_json=row.get("value_json"),
                authority=row["authority"],
                mutation_id=row["mutation_id"],
                source_event_revisions=revisions,
                retention_class=row.get("retention_class", "durable"),
            )
            state[(item.entity_id, item.predicate)] = item
        return state


@dataclass(frozen=True)
class PostCondition:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class MemoryCommitReport:
    mutation_id: str
    revision: int
    outcome: str
    checks: tuple[PostCondition, ...]
    warnings: tuple[str, ...] = ()

    @property
    def successful(self) -> bool:
        return self.outcome in {"committed", "already_committed"} and all(
            check.passed for check in self.checks
        )


class MutationLedger:
    """In-memory reference ledger suitable for host adapters and tests.

    Durable hosts can persist the same mutation/snapshot values in their own
    database.  Optimistic ``base_revision`` checks prevent silent last-writer
    wins; callers may re-read and explicitly build a new mutation to rebase.
    """

    def __init__(
        self,
        stream_id: str,
        schemas: Sequence[MemoryFieldSchema],
        *,
        snapshot: CanonicalMemorySnapshot | None = None,
    ) -> None:
        _require_text("stream id", stream_id)
        self.stream_id = stream_id
        self._schemas = {
            (item.schema_id, item.version, item.predicate): item for item in schemas
        }
        if len(self._schemas) != len(schemas):
            raise ValueError("memory schemas must be unique")
        if snapshot is not None and snapshot.stream_id != stream_id:
            raise ValueError("snapshot belongs to another stream")
        self._snapshot = snapshot
        self._state = snapshot.materialize() if snapshot else {}
        self._revision = snapshot.through_revision if snapshot else 0
        self._mutations: list[MemoryMutation] = []
        self._mutation_ids: set[str] = set()

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def state(self) -> tuple[MaterializedMemory, ...]:
        return tuple(item for _, item in sorted(self._state.items()))

    def append(self, mutation: MemoryMutation) -> MemoryCommitReport:
        if mutation.mutation_id in self._mutation_ids:
            return MemoryCommitReport(
                mutation.mutation_id,
                self._revision,
                "already_committed",
                (PostCondition("idempotent_replay", True),),
            )
        if mutation.stream_id != self.stream_id:
            raise ValueError("mutation belongs to another stream")
        if mutation.base_revision != self._revision:
            return MemoryCommitReport(
                mutation.mutation_id,
                self._revision,
                "conflict",
                (
                    PostCondition(
                        "base_revision_matches",
                        False,
                        f"expected {self._revision}, got {mutation.base_revision}",
                    ),
                ),
            )
        schema = self._schemas.get(
            (mutation.schema_id, mutation.schema_version, mutation.predicate)
        )
        if schema is None:
            return MemoryCommitReport(
                mutation.mutation_id,
                self._revision,
                "rejected",
                (PostCondition("schema_resolved", False),),
            )
        if _AUTHORITY_RANK[mutation.authority] > _AUTHORITY_RANK[schema.max_authority]:
            return MemoryCommitReport(
                mutation.mutation_id,
                self._revision,
                "rejected",
                (
                    PostCondition("schema_resolved", True),
                    PostCondition(
                        "authority_within_schema_limit",
                        False,
                        f"maximum authority is {schema.max_authority}",
                    ),
                ),
            )
        if mutation.operation == "assert":
            try:
                schema.validate(json.loads(mutation.value_json or "null"))
            except TypeError as exc:
                return MemoryCommitReport(
                    mutation.mutation_id,
                    self._revision,
                    "rejected",
                    (
                        PostCondition("schema_resolved", True),
                        PostCondition("schema_value_valid", False, str(exc)),
                    ),
                )

        status = {
            "assert": "active",
            "dispute": "disputed",
            "withdraw": "withdrawn",
        }[mutation.operation]
        previous = self._state.get((mutation.entity_id, mutation.predicate))
        value_json = mutation.value_json
        if value_json is None and previous is not None:
            value_json = previous.value_json
        self._state[(mutation.entity_id, mutation.predicate)] = MaterializedMemory(
            entity_id=mutation.entity_id,
            predicate=mutation.predicate,
            status=status,
            value_json=value_json,
            authority=mutation.authority,
            mutation_id=mutation.mutation_id,
            source_event_revisions=mutation.source_event_revisions,
            retention_class=schema.retention_class,
        )
        self._mutations.append(mutation)
        self._mutation_ids.add(mutation.mutation_id)
        self._revision = mutation.revision
        materialized = self._state[(mutation.entity_id, mutation.predicate)]
        return MemoryCommitReport(
            mutation.mutation_id,
            self._revision,
            "committed",
            (
                PostCondition("base_revision_matches", True),
                PostCondition("schema_resolved", True),
                PostCondition("schema_value_valid", True),
                PostCondition("authority_within_schema_limit", True),
                PostCondition(
                    "canonical_state_matches",
                    materialized.mutation_id == mutation.mutation_id,
                ),
            ),
        )

    def compact(self) -> CanonicalMemorySnapshot:
        snapshot = CanonicalMemorySnapshot.create(
            stream_id=self.stream_id,
            through_revision=self._revision,
            state=self._state,
            mutation_ids=tuple(item.mutation_id for item in self._mutations),
            parent_snapshot_id=(self._snapshot.snapshot_id if self._snapshot else None),
        )
        if snapshot.materialize() != self._state:
            raise ValueError("snapshot failed canonical round-trip")
        self._snapshot = snapshot
        self._mutations.clear()
        self._mutation_ids.clear()
        return snapshot

    def invalid_source_bindings(
        self, current_revisions: Mapping[str, str]
    ) -> tuple[str, ...]:
        invalid = {
            item.mutation_id
            for item in self._state.values()
            if any(
                current_revisions.get(event_id) != digest
                for event_id, digest in item.source_event_revisions
            )
        }
        return tuple(sorted(invalid))
