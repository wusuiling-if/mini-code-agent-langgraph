from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mini_code_agent.locking import exclusive_file_lock
from mini_code_agent.memory_models import (
    MEMORY_CONTROL_OPERATIONS,
    MEMORY_KINDS,
    MEMORY_ORIGINS,
    MEMORY_RELATIONS,
    MEMORY_STATUSES,
    EvidenceSource,
    MemoryCard,
    MemoryDecisionRecord,
    MemoryEdge,
    MemoryIntegrityError,
    MemoryNotInitializedError,
    MemoryOutcomeRecord,
    MemorySearchResult,
    MemoryStoreHealth,
    MemoryStoreStatus,
    MemoryUtilityStats,
    MemoryVerification,
    validate_authority,
    validate_derived_authority,
)
from mini_code_agent.receipt import canonical_json, digest_json
from mini_code_agent.utils import MAX_STATE_FILE_BYTES

SCHEMA_VERSION = 2
READABLE_SCHEMA_VERSIONS = frozenset({1, SCHEMA_VERSION})
DATABASE_NAME = "memory.sqlite3"
KEY_NAME = "memory.key"
LOCK_NAME = "memory.lock"
MAX_QUERY_LENGTH = 2_000
MAX_TEXT_LENGTH = 1_000_000


class SQLiteMemoryStore:
    """Evidence-bound, append-only local memory storage.

    Read-only construction never creates the directory, database, or key. The
    runtime does not instantiate this class, so memory remains opt-in in phase 1.
    """

    def __init__(self, directory: Path, *, read_only: bool = False):
        self.directory = Path(directory).expanduser()
        self.database_path = self.directory / DATABASE_NAME
        self.key_path = self.directory / KEY_NAME
        self.read_only = read_only

    @property
    def initialized(self) -> bool:
        return self.database_path.is_file()

    def initialize(self) -> None:
        if self.read_only:
            raise PermissionError("read-only memory store cannot be initialized")
        self._ensure_private_directory()
        with exclusive_file_lock(self.directory / LOCK_NAME):
            self._initialize_locked()

    def _initialize_locked(self) -> None:
        """Initialize while the caller holds the store-wide write lock."""

        key = self._load_or_create_key()
        if len(key) != 32:  # Defensive: the schema must never be created unsigned.
            raise MemoryIntegrityError("memory authentication key is invalid")
        with self._connect(write=True) as connection:
            self._create_schema(connection)

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize a complete logical write and open an immediate transaction."""

        self._require_writable()
        self._ensure_private_directory()
        with exclusive_file_lock(self.directory / LOCK_NAME):
            self._initialize_locked()
            with self._connect(write=True) as connection:
                connection.execute("BEGIN IMMEDIATE")
                yield connection

    def add_card(
        self,
        *,
        value: str,
        abstraction: str,
        cue_anchors: Sequence[str] = (),
        kind: str = "semantic",
        subtype: str = "fact",
        scope: str = "global",
        scope_key: str = "",
        origin: str = "agent",
        authority: str = "inform",
        confidence: float = 0.5,
        importance: float = 0.5,
        valid_from: str | None = None,
        valid_to: str | None = None,
        sources: Sequence[EvidenceSource] = (),
        derived_from: Sequence[str] = (),
    ) -> MemoryCard:
        self._require_writable()
        self._validate_card_input(
            value=value,
            abstraction=abstraction,
            cue_anchors=cue_anchors,
            kind=kind,
            subtype=subtype,
            scope=scope,
            scope_key=scope_key,
            origin=origin,
            authority=authority,
            confidence=confidence,
            importance=importance,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        if not sources:
            raise ValueError("memory card requires at least one evidence source")
        with self._write_transaction() as connection:
            source_authorities = tuple(
                self._card_authority(connection, card_id) for card_id in derived_from
            )
            validate_derived_authority(authority, source_authorities)
            card = self._insert_card(
                connection,
                value=value,
                abstraction=abstraction,
                cue_anchors=tuple(cue_anchors),
                kind=kind,
                subtype=subtype,
                scope=scope,
                scope_key=scope_key,
                origin=origin,
                authority=authority,
                confidence=confidence,
                importance=importance,
                valid_from=valid_from,
                valid_to=valid_to,
            )
            for source in sources:
                self._insert_source(connection, card.id, source)
            for source_id in derived_from:
                self._insert_edge(connection, card.id, source_id, "derived_from")
            return card

    def add_card_once_for_source(
        self,
        *,
        source: EvidenceSource,
        value: str,
        abstraction: str,
        cue_anchors: Sequence[str] = (),
        kind: str = "semantic",
        subtype: str = "fact",
        scope: str = "global",
        scope_key: str = "",
        origin: str = "agent",
        authority: str = "inform",
        confidence: float = 0.5,
        importance: float = 0.5,
        valid_from: str | None = None,
        valid_to: str | None = None,
    ) -> MemoryCard:
        """Atomically replay one source-bound memory without duplicating it."""

        self._require_writable()
        values = {
            "value": value,
            "abstraction": abstraction,
            "cue_anchors": cue_anchors,
            "kind": kind,
            "subtype": subtype,
            "scope": scope,
            "scope_key": scope_key,
            "origin": origin,
            "authority": authority,
            "confidence": confidence,
            "importance": importance,
            "valid_from": valid_from,
            "valid_to": valid_to,
        }
        self._validate_card_input(**values)
        with self._write_transaction() as connection:
            key = self._load_existing_key()
            rows = connection.execute(
                """
                SELECT * FROM sources
                WHERE source_type = ? AND source_ref = ?
                ORDER BY recorded_at_ns, rowid
                """,
                (source.source_type, source.source_ref),
            ).fetchall()
            for row in rows:
                self._verify_row("source", row, self._source_payload(row), key)
                if (
                    row["source_sha256"] != source.source_sha256.lower()
                    or row["origin"] != source.origin
                ):
                    raise ValueError(
                        "evidence source reference cannot change provenance"
                    )
                card_row = self._require_authenticated_card(
                    connection, str(row["card_id"])
                )
                if card_row["kind"] == kind and card_row["subtype"] == subtype:
                    status = self._current_status(connection, card_row["id"], key)
                    return self._card_from_row(card_row, status)

            card = self._insert_card(
                connection,
                **values,
            )
            self._insert_source(connection, card.id, source)
            return card

    def upsert_scoped_singleton(
        self,
        *,
        identity_anchor: str,
        value: str,
        abstraction: str,
        cue_anchors: Sequence[str] = (),
        kind: str,
        subtype: str,
        scope: str,
        scope_key: str,
        origin: str,
        authority: str,
        confidence: float,
        importance: float,
        valid_from: str | None,
        valid_to: str | None = None,
        sources: Sequence[EvidenceSource],
    ) -> MemoryCard:
        """Atomically merge or revise a singleton memory family.

        A family is identified by kind, subtype, scope and scope key. Replaying
        the same identity appends evidence idempotently. A changed identity
        creates one new active revision and supersedes every active predecessor.
        The store-wide lock and ``BEGIN IMMEDIATE`` cover the full read/write
        decision so concurrent callers cannot form duplicate cards.
        """

        self._require_writable()
        self._validate_card_input(
            value=value,
            abstraction=abstraction,
            cue_anchors=cue_anchors,
            kind=kind,
            subtype=subtype,
            scope=scope,
            scope_key=scope_key,
            origin=origin,
            authority=authority,
            confidence=confidence,
            importance=importance,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        if not identity_anchor.strip() or identity_anchor not in cue_anchors:
            raise ValueError("singleton identity anchor must be a cue anchor")
        if not sources:
            raise ValueError("memory card requires at least one evidence source")

        with self._write_transaction() as connection:
            key = self._load_existing_key()
            rows = connection.execute(
                """
                SELECT * FROM cards
                WHERE kind = ? AND subtype = ? AND scope = ? AND scope_key = ?
                ORDER BY recorded_at_ns DESC, rowid DESC
                """,
                (kind, subtype, scope, scope_key),
            ).fetchall()
            active: list[sqlite3.Row] = []
            matching: list[tuple[sqlite3.Row, str]] = []
            for row in rows:
                self._verify_row("card", row, self._card_payload(row), key)
                status = self._current_status(connection, row["id"], key)
                if identity_anchor in json.loads(row["cue_anchors_json"]):
                    matching.append((row, status))
                if status == "active":
                    active.append(row)

            def revision_moment(row: sqlite3.Row) -> datetime:
                parsed = self._parse_timestamp(row["valid_from"], "valid_from")
                if parsed is not None:
                    return parsed
                return datetime.fromtimestamp(
                    int(row["recorded_at_ns"]) / 1_000_000_000,
                    tz=timezone.utc,
                )

            incoming_moment = self._parse_timestamp(valid_from, "valid_from")
            newest_active = max(active, key=revision_moment, default=None)
            stale_arrival = (
                newest_active is not None
                and incoming_moment is not None
                and incoming_moment < revision_moment(newest_active)
                and identity_anchor not in json.loads(newest_active["cue_anchors_json"])
            )
            if stale_arrival:
                for old in active:
                    if old["id"] == newest_active["id"]:
                        continue
                    self._insert_edge(
                        connection,
                        str(newest_active["id"]),
                        str(old["id"]),
                        "supersedes",
                    )
                    self._insert_event(
                        connection,
                        str(old["id"]),
                        "superseded",
                        str(newest_active["id"]),
                    )
                historical = next(
                    ((row, status) for row, status in matching if status != "active"),
                    None,
                )
                if historical is not None:
                    row, status = historical
                    for source in sources:
                        self._add_source_in_connection(
                            connection, str(row["id"]), source
                        )
                    return self._card_from_row(row, status)

                card = self._insert_card(
                    connection,
                    value=value,
                    abstraction=abstraction,
                    cue_anchors=tuple(cue_anchors),
                    kind=kind,
                    subtype=subtype,
                    scope=scope,
                    scope_key=scope_key,
                    origin=origin,
                    authority=authority,
                    confidence=confidence,
                    importance=importance,
                    valid_from=valid_from,
                    valid_to=valid_to,
                )
                for source in sources:
                    self._add_source_in_connection(connection, card.id, source)
                self._insert_edge(
                    connection, str(newest_active["id"]), card.id, "supersedes"
                )
                self._insert_event(
                    connection,
                    card.id,
                    "superseded",
                    str(newest_active["id"]),
                )
                row = connection.execute(
                    "SELECT * FROM cards WHERE id = ?", (card.id,)
                ).fetchone()
                return self._card_from_row(row, "superseded")

            exact_active = [row for row, status in matching if status == "active"]
            if exact_active:
                canonical = max(exact_active, key=revision_moment)
                for source in sources:
                    self._add_source_in_connection(
                        connection, str(canonical["id"]), source
                    )
                for old in active:
                    if old["id"] == canonical["id"]:
                        continue
                    self._insert_edge(
                        connection, str(canonical["id"]), str(old["id"]), "supersedes"
                    )
                    self._insert_event(
                        connection, str(old["id"]), "superseded", str(canonical["id"])
                    )
                return self._card_from_row(canonical, "active")

            card = self._insert_card(
                connection,
                value=value,
                abstraction=abstraction,
                cue_anchors=tuple(cue_anchors),
                kind=kind,
                subtype=subtype,
                scope=scope,
                scope_key=scope_key,
                origin=origin,
                authority=authority,
                confidence=confidence,
                importance=importance,
                valid_from=valid_from,
                valid_to=valid_to,
            )
            for source in sources:
                self._add_source_in_connection(connection, card.id, source)
            for old in active:
                self._insert_edge(connection, card.id, str(old["id"]), "supersedes")
                self._insert_event(connection, str(old["id"]), "superseded", card.id)
            return card

    def supersede(
        self,
        old_card_id: str,
        *,
        value: str,
        abstraction: str,
        cue_anchors: Sequence[str] = (),
        kind: str = "semantic",
        subtype: str = "fact",
        scope: str = "global",
        scope_key: str = "",
        origin: str = "agent",
        authority: str = "inform",
        confidence: float = 0.5,
        importance: float = 0.5,
        valid_from: str | None = None,
        valid_to: str | None = None,
        sources: Sequence[EvidenceSource] = (),
    ) -> MemoryCard:
        self._require_writable()
        self._validate_card_input(
            value=value,
            abstraction=abstraction,
            cue_anchors=cue_anchors,
            kind=kind,
            subtype=subtype,
            scope=scope,
            scope_key=scope_key,
            origin=origin,
            authority=authority,
            confidence=confidence,
            importance=importance,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        if not sources:
            raise ValueError("memory card requires at least one evidence source")
        with self._write_transaction() as connection:
            old_authority = self._card_authority(connection, old_card_id)
            validate_derived_authority(authority, (old_authority,))
            new_card = self._insert_card(
                connection,
                value=value,
                abstraction=abstraction,
                cue_anchors=tuple(cue_anchors),
                kind=kind,
                subtype=subtype,
                scope=scope,
                scope_key=scope_key,
                origin=origin,
                authority=authority,
                confidence=confidence,
                importance=importance,
                valid_from=valid_from,
                valid_to=valid_to,
            )
            for source in sources:
                self._insert_source(connection, new_card.id, source)
            self._insert_edge(connection, new_card.id, old_card_id, "supersedes")
            self._insert_event(connection, old_card_id, "superseded", new_card.id)
            return new_card

    def transition(
        self, card_id: str, status: str, *, related_card_id: str | None = None
    ) -> None:
        self._require_writable()
        if status not in MEMORY_STATUSES or status == "active":
            raise ValueError("transition status must retire or qualify a memory")
        with self._write_transaction() as connection:
            self._require_authenticated_card(connection, card_id)
            if related_card_id is not None:
                self._require_authenticated_card(connection, related_card_id)
            self._insert_event(connection, card_id, status, related_card_id)

    def add_edge(self, source_id: str, target_id: str, relation: str) -> MemoryEdge:
        self._require_writable()
        with self._write_transaction() as connection:
            return self._insert_edge(connection, source_id, target_id, relation)

    def add_source(self, card_id: str, source: EvidenceSource) -> EvidenceSource:
        """Append authenticated evidence to an existing immutable card.

        Replaying the same source is idempotent. Reusing a source reference with
        different provenance is rejected instead of silently rewriting evidence.
        """

        self._require_writable()
        with self._write_transaction() as connection:
            return self._add_source_in_connection(connection, card_id, source)

    def _add_source_in_connection(
        self,
        connection: sqlite3.Connection,
        card_id: str,
        source: EvidenceSource,
    ) -> EvidenceSource:
        self._require_authenticated_card(connection, card_id)
        key = self._load_existing_key()
        rows = connection.execute(
            """
            SELECT * FROM sources
            WHERE card_id = ? AND source_type = ? AND source_ref = ?
            ORDER BY recorded_at_ns, rowid
            """,
            (card_id, source.source_type, source.source_ref),
        ).fetchall()
        for row in rows:
            self._verify_row("source", row, self._source_payload(row), key)
            if (
                row["source_sha256"] != source.source_sha256.lower()
                or row["origin"] != source.origin
            ):
                raise ValueError("evidence source reference cannot change provenance")
            return self._source_from_row(row)
        return self._insert_source(connection, card_id, source)

    def record_memory_decision(
        self,
        *,
        query_sha256: str,
        stage: str,
        operation: str,
        selected_card_ids: Sequence[str],
        expected_utility: float,
        reason: str,
        shadow: bool,
    ) -> MemoryDecisionRecord:
        """Append a value-free, authenticated memory-control decision."""

        if not re.fullmatch(r"[0-9a-fA-F]{64}", query_sha256):
            raise ValueError("memory decision query fingerprint must be SHA-256")
        if not isinstance(stage, str) or not stage.strip() or len(stage) > 100:
            raise ValueError("memory decision stage must be a short non-blank string")
        if operation not in MEMORY_CONTROL_OPERATIONS:
            raise ValueError("unsupported memory control operation")
        card_ids = tuple(dict.fromkeys(str(item) for item in selected_card_ids))
        if len(card_ids) > 20 or any(not item for item in card_ids):
            raise ValueError("memory decision selected-card set is invalid")
        if (
            not isinstance(expected_utility, (int, float))
            or isinstance(expected_utility, bool)
            or not math.isfinite(float(expected_utility))
            or not -1 <= float(expected_utility) <= 1
        ):
            raise ValueError(
                "memory decision utility must be finite and between -1 and 1"
            )
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ValueError("memory decision reason must be a short non-blank string")
        if not isinstance(shadow, bool):
            raise TypeError("memory decision shadow flag must be boolean")

        with self._write_transaction() as connection:
            for card_id in card_ids:
                self._require_authenticated_card(connection, card_id)
            payload = {
                "id": uuid.uuid4().hex,
                "query_sha256": query_sha256.lower(),
                "stage": stage.strip(),
                "operation": operation,
                "selected_card_ids": list(card_ids),
                "expected_utility": float(expected_utility),
                "reason": reason.strip(),
                "shadow": shadow,
                "recorded_at_ns": time.time_ns(),
            }
            connection.execute(
                """
                INSERT INTO memory_decisions(
                    id, query_sha256, stage, operation, selected_card_ids_json,
                    expected_utility, reason, shadow, recorded_at_ns, signature
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["id"],
                    payload["query_sha256"],
                    payload["stage"],
                    payload["operation"],
                    json.dumps(card_ids, separators=(",", ":")),
                    payload["expected_utility"],
                    payload["reason"],
                    int(shadow),
                    payload["recorded_at_ns"],
                    self._sign(payload),
                ),
            )
            return MemoryDecisionRecord(**payload)

    def record_memory_outcome(
        self,
        decision_id: str,
        *,
        success: bool,
        reward: float,
        harmful: bool,
        token_cost: int,
        evidence: EvidenceSource,
    ) -> MemoryOutcomeRecord:
        """Attach one authenticated downstream outcome to a control decision."""

        if not isinstance(success, bool) or not isinstance(harmful, bool):
            raise TypeError("memory outcome flags must be boolean")
        if (
            not isinstance(reward, (int, float))
            or isinstance(reward, bool)
            or not math.isfinite(float(reward))
            or not -1 <= float(reward) <= 1
        ):
            raise ValueError(
                "memory outcome reward must be finite and between -1 and 1"
            )
        if (
            not isinstance(token_cost, int)
            or isinstance(token_cost, bool)
            or token_cost < 0
        ):
            raise ValueError("memory outcome token cost must be a non-negative integer")
        if evidence.origin not in {"trusted_tool", "user"}:
            raise ValueError("memory outcome requires trusted runtime or user evidence")
        if not evidence.source_type.strip() or not evidence.source_ref.strip():
            raise ValueError("memory outcome evidence must have a type and reference")
        if len(evidence.source_type) > 200 or len(evidence.source_ref) > 10_000:
            raise ValueError("memory outcome evidence metadata exceeds the size limit")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", evidence.source_sha256):
            raise ValueError("memory outcome evidence digest must be SHA-256")

        with self._write_transaction() as connection:
            decision = connection.execute(
                "SELECT * FROM memory_decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            if decision is None:
                raise KeyError(f"memory decision not found: {decision_id}")
            self._verify_row(
                "decision",
                decision,
                self._decision_payload(decision),
                self._load_existing_key(),
            )
            if connection.execute(
                "SELECT 1 FROM memory_outcomes WHERE decision_id = ?", (decision_id,)
            ).fetchone():
                raise ValueError("memory decision already has an outcome")
            payload = {
                "id": uuid.uuid4().hex,
                "decision_id": decision_id,
                "success": success,
                "reward": float(reward),
                "harmful": harmful,
                "token_cost": token_cost,
                "evidence_type": evidence.source_type,
                "evidence_ref": evidence.source_ref,
                "evidence_sha256": evidence.source_sha256.lower(),
                "evidence_origin": evidence.origin,
                "recorded_at_ns": time.time_ns(),
            }
            connection.execute(
                """
                INSERT INTO memory_outcomes(
                    id, decision_id, success, reward, harmful, token_cost,
                    evidence_type, evidence_ref, evidence_sha256, evidence_origin,
                    recorded_at_ns, signature
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["id"],
                    decision_id,
                    int(success),
                    payload["reward"],
                    int(harmful),
                    token_cost,
                    payload["evidence_type"],
                    payload["evidence_ref"],
                    payload["evidence_sha256"],
                    payload["evidence_origin"],
                    payload["recorded_at_ns"],
                    self._sign(payload),
                ),
            )
            return MemoryOutcomeRecord(**payload)

    def memory_decisions(self) -> tuple[MemoryDecisionRecord, ...]:
        with self._connect() as connection:
            if self._schema_version(connection) < 2:
                return ()
            key = self._load_existing_key()
            rows = connection.execute(
                "SELECT * FROM memory_decisions ORDER BY recorded_at_ns, rowid"
            ).fetchall()
            records = []
            for row in rows:
                self._verify_row("decision", row, self._decision_payload(row), key)
                records.append(self._decision_from_row(row))
            return tuple(records)

    def memory_outcomes(self) -> tuple[MemoryOutcomeRecord, ...]:
        with self._connect() as connection:
            if self._schema_version(connection) < 2:
                return ()
            key = self._load_existing_key()
            rows = connection.execute(
                "SELECT * FROM memory_outcomes ORDER BY recorded_at_ns, rowid"
            ).fetchall()
            records = []
            for row in rows:
                self._verify_row("outcome", row, self._outcome_payload(row), key)
                records.append(self._outcome_from_row(row))
            return tuple(records)

    def memory_utility_stats(
        self, card_ids: Sequence[str] = ()
    ) -> tuple[MemoryUtilityStats, ...]:
        """Aggregate authenticated outcome feedback without trusting raw SQL rows."""

        requested = set(card_ids)
        decisions = {record.id: record for record in self.memory_decisions()}
        aggregates: dict[str, dict[str, float | int]] = {}
        if requested:
            aggregates.update(
                {
                    card_id: {
                        "uses": 0,
                        "successes": 0,
                        "failures": 0,
                        "harmful_uses": 0,
                        "reward_total": 0.0,
                    }
                    for card_id in requested
                }
            )
        for outcome in self.memory_outcomes():
            decision = decisions.get(outcome.decision_id)
            if decision is None:
                raise MemoryIntegrityError(
                    f"memory outcome has no decision: {outcome.id}"
                )
            if decision.shadow:
                continue
            for card_id in decision.selected_card_ids:
                if requested and card_id not in requested:
                    continue
                values = aggregates.setdefault(
                    card_id,
                    {
                        "uses": 0,
                        "successes": 0,
                        "failures": 0,
                        "harmful_uses": 0,
                        "reward_total": 0.0,
                    },
                )
                values["uses"] += 1
                values["successes" if outcome.success else "failures"] += 1
                values["harmful_uses"] += int(outcome.harmful)
                values["reward_total"] += outcome.reward
        return tuple(
            MemoryUtilityStats(card_id=card_id, **values)
            for card_id, values in sorted(aggregates.items())
        )

    def status(self) -> MemoryStoreStatus:
        if not self.initialized:
            return MemoryStoreStatus(False, str(self.database_path))
        with self._connect() as connection:
            version = self._schema_version(connection)
            fts_enabled = self._meta(connection, "fts_enabled") == "1"
            cards = int(connection.execute("SELECT COUNT(*) FROM cards").fetchone()[0])
            sources = int(
                connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            )
            edges = int(connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0])
            counts = []
            for memory_status in sorted(MEMORY_STATUSES):
                count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM cards AS c
                        WHERE COALESCE((
                            SELECT event_type FROM card_events AS e
                            WHERE e.card_id = c.id
                            ORDER BY e.recorded_at_ns DESC, e.rowid DESC LIMIT 1
                        ), 'active') = ?
                        """,
                        (memory_status,),
                    ).fetchone()[0]
                )
                counts.append((memory_status, count))
            return MemoryStoreStatus(
                True,
                str(self.database_path),
                schema_version=version,
                fts_enabled=fts_enabled,
                cards=cards,
                sources=sources,
                edges=edges,
                status_counts=tuple(counts),
            )

    def get_card(self, card_id: str) -> MemoryCard:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM cards WHERE id = ?", (card_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"memory card not found: {card_id}")
            key = self._load_existing_key()
            self._verify_row("card", row, self._card_payload(row), key)
            status = self._current_status(connection, card_id, key)
            return self._card_from_row(row, status)

    def sources(self, card_id: str) -> tuple[EvidenceSource, ...]:
        with self._connect() as connection:
            self._require_authenticated_card(connection, card_id)
            key = self._load_existing_key()
            rows = connection.execute(
                "SELECT * FROM sources WHERE card_id = ? ORDER BY recorded_at_ns, rowid",
                (card_id,),
            ).fetchall()
            result = []
            for row in rows:
                self._verify_row("source", row, self._source_payload(row), key)
                result.append(
                    EvidenceSource(
                        id=row["id"],
                        card_id=row["card_id"],
                        source_type=row["source_type"],
                        source_ref=row["source_ref"],
                        source_sha256=row["source_sha256"],
                        origin=row["origin"],
                        recorded_at_ns=row["recorded_at_ns"],
                    )
                )
            return tuple(result)

    def list_cards(
        self,
        *,
        include_inactive: bool = False,
        scope_pairs: Sequence[tuple[str, str]] | None = None,
        include_global: bool = True,
    ) -> tuple[MemoryCard, ...]:
        """Return authenticated cards for policy-driven retrieval.

        This deliberately bypasses the unsigned FTS acceleration table. Higher
        level retrievers can build multiple candidate routes from authenticated
        card content without trusting an index as evidence. ``scope_pairs`` is
        an optional SQL narrowing hint: every returned card and its latest state
        event is still authenticated before it can become a candidate.
        """

        with self._connect() as connection:
            key = self._load_existing_key()
            parameters: list[str] = []
            if scope_pairs is None:
                where = ""
            else:
                normalized_pairs: list[tuple[str, str]] = []
                for pair in scope_pairs:
                    if len(pair) != 2 or any(
                        not isinstance(value, str) for value in pair
                    ):
                        raise TypeError("memory scope pairs must contain two strings")
                    normalized_pairs.append((pair[0], pair[1]))
                clauses = ["scope = 'global'"] if include_global else []
                for scope, scope_key in dict.fromkeys(normalized_pairs):
                    clauses.append("(scope = ? AND scope_key = ?)")
                    parameters.extend((scope, scope_key))
                where = " WHERE " + " OR ".join(clauses) if clauses else " WHERE 0"
            rows = connection.execute(
                "SELECT * FROM cards"
                + where
                + " ORDER BY recorded_at_ns DESC, rowid DESC",
                parameters,
            ).fetchall()
            statuses = self._latest_statuses(
                connection, tuple(str(row["id"]) for row in rows), key
            )
            cards: list[MemoryCard] = []
            for row in rows:
                self._verify_row("card", row, self._card_payload(row), key)
                status = statuses[str(row["id"])]
                if include_inactive or status == "active":
                    cards.append(self._card_from_row(row, status))
            return tuple(cards)

    def relations(self, card_id: str) -> tuple[MemoryEdge, ...]:
        """Return authenticated incoming and outgoing relations for one card."""

        with self._connect() as connection:
            self._require_authenticated_card(connection, card_id)
            key = self._load_existing_key()
            rows = connection.execute(
                """
                SELECT * FROM edges
                WHERE source_id = ? OR target_id = ?
                ORDER BY recorded_at_ns, rowid
                """,
                (card_id, card_id),
            ).fetchall()
            result: list[MemoryEdge] = []
            for row in rows:
                self._verify_row("edge", row, self._edge_payload(row), key)
                result.append(
                    MemoryEdge(
                        id=row["id"],
                        source_id=row["source_id"],
                        target_id=row["target_id"],
                        relation=row["relation"],
                        recorded_at_ns=int(row["recorded_at_ns"]),
                    )
                )
            return tuple(result)

    def search(
        self, query: str, *, limit: int = 10, include_inactive: bool = False
    ) -> tuple[MemorySearchResult, ...]:
        query = query.strip()
        if not query or len(query) > MAX_QUERY_LENGTH:
            raise ValueError(
                "memory query must be non-empty and at most 2000 characters"
            )
        if limit < 1 or limit > 100:
            raise ValueError("memory search limit must be between 1 and 100")
        with self._connect() as connection:
            key = self._load_existing_key()
            rows = self._search_rows(connection, query, max(limit * 5, limit))
            results: list[MemorySearchResult] = []
            for row, score in rows:
                self._verify_row("card", row, self._card_payload(row), key)
                if not self._signed_text_matches_query(row, query):
                    # The FTS table is an unsigned acceleration structure. It
                    # may narrow candidates, but cannot make unrelated signed
                    # content eligible for retrieval.
                    continue
                status = self._current_status(connection, row["id"], key)
                if not include_inactive and status != "active":
                    continue
                results.append(
                    MemorySearchResult(
                        id=row["id"],
                        kind=row["kind"],
                        scope=row["scope"],
                        abstraction=row["abstraction"],
                        origin=row["origin"],
                        authority=row["authority"],
                        confidence=float(row["confidence"]),
                        importance=float(row["importance"]),
                        status=status,
                        score=float(score),
                    )
                )
                if len(results) >= limit:
                    break
            return tuple(results)

    def verify(self) -> MemoryVerification:
        if not self.initialized:
            return MemoryVerification(False, 0, 0, 0, 0, ("store is not initialized",))
        errors: list[str] = []
        counts = {
            "cards": 0,
            "sources": 0,
            "edges": 0,
            "card_events": 0,
            "memory_decisions": 0,
            "memory_outcomes": 0,
        }
        try:
            key = self._load_existing_key()
        except (MemoryIntegrityError, OSError, PermissionError) as exc:
            return MemoryVerification(False, 0, 0, 0, 0, (str(exc),))
        try:
            with self._connect() as connection:
                integrity = str(
                    connection.execute("PRAGMA integrity_check").fetchone()[0]
                )
                if integrity != "ok":
                    errors.append(f"sqlite integrity check failed: {integrity}")
                schema_version = self._schema_version(connection)
                table_payloads = {
                    "cards": ("card", self._card_payload),
                    "sources": ("source", self._source_payload),
                    "edges": ("edge", self._edge_payload),
                    "card_events": ("event", self._event_payload),
                }
                if schema_version >= 2:
                    table_payloads.update(
                        {
                            "memory_decisions": (
                                "decision",
                                self._decision_payload,
                            ),
                            "memory_outcomes": ("outcome", self._outcome_payload),
                        }
                    )
                for table, (label, payload_factory) in table_payloads.items():
                    for row in connection.execute(f"SELECT rowid, * FROM {table}"):
                        counts[table] += 1
                        try:
                            self._verify_row(label, row, payload_factory(row), key)
                        except MemoryIntegrityError as exc:
                            errors.append(str(exc))
                self._verify_references(
                    connection, errors, include_control=schema_version >= 2
                )
                fts_value = self._meta(connection, "fts_enabled")
                if fts_value not in {"0", "1"}:
                    errors.append("memory FTS metadata is missing or invalid")
                elif fts_value == "1":
                    self._verify_fts(connection, errors)
        except (
            sqlite3.Error,
            MemoryIntegrityError,
            MemoryNotInitializedError,
            OSError,
        ) as exc:
            errors.append(str(exc))
        return MemoryVerification(
            not errors,
            counts["cards"],
            counts["sources"],
            counts["edges"],
            counts["card_events"],
            tuple(errors),
        )

    def health(self, *, as_of: str | None = None) -> MemoryStoreHealth:
        """Summarize integrity and temporal debt without mutating the store."""

        if as_of is None:
            moment = datetime.now(timezone.utc)
        else:
            normalized = as_of[:-1] + "+00:00" if as_of.endswith("Z") else as_of
            moment = datetime.fromisoformat(normalized)
            if moment.tzinfo is None:
                raise ValueError("memory health as_of must include a UTC offset")
        cards = self.list_cards(include_inactive=True)
        active = [card for card in cards if card.status == "active"]
        expired = [
            card
            for card in active
            if card.valid_to is not None
            and self._parse_timestamp(card.valid_to, "valid_to") <= moment
        ]
        future = [
            card
            for card in active
            if card.valid_from is not None
            and self._parse_timestamp(card.valid_from, "valid_from") > moment
        ]
        verification = self.verify()
        return MemoryStoreHealth(
            as_of=moment.isoformat(),
            verification_ok=verification.ok,
            cards=len(cards),
            active_cards=len(active),
            inactive_cards=len(cards) - len(active),
            expired_active_cards=len(expired),
            future_active_cards=len(future),
            scopes=len({(card.scope, card.scope_key) for card in cards}),
            database_bytes=self.database_path.stat().st_size,
            verification_errors=verification.errors,
        )

    def _connect(self, *, write: bool = False) -> sqlite3.Connection:
        if write:
            self._require_writable()
            self._ensure_private_directory()
            self._ensure_private_database_file()
        elif self.directory.exists() or self.directory.is_symlink():
            self._validate_existing_directory()
        if not self.database_path.exists():
            if not write:
                raise MemoryNotInitializedError("memory store is not initialized")
        else:
            self._validate_private_file(self.database_path, "memory database")
            if self.database_path.stat().st_size > MAX_STATE_FILE_BYTES:
                raise MemoryIntegrityError(
                    "memory database exceeds the state size limit"
                )
        if write:
            connection = sqlite3.connect(self.database_path, timeout=5)
        else:
            encoded_path = quote(self.database_path.resolve().as_posix(), safe="/")
            uri = f"file:{encoded_path}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if write:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            if os.name != "nt" and self.database_path.exists():
                self.database_path.chmod(0o600)
        return connection

    def _ensure_private_database_file(self) -> None:
        """Create the SQLite path without a world-readable creation window."""

        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.database_path, flags, 0o600)
        except FileExistsError:
            self._validate_private_file(self.database_path, "memory database")
            return
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._validate_private_file(self.database_path, "memory database")

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                signature TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cards (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                subtype TEXT NOT NULL,
                scope TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                value TEXT NOT NULL,
                abstraction TEXT NOT NULL,
                cue_anchors_json TEXT NOT NULL,
                origin TEXT NOT NULL,
                authority TEXT NOT NULL,
                confidence REAL NOT NULL,
                importance REAL NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                recorded_at_ns INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL,
                signature TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                card_id TEXT NOT NULL REFERENCES cards(id),
                source_type TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,
                origin TEXT NOT NULL,
                recorded_at_ns INTEGER NOT NULL,
                signature TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS edges (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES cards(id),
                target_id TEXT NOT NULL REFERENCES cards(id),
                relation TEXT NOT NULL,
                recorded_at_ns INTEGER NOT NULL,
                signature TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS card_events (
                id TEXT PRIMARY KEY,
                card_id TEXT NOT NULL REFERENCES cards(id),
                event_type TEXT NOT NULL,
                related_card_id TEXT REFERENCES cards(id),
                recorded_at_ns INTEGER NOT NULL,
                signature TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_decisions (
                id TEXT PRIMARY KEY,
                query_sha256 TEXT NOT NULL,
                stage TEXT NOT NULL,
                operation TEXT NOT NULL,
                selected_card_ids_json TEXT NOT NULL,
                expected_utility REAL NOT NULL,
                reason TEXT NOT NULL,
                shadow INTEGER NOT NULL,
                recorded_at_ns INTEGER NOT NULL,
                signature TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_outcomes (
                id TEXT PRIMARY KEY,
                decision_id TEXT NOT NULL UNIQUE REFERENCES memory_decisions(id),
                success INTEGER NOT NULL,
                reward REAL NOT NULL,
                harmful INTEGER NOT NULL,
                token_cost INTEGER NOT NULL,
                evidence_type TEXT NOT NULL,
                evidence_ref TEXT NOT NULL,
                evidence_sha256 TEXT NOT NULL,
                evidence_origin TEXT NOT NULL,
                recorded_at_ns INTEGER NOT NULL,
                signature TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sources_card_id ON sources(card_id);
            CREATE INDEX IF NOT EXISTS edges_source_id ON edges(source_id);
            CREATE INDEX IF NOT EXISTS edges_target_id ON edges(target_id);
            CREATE INDEX IF NOT EXISTS events_card_time ON card_events(card_id, recorded_at_ns);
            CREATE INDEX IF NOT EXISTS memory_outcomes_decision ON memory_outcomes(decision_id);
            """
        )
        existing = self._meta(connection, "schema_version")
        if existing is not None and int(existing) not in READABLE_SCHEMA_VERSIONS:
            raise MemoryIntegrityError("unsupported memory schema version")
        self._set_meta(connection, "schema_version", str(SCHEMA_VERSION))
        fts_enabled = True
        try:
            connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
                USING fts5(card_id UNINDEXED, abstraction, cue_text, tokenize='unicode61')
                """
            )
        except sqlite3.OperationalError:
            fts_enabled = False
        self._set_meta(connection, "fts_enabled", "1" if fts_enabled else "0")
        connection.commit()

    def _insert_card(self, connection: sqlite3.Connection, **values: Any) -> MemoryCard:
        card_id = uuid.uuid4().hex
        recorded_at_ns = time.time_ns()
        cue_anchors = tuple(str(item).strip() for item in values.pop("cue_anchors"))
        payload = {
            "id": card_id,
            **values,
            "cue_anchors": list(cue_anchors),
            "recorded_at_ns": recorded_at_ns,
        }
        content_sha256 = digest_json(payload)
        signed_payload = {**payload, "content_sha256": content_sha256}
        signature = self._sign(signed_payload)
        connection.execute(
            """
            INSERT INTO cards(
                id, kind, subtype, scope, scope_key, value, abstraction,
                cue_anchors_json, origin, authority, confidence, importance,
                valid_from, valid_to, recorded_at_ns, content_sha256, signature
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card_id,
                values["kind"],
                values["subtype"],
                values["scope"],
                values["scope_key"],
                values["value"],
                values["abstraction"],
                json.dumps(cue_anchors, ensure_ascii=False, separators=(",", ":")),
                values["origin"],
                values["authority"],
                values["confidence"],
                values["importance"],
                values["valid_from"],
                values["valid_to"],
                recorded_at_ns,
                content_sha256,
                signature,
            ),
        )
        self._insert_event(connection, card_id, "active", None)
        if self._meta(connection, "fts_enabled") == "1":
            connection.execute(
                "INSERT INTO memory_fts(card_id, abstraction, cue_text) VALUES(?, ?, ?)",
                (card_id, values["abstraction"], " ".join(cue_anchors)),
            )
        row = connection.execute(
            "SELECT * FROM cards WHERE id = ?", (card_id,)
        ).fetchone()
        return self._card_from_row(row, "active")

    def _insert_source(
        self, connection: sqlite3.Connection, card_id: str, source: EvidenceSource
    ) -> EvidenceSource:
        if source.origin not in MEMORY_ORIGINS:
            raise ValueError(f"unsupported evidence origin: {source.origin}")
        if not source.source_type.strip() or not source.source_ref.strip():
            raise ValueError("evidence source type and reference must not be blank")
        if len(source.source_type) > 200 or len(source.source_ref) > 10_000:
            raise ValueError("evidence source metadata exceeds the size limit")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", source.source_sha256):
            raise ValueError("evidence source_sha256 must be a SHA-256 hex digest")
        payload = {
            "id": uuid.uuid4().hex,
            "card_id": card_id,
            "source_type": source.source_type,
            "source_ref": source.source_ref,
            "source_sha256": source.source_sha256.lower(),
            "origin": source.origin,
            "recorded_at_ns": time.time_ns(),
        }
        connection.execute(
            """
            INSERT INTO sources(id, card_id, source_type, source_ref, source_sha256,
                                origin, recorded_at_ns, signature)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*payload.values(), self._sign(payload)),
        )
        return EvidenceSource(**payload)

    def _insert_edge(
        self,
        connection: sqlite3.Connection,
        source_id: str,
        target_id: str,
        relation: str,
    ) -> MemoryEdge:
        if relation not in MEMORY_RELATIONS:
            raise ValueError(f"unsupported memory relation: {relation}")
        self._require_authenticated_card(connection, source_id)
        self._require_authenticated_card(connection, target_id)
        payload = {
            "id": uuid.uuid4().hex,
            "source_id": source_id,
            "target_id": target_id,
            "relation": relation,
            "recorded_at_ns": time.time_ns(),
        }
        connection.execute(
            """
            INSERT INTO edges(id, source_id, target_id, relation, recorded_at_ns, signature)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (*payload.values(), self._sign(payload)),
        )
        return MemoryEdge(**payload)

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        card_id: str,
        event_type: str,
        related_card_id: str | None,
    ) -> None:
        if event_type not in MEMORY_STATUSES:
            raise ValueError(f"unsupported memory status: {event_type}")
        payload = {
            "id": uuid.uuid4().hex,
            "card_id": card_id,
            "event_type": event_type,
            "related_card_id": related_card_id,
            "recorded_at_ns": time.time_ns(),
        }
        connection.execute(
            """
            INSERT INTO card_events(id, card_id, event_type, related_card_id,
                                    recorded_at_ns, signature)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (*payload.values(), self._sign(payload)),
        )

    def _search_rows(
        self, connection: sqlite3.Connection, query: str, candidate_limit: int
    ) -> list[tuple[sqlite3.Row, float]]:
        if self._meta(connection, "fts_enabled") == "1":
            tokens = re.findall(r"\w+", query, flags=re.UNICODE)
            expression = " OR ".join(
                f'"{item.replace(chr(34), "")}"' for item in tokens
            )
            if expression:
                matches = connection.execute(
                    """
                    SELECT c.*, bm25(memory_fts) AS rank
                    FROM memory_fts JOIN cards AS c ON c.id = memory_fts.card_id
                    WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?
                    """,
                    (expression, candidate_limit),
                ).fetchall()
                return [(row, -float(row["rank"])) for row in matches]
        fallback_tokens = re.findall(r"\w+", query, flags=re.UNICODE)[:32] or [query]
        clauses = []
        parameters: list[Any] = []
        for token in fallback_tokens:
            escaped_token = token.replace("%", "\\%").replace("_", "\\_")
            pattern = "%" + escaped_token + "%"
            clauses.append(
                "(abstraction LIKE ? ESCAPE '\\' OR cue_anchors_json LIKE ? ESCAPE '\\')"
            )
            parameters.extend((pattern, pattern))
        parameters.append(candidate_limit)
        matches = connection.execute(
            "SELECT * FROM cards WHERE "
            + " OR ".join(clauses)
            + " ORDER BY importance DESC, recorded_at_ns DESC LIMIT ?",
            parameters,
        ).fetchall()
        return [(row, float(row["importance"])) for row in matches]

    def _current_status(
        self, connection: sqlite3.Connection, card_id: str, key: bytes
    ) -> str:
        row = connection.execute(
            """
            SELECT rowid, * FROM card_events WHERE card_id = ?
            ORDER BY recorded_at_ns DESC, rowid DESC LIMIT 1
            """,
            (card_id,),
        ).fetchone()
        if row is None:
            raise MemoryIntegrityError(f"memory card {card_id} has no state event")
        self._verify_row("event", row, self._event_payload(row), key)
        return str(row["event_type"])

    def _latest_statuses(
        self,
        connection: sqlite3.Connection,
        card_ids: Sequence[str],
        key: bytes,
    ) -> dict[str, str]:
        """Load latest authenticated state events without one query per card."""

        statuses: dict[str, str] = {}
        for offset in range(0, len(card_ids), 500):
            chunk = tuple(card_ids[offset : offset + 500])
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                "SELECT rowid, * FROM card_events WHERE card_id IN ("
                + placeholders
                + ") ORDER BY card_id, recorded_at_ns DESC, rowid DESC",
                chunk,
            ).fetchall()
            for row in rows:
                card_id = str(row["card_id"])
                if card_id in statuses:
                    continue
                self._verify_row("event", row, self._event_payload(row), key)
                statuses[card_id] = str(row["event_type"])
        missing = set(card_ids) - statuses.keys()
        if missing:
            raise MemoryIntegrityError(f"memory card {min(missing)} has no state event")
        return statuses

    def _verify_references(
        self,
        connection: sqlite3.Connection,
        errors: list[str],
        *,
        include_control: bool,
    ) -> None:
        checks = (
            ("sources", "card_id"),
            ("edges", "source_id"),
            ("edges", "target_id"),
            ("card_events", "card_id"),
            ("card_events", "related_card_id"),
        )
        for table, column in checks:
            count = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) FROM {table} AS child
                    LEFT JOIN cards AS card ON card.id = child.{column}
                    WHERE child.{column} IS NOT NULL AND card.id IS NULL
                    """
                ).fetchone()[0]
            )
            if count:
                errors.append(f"{table}.{column} has {count} broken reference(s)")
        if not include_control:
            self._verify_card_coverage(connection, errors)
            return
        missing_decisions = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM memory_outcomes AS outcome
                LEFT JOIN memory_decisions AS decision
                  ON decision.id = outcome.decision_id
                WHERE decision.id IS NULL
                """
            ).fetchone()[0]
        )
        if missing_decisions:
            errors.append(
                f"memory_outcomes.decision_id has {missing_decisions} broken reference(s)"
            )
        existing_cards = {
            str(row[0]) for row in connection.execute("SELECT id FROM cards")
        }
        broken_selected = 0
        for row in connection.execute(
            "SELECT selected_card_ids_json FROM memory_decisions"
        ):
            try:
                selected = json.loads(row[0])
            except (TypeError, json.JSONDecodeError):
                broken_selected += 1
                continue
            if not isinstance(selected, list):
                broken_selected += 1
                continue
            broken_selected += sum(
                1 for card_id in selected if str(card_id) not in existing_cards
            )
        if broken_selected:
            errors.append(
                f"memory_decisions selected cards have {broken_selected} broken reference(s)"
            )
        self._verify_card_coverage(connection, errors)

    @staticmethod
    def _verify_card_coverage(
        connection: sqlite3.Connection, errors: list[str]
    ) -> None:
        missing_events = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM cards AS c
                WHERE NOT EXISTS (
                    SELECT 1 FROM card_events AS event WHERE event.card_id = c.id
                )
                """
            ).fetchone()[0]
        )
        if missing_events:
            errors.append(f"{missing_events} memory card(s) have no state event")
        missing_sources = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM cards AS c
                WHERE NOT EXISTS (
                    SELECT 1 FROM sources AS source WHERE source.card_id = c.id
                )
                """
            ).fetchone()[0]
        )
        if missing_sources:
            errors.append(f"{missing_sources} memory card(s) have no evidence source")

    def _verify_fts(self, connection: sqlite3.Connection, errors: list[str]) -> None:
        rows = connection.execute(
            """
            SELECT c.id, c.abstraction, c.cue_anchors_json,
                   f.abstraction AS indexed_abstraction, f.cue_text
            FROM cards AS c LEFT JOIN memory_fts AS f ON f.card_id = c.id
            """
        ).fetchall()
        for row in rows:
            expected_cues = " ".join(json.loads(row["cue_anchors_json"]))
            if (
                row["indexed_abstraction"] != row["abstraction"]
                or row["cue_text"] != expected_cues
            ):
                errors.append(f"memory FTS index mismatch: {row['id']}")
        extra = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM memory_fts AS f
                LEFT JOIN cards AS c ON c.id = f.card_id WHERE c.id IS NULL
                """
            ).fetchone()[0]
        )
        if extra:
            errors.append(f"memory FTS index has {extra} orphan row(s)")

    def _card_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "subtype": row["subtype"],
            "scope": row["scope"],
            "scope_key": row["scope_key"],
            "value": row["value"],
            "abstraction": row["abstraction"],
            "origin": row["origin"],
            "authority": row["authority"],
            "confidence": row["confidence"],
            "importance": row["importance"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "cue_anchors": json.loads(row["cue_anchors_json"]),
            "recorded_at_ns": row["recorded_at_ns"],
            "content_sha256": row["content_sha256"],
        }

    @staticmethod
    def _source_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            key: row[key]
            for key in (
                "id",
                "card_id",
                "source_type",
                "source_ref",
                "source_sha256",
                "origin",
                "recorded_at_ns",
            )
        }

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> EvidenceSource:
        return EvidenceSource(
            id=row["id"],
            card_id=row["card_id"],
            source_type=row["source_type"],
            source_ref=row["source_ref"],
            source_sha256=row["source_sha256"],
            origin=row["origin"],
            recorded_at_ns=int(row["recorded_at_ns"]),
        )

    @staticmethod
    def _decision_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "query_sha256": row["query_sha256"],
            "stage": row["stage"],
            "operation": row["operation"],
            "selected_card_ids": json.loads(row["selected_card_ids_json"]),
            "expected_utility": float(row["expected_utility"]),
            "reason": row["reason"],
            "shadow": bool(row["shadow"]),
            "recorded_at_ns": int(row["recorded_at_ns"]),
        }

    @staticmethod
    def _decision_from_row(row: sqlite3.Row) -> MemoryDecisionRecord:
        return MemoryDecisionRecord(
            id=row["id"],
            query_sha256=row["query_sha256"],
            stage=row["stage"],
            operation=row["operation"],
            selected_card_ids=tuple(json.loads(row["selected_card_ids_json"])),
            expected_utility=float(row["expected_utility"]),
            reason=row["reason"],
            shadow=bool(row["shadow"]),
            recorded_at_ns=int(row["recorded_at_ns"]),
        )

    @staticmethod
    def _outcome_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "decision_id": row["decision_id"],
            "success": bool(row["success"]),
            "reward": float(row["reward"]),
            "harmful": bool(row["harmful"]),
            "token_cost": int(row["token_cost"]),
            "evidence_type": row["evidence_type"],
            "evidence_ref": row["evidence_ref"],
            "evidence_sha256": row["evidence_sha256"],
            "evidence_origin": row["evidence_origin"],
            "recorded_at_ns": int(row["recorded_at_ns"]),
        }

    @staticmethod
    def _outcome_from_row(row: sqlite3.Row) -> MemoryOutcomeRecord:
        return MemoryOutcomeRecord(**SQLiteMemoryStore._outcome_payload(row))

    @staticmethod
    def _edge_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            key: row[key]
            for key in (
                "id",
                "source_id",
                "target_id",
                "relation",
                "recorded_at_ns",
            )
        }

    @staticmethod
    def _event_payload(row: sqlite3.Row) -> dict[str, Any]:
        return {
            key: row[key]
            for key in (
                "id",
                "card_id",
                "event_type",
                "related_card_id",
                "recorded_at_ns",
            )
        }

    def _verify_row(
        self, label: str, row: sqlite3.Row, payload: dict[str, Any], key: bytes
    ) -> None:
        if label == "card":
            content_payload = dict(payload)
            supplied_digest = str(content_payload.pop("content_sha256"))
            if not hmac.compare_digest(supplied_digest, digest_json(content_payload)):
                raise MemoryIntegrityError(f"memory card digest mismatch: {row['id']}")
        expected = hmac.new(key, canonical_json(payload), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(str(row["signature"]), expected):
            raise MemoryIntegrityError(
                f"memory {label} authentication failed: {row['id']}"
            )

    def _card_from_row(self, row: sqlite3.Row, status: str) -> MemoryCard:
        return MemoryCard(
            id=row["id"],
            kind=row["kind"],
            subtype=row["subtype"],
            scope=row["scope"],
            scope_key=row["scope_key"],
            value=row["value"],
            abstraction=row["abstraction"],
            cue_anchors=tuple(json.loads(row["cue_anchors_json"])),
            origin=row["origin"],
            authority=row["authority"],
            confidence=float(row["confidence"]),
            importance=float(row["importance"]),
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            recorded_at_ns=int(row["recorded_at_ns"]),
            content_sha256=row["content_sha256"],
            status=status,
        )

    def _sign(self, payload: dict[str, Any]) -> str:
        return hmac.new(
            self._load_or_create_key(), canonical_json(payload), hashlib.sha256
        ).hexdigest()

    def _load_or_create_key(self) -> bytes:
        self._ensure_private_directory()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.key_path, flags, 0o600)
        except FileExistsError:
            return self._load_existing_key()
        key = secrets.token_bytes(32)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return key

    def _load_existing_key(self) -> bytes:
        self._validate_private_file(self.key_path, "memory authentication key")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.key_path, flags)
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_size != 32:
                raise MemoryIntegrityError("memory authentication key is invalid")
            key = os.read(descriptor, 33)
        finally:
            os.close(descriptor)
        if len(key) != 32:
            raise MemoryIntegrityError("memory authentication key is invalid")
        return key

    def _ensure_private_directory(self) -> None:
        if self.directory.is_symlink():
            raise MemoryIntegrityError(
                f"memory state directory must not be a symlink: {self.directory}"
            )
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = self.directory.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise MemoryIntegrityError("memory state path is not a real directory")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("memory state directory is not owned by this user")
        if os.name != "nt":
            self.directory.chmod(0o700)

    def _validate_existing_directory(self) -> None:
        try:
            metadata = self.directory.lstat()
        except OSError as exc:
            raise MemoryIntegrityError(
                f"could not inspect memory state directory: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise MemoryIntegrityError(
                "memory state directory must be a real directory"
            )
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("memory state directory is not owned by this user")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise PermissionError("memory state directory permissions are too broad")

    @staticmethod
    def _validate_private_file(path: Path, label: str) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise MemoryIntegrityError(f"could not inspect {label}: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise MemoryIntegrityError(f"{label} must be a regular file")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError(f"{label} is not owned by this user")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise PermissionError(f"{label} permissions are too broad")

    def _validate_card_input(self, **values: Any) -> None:
        if values["kind"] not in MEMORY_KINDS:
            raise ValueError(f"unsupported memory kind: {values['kind']}")
        validate_authority(values["origin"], values["authority"])
        for field in ("value", "abstraction", "subtype", "scope"):
            raw = values[field]
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError(f"memory {field} must not be blank")
            if len(raw) > MAX_TEXT_LENGTH:
                raise ValueError(f"memory {field} exceeds the size limit")
        if len(values["scope_key"]) > MAX_TEXT_LENGTH:
            raise ValueError("memory scope_key exceeds the size limit")
        if len(values["cue_anchors"]) > 100:
            raise ValueError("memory cue anchors exceed the count limit")
        for cue in values["cue_anchors"]:
            if not isinstance(cue, str) or not cue.strip() or len(cue) > 500:
                raise ValueError("memory cue anchors must be non-blank short strings")
        for field in ("confidence", "importance"):
            number = values[field]
            if (
                not isinstance(number, (float, int))
                or isinstance(number, bool)
                or not 0 <= number <= 1
            ):
                raise ValueError(f"memory {field} must be between 0 and 1")
        valid_from, valid_to = values["valid_from"], values["valid_to"]
        parsed_from = self._parse_timestamp(valid_from, "valid_from")
        parsed_to = self._parse_timestamp(valid_to, "valid_to")
        if (
            parsed_from is not None
            and parsed_to is not None
            and parsed_from > parsed_to
        ):
            raise ValueError("valid_from must not be after valid_to")

    def _card_authority(self, connection: sqlite3.Connection, card_id: str) -> str:
        row = self._require_authenticated_card(connection, card_id)
        return str(row["authority"])

    def _require_authenticated_card(
        self, connection: sqlite3.Connection, card_id: str
    ) -> sqlite3.Row:
        row = self._require_card(connection, card_id)
        self._verify_row(
            "card", row, self._card_payload(row), self._load_existing_key()
        )
        return row

    @staticmethod
    def _signed_text_matches_query(row: sqlite3.Row, query: str) -> bool:
        tokens = [item.casefold() for item in re.findall(r"\w+", query, re.UNICODE)]
        signed_text = " ".join(
            [row["abstraction"], *json.loads(row["cue_anchors_json"])]
        ).casefold()
        return not tokens or any(token in signed_text for token in tokens)

    @staticmethod
    def _require_card(connection: sqlite3.Connection, card_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM cards WHERE id = ?", (card_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"memory card not found: {card_id}")
        return row

    def _meta(self, connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute(
            "SELECT key, value, signature FROM meta WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        payload = {"key": str(row["key"]), "value": str(row["value"])}
        expected = hmac.new(
            self._load_existing_key(), canonical_json(payload), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(str(row["signature"]), expected):
            raise MemoryIntegrityError(f"memory metadata authentication failed: {key}")
        return str(row["value"])

    def _set_meta(self, connection: sqlite3.Connection, key: str, value: str) -> None:
        payload = {"key": key, "value": value}
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value, signature) VALUES(?, ?, ?)",
            (key, value, self._sign(payload)),
        )

    def _schema_version(self, connection: sqlite3.Connection) -> int:
        value = self._meta(connection, "schema_version")
        if value is None or int(value) not in READABLE_SCHEMA_VERSIONS:
            raise MemoryIntegrityError("unsupported memory schema version")
        return int(value)

    @staticmethod
    def _parse_timestamp(value: str | None, label: str) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a non-blank timestamp")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{label} must include a UTC offset")
        return parsed

    def _require_writable(self) -> None:
        if self.read_only:
            raise PermissionError("memory store is read-only")


MemoryStore = SQLiteMemoryStore
