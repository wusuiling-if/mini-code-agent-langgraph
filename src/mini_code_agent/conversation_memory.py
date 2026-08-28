"""Opt-in, evidence-bound long-term memory for interactive MCA chats.

The chat runtime keeps raw conversation events as the source of truth and uses
the existing authenticated memory store for durable, explicitly approved
facts.  Heuristic extraction can only stage candidates; it never writes a
durable card without a user command.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from memory_core.conversation import ConversationEvent
from memory_core.contracts import SemanticCandidateProvider
from memory_core.rendering import ContextBudget
from memory_core.security import SecretDetector
from mini_code_agent.conversation_ledger import (
    AuthenticatedConversationLedger,
    ConversationLedgerError,
)
from mini_code_agent.locking import exclusive_file_lock
from mini_code_agent.memory_adapters.project import GitProjectIdentityProvider
from mini_code_agent.memory_models import EvidenceSource, MemoryCard
from mini_code_agent.memory_retrieval import (
    SCENARIO_POLICIES,
    EvidenceTemporalRetriever,
    MemoryQuery,
    MemoryScope,
    lexical_tokens,
)
from mini_code_agent.memory_store import SQLiteMemoryStore

MAX_EVENT_CONTENT_CHARS = 1_000_000
MAX_CANDIDATE_CHARS = 2_000
USER_IDENTITY_NAME = "user.identity"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_user_identity(path: Path) -> str:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ConversationLedgerError("user identity must be a regular file")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise PermissionError("user identity is not owned by this user")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PermissionError("user identity permissions are too broad")
    identity = path.read_text(encoding="ascii")
    if not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise ConversationLedgerError("user identity is invalid")
    return identity


@dataclass(frozen=True)
class ConversationMemoryCandidate:
    candidate_id: str
    value: str
    source_ref: str
    source_sha256: str
    source_event_id: str
    created_at: str
    scope: str = "user"


@dataclass(frozen=True)
class ConversationMemoryVerification:
    ok: bool
    checked_logs: int
    checked_events: int
    checked_candidates: int
    checked_sources: int
    errors: tuple[str, ...] = ()


class MemorySelectionError(ValueError):
    """A forget/correct selector did not resolve to exactly one memory."""

    def __init__(self, message: str, matches: tuple[MemoryCard, ...] = ()) -> None:
        super().__init__(message)
        self.matches = matches


def list_conversation_memories(
    state_root: Path, workspace: Path
) -> tuple[MemoryCard, ...]:
    """List user/current-workspace cards without initializing or mutating state."""

    state_root = Path(state_root).expanduser().resolve()
    workspace = Path(workspace).expanduser().resolve()
    store = SQLiteMemoryStore(state_root / "memory", read_only=True)
    if not store.initialized:
        return ()
    scope_pairs: list[tuple[str, str]] = []
    identity_path = store.directory / "conversations" / USER_IDENTITY_NAME
    try:
        identity = _read_user_identity(identity_path)
    except FileNotFoundError:
        pass
    else:
        user_identity = hashlib.sha256(
            f"mca-user:{identity}".encode("ascii")
        ).hexdigest()
        scope_pairs.append(("user", f"sha256:{user_identity}"))
    try:
        workspace_identity = GitProjectIdentityProvider().identity_sha256(
            workspace, create=False
        )
    except RuntimeError:
        workspace_identity = hashlib.sha256(
            f"mca-workspace:{workspace}".encode("utf-8")
        ).hexdigest()
    scope_pairs.append(("workspace", f"sha256:{workspace_identity}"))
    return store.list_cards(scope_pairs=tuple(scope_pairs), include_global=False)


def verify_conversation_memory(
    memory_directory: Path,
) -> ConversationMemoryVerification:
    """Verify every conversation chain and bind card sources back to raw events."""

    directory = Path(memory_directory).expanduser()
    events_directory = directory / "conversations"
    if not events_directory.exists():
        store = SQLiteMemoryStore(directory, read_only=True)
        if store.initialized:
            for card in store.list_cards(include_inactive=True):
                if any(
                    source.source_type
                    in {"conversation_event", "conversation_forget_event"}
                    for source in store.sources(card.id)
                ):
                    return ConversationMemoryVerification(
                        False,
                        0,
                        0,
                        0,
                        0,
                        ("conversation evidence directory is missing",),
                    )
        return ConversationMemoryVerification(True, 0, 0, 0, 0)
    errors: list[str] = []
    checked_logs = 0
    checked_events = 0
    checked_candidates = 0
    checked_sources = 0
    event_revisions: dict[str, str] = {}
    candidate_sources: dict[str, tuple[str, str]] = {}
    candidate_decisions: dict[str, dict[str, object]] = {}
    try:
        ledger = AuthenticatedConversationLedger(events_directory, create=False)
        if not ledger.key:
            raise ConversationLedgerError("conversation authentication key is missing")
        identity_path = events_directory / USER_IDENTITY_NAME
        if any(events_directory.glob("*.jsonl")):
            try:
                _read_user_identity(identity_path)
            except FileNotFoundError as exc:
                raise ConversationLedgerError("user identity is missing") from exc
        for path in sorted(events_directory.glob("*.jsonl")):
            if path.name == "candidates.jsonl":
                continue
            checked_logs += 1
            rows = ledger.read(path, f"events:{path.stem}")
            for index, row in enumerate(rows):
                LocalConversationMemory._validate_event_payload(row)
                event = ConversationEvent(**cast(Any, row))
                if event.conversation_id != path.stem or event.sequence != index:
                    raise ConversationLedgerError(
                        f"conversation event binding mismatch: {path.name}:{index}"
                    )
                previous = event_revisions.setdefault(
                    event.source_ref, event.source_sha256
                )
                if previous != event.source_sha256:
                    raise ConversationLedgerError(
                        f"conversation source reference is ambiguous: {event.source_ref}"
                    )
                checked_events += 1

        candidates_path = events_directory / "candidates.jsonl"
        if candidates_path.exists():
            checked_logs += 1
            for row in ledger.read(candidates_path, "candidates"):
                LocalConversationMemory._validate_candidate_payload(row)
                if row.get("record_type") == "candidate":
                    checked_candidates += 1
                    candidate_id = str(row.get("candidate_id", ""))
                    source_ref = str(row.get("source_ref", ""))
                    source_sha256 = str(row.get("source_sha256", ""))
                    if event_revisions.get(source_ref) != source_sha256:
                        raise ConversationLedgerError(
                            f"candidate evidence does not match an event: {source_ref}"
                        )
                    candidate_sources[candidate_id] = (source_ref, source_sha256)
                else:
                    candidate_id = str(row.get("candidate_id", ""))
                    if candidate_id in candidate_decisions:
                        raise ConversationLedgerError(
                            f"candidate has multiple terminal decisions: {candidate_id}"
                        )
                    candidate_decisions[candidate_id] = row

        store = SQLiteMemoryStore(directory, read_only=True)
        cards_by_id: dict[str, MemoryCard] = {}
        if store.initialized:
            for card in store.list_cards(include_inactive=True):
                cards_by_id[card.id] = card
                for source in store.sources(card.id):
                    if source.source_type not in {
                        "conversation_event",
                        "conversation_forget_event",
                    }:
                        continue
                    checked_sources += 1
                    if event_revisions.get(source.source_ref) != source.source_sha256:
                        errors.append(
                            "memory source does not match authenticated conversation "
                            f"evidence: {card.id}:{source.source_ref}"
                        )
        for candidate_id, decision in candidate_decisions.items():
            if candidate_id not in candidate_sources:
                errors.append(f"candidate decision has no candidate: {candidate_id}")
                continue
            kind = str(decision.get("decision", ""))
            if kind not in {"approved", "dismissed"}:
                errors.append(f"candidate decision is invalid: {candidate_id}")
                continue
            card_id = str(decision.get("card_id", ""))
            if kind == "dismissed":
                if card_id:
                    errors.append(
                        f"dismissed candidate unexpectedly names a card: {candidate_id}"
                    )
                continue
            approved_card = cards_by_id.get(card_id)
            if approved_card is None:
                errors.append(
                    f"approved candidate card is missing: {candidate_id}:{card_id}"
                )
                continue
            expected_ref, expected_sha256 = candidate_sources[candidate_id]
            sources = store.sources(approved_card.id)
            if not any(
                source.source_ref == expected_ref
                and source.source_sha256 == expected_sha256
                for source in sources
            ):
                errors.append(
                    f"approved candidate card has wrong evidence: {candidate_id}"
                )
    except (OSError, TypeError, ValueError, ConversationLedgerError) as exc:
        errors.append(str(exc))
    return ConversationMemoryVerification(
        ok=not errors,
        checked_logs=checked_logs,
        checked_events=checked_events,
        checked_candidates=checked_candidates,
        checked_sources=checked_sources,
        errors=tuple(errors),
    )


class LocalConversationMemory:
    """Bridge an MCA chat session to the local evidence-bound memory store."""

    def __init__(
        self,
        state_root: Path,
        workspace: Path,
        session_path: Path,
        *,
        store: SQLiteMemoryStore | None = None,
        semantic_provider: SemanticCandidateProvider | None = None,
    ) -> None:
        self.state_root = Path(state_root).expanduser().resolve()
        self.workspace = Path(workspace).expanduser().resolve()
        self.store = store or SQLiteMemoryStore(self.state_root / "memory")
        if self.store.read_only:
            raise ValueError("conversation memory requires a writable store")
        self.store.initialize()
        self.workspace_key = f"sha256:{self._workspace_identity()}"
        self.semantic_provider = semantic_provider
        self.conversation_id = _digest(
            {"host": "mca-chat", "session_path": str(Path(session_path).resolve())}
        )
        self.events_directory = self.store.directory / "conversations"
        self.ledger = AuthenticatedConversationLedger(
            self.events_directory, create=True
        )
        self.user_key = f"sha256:{self._user_identity()}"
        self.events_path = self.events_directory / f"{self.conversation_id}.jsonl"
        self.candidates_path = self.events_directory / "candidates.jsonl"
        self.lock_path = self.ledger.lock_path
        self.ledger.migrate_legacy(
            self.events_path,
            self._event_log_name,
            self._validate_event_payload,
        )
        self.ledger.migrate_legacy(
            self.candidates_path,
            "candidates",
            self._validate_candidate_payload,
        )
        self._next_sequence = len(self._read_event_rows())

    @property
    def _event_log_name(self) -> str:
        return f"events:{self.conversation_id}"

    def record_event(
        self,
        role: str,
        content: str,
        *,
        metadata: dict[str, object] | None = None,
    ) -> ConversationEvent:
        """Append one immutable raw event revision and return its evidence handle."""

        if not isinstance(content, str) or len(content) > MAX_EVENT_CONTENT_CHARS:
            raise ValueError("conversation event content is invalid or too large")
        with exclusive_file_lock(self.lock_path):
            persisted_next = self._persisted_next_sequence()
            if persisted_next < self._next_sequence:
                raise ValueError("conversation event log was truncated while in use")
            sequence = persisted_next
            source_ref = f"state:conversation-event:{self.conversation_id}:{sequence}"
            event = ConversationEvent.create(
                host_id="mca-chat",
                conversation_id=self.conversation_id,
                source_ref=source_ref,
                sequence=sequence,
                role=role,
                content=content,
                participant_id="user" if role == "user" else "mca",
                created_at=_utc_now(),
                metadata=metadata,
            )
            self.ledger.append_unlocked(
                self.events_path, self._event_log_name, asdict(event)
            )
            self._next_sequence = sequence + 1
        return event

    def remember(
        self,
        value: str,
        source_event: ConversationEvent,
        *,
        scope: str = "workspace",
    ) -> MemoryCard:
        """Persist a user-approved memory bound to its exact chat event."""

        return self._remember_from_source(
            value,
            source_ref=source_event.source_ref,
            source_sha256=source_event.source_sha256,
            valid_from=source_event.created_at,
            scope=scope,
        )

    def remember_candidate(
        self, selector: str, *, scope: str | None = None
    ) -> MemoryCard:
        candidate = self._resolve_candidate(selector)
        card = self._remember_from_source(
            candidate.value,
            source_ref=candidate.source_ref,
            source_sha256=candidate.source_sha256,
            valid_from=candidate.created_at,
            scope=scope or candidate.scope,
        )
        self._append_candidate_decision(candidate.candidate_id, "approved", card.id)
        return card

    def forget(self, selector: str, source_event: ConversationEvent) -> MemoryCard:
        """Tombstone one selected memory while retaining the audit trail."""

        card = self.resolve_card(selector)
        self.store.add_source(
            card.id,
            self._event_source(source_event, source_type="conversation_forget_event"),
        )
        self.store.transition(card.id, "tombstoned")
        return card

    def correct(
        self,
        selector: str,
        replacement: str,
        source_event: ConversationEvent,
    ) -> tuple[MemoryCard, MemoryCard]:
        """Supersede one selected memory with a source-bound correction."""

        old = self.resolve_card(selector)
        replacement = self._validate_memory_text(replacement)
        authority = "none" if old.authority == "none" else "inform"
        new = self.store.supersede(
            old.id,
            value=replacement,
            abstraction=replacement,
            cue_anchors=self._cue_anchors(replacement),
            kind=old.kind,
            subtype=old.subtype,
            scope=old.scope,
            scope_key=old.scope_key,
            origin="user",
            authority=authority,
            confidence=0.99,
            importance=max(old.importance, 0.9),
            valid_from=source_event.created_at,
            sources=(self._event_source(source_event),),
        )
        return old, new

    def recall(self, query: str) -> str:
        """Render bounded same-workspace advisory context for the next turn."""

        pack = EvidenceTemporalRetriever(
            self.store,
            policy=SCENARIO_POLICIES["personal_assistant"],
            semantic_provider=self.semantic_provider,
        ).retrieve(
            MemoryQuery(
                text=query,
                scopes=(
                    MemoryScope("user", self.user_key),
                    MemoryScope("workspace", self.workspace_key),
                ),
                required_authority="none",
                limit=4,
            )
        )
        return pack.render(
            ContextBudget(max_chars=5_000, max_item_chars=2_000, max_items=4)
        )

    def list_memories(self, query: str = "") -> tuple[MemoryCard, ...]:
        cards = self.store.list_cards(
            scope_pairs=(
                ("user", self.user_key),
                ("workspace", self.workspace_key),
            ),
            include_global=False,
        )
        if not query.strip():
            return cards
        normalized = query.casefold().strip()
        query_tokens = set(lexical_tokens(query))

        def score(card: MemoryCard) -> tuple[int, int, int]:
            text = f"{card.value} {card.abstraction} {' '.join(card.cue_anchors)}"
            exact = int(normalized in text.casefold())
            overlap = len(query_tokens.intersection(lexical_tokens(text)))
            return exact, overlap, card.recorded_at_ns

        matches = tuple(card for card in cards if score(card)[:2] != (0, 0))
        return tuple(sorted(matches, key=score, reverse=True))

    def resolve_card(self, selector: str) -> MemoryCard:
        selector = selector.strip().removeprefix("@")
        if not selector:
            raise MemorySelectionError("memory selector must not be blank")
        cards = self.list_memories()
        id_matches = tuple(card for card in cards if card.id.startswith(selector))
        if len(id_matches) == 1:
            return id_matches[0]
        if len(id_matches) > 1:
            raise MemorySelectionError("memory id prefix is ambiguous", id_matches)
        matches = self.list_memories(selector)
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise MemorySelectionError("no active same-workspace memory matched")
        raise MemorySelectionError(
            "memory selector matched multiple cards; use an id prefix", matches[:8]
        )

    def stage_candidate(
        self, source_event: ConversationEvent
    ) -> ConversationMemoryCandidate | None:
        """Stage one conservative heuristic candidate without admitting it."""

        value = self._candidate_value(source_event.content)
        if value is None or SecretDetector().contains_secret(value):
            return None
        payload = {
            "value": value,
            "source_ref": source_event.source_ref,
            "source_sha256": source_event.source_sha256,
            "source_event_id": source_event.event_id,
            "created_at": source_event.created_at,
            "scope": "user",
        }
        candidate = ConversationMemoryCandidate(
            candidate_id=_digest(payload), **payload
        )
        with exclusive_file_lock(self.lock_path):
            existing = {
                item.candidate_id
                for item in self.pending_candidates(include_decided=True)
            }
            if candidate.candidate_id not in existing:
                self.ledger.append_unlocked(
                    self.candidates_path,
                    "candidates",
                    {"record_type": "candidate", **asdict(candidate)},
                )
        return candidate

    def pending_candidates(
        self, *, include_decided: bool = False
    ) -> tuple[ConversationMemoryCandidate, ...]:
        candidates: dict[str, ConversationMemoryCandidate] = {}
        decided: set[str] = set()
        for row in self.ledger.read(self.candidates_path, "candidates"):
            if row.get("record_type") == "decision":
                decided.add(str(row.get("candidate_id", "")))
                continue
            if row.get("record_type") != "candidate":
                raise ValueError("unknown conversation candidate record")
            values = {
                key: str(row.get(key, ""))
                for key in (
                    "candidate_id",
                    "value",
                    "source_ref",
                    "source_sha256",
                    "source_event_id",
                    "created_at",
                )
            }
            scope = str(row.get("scope", "workspace"))
            candidate = ConversationMemoryCandidate(**values, scope=scope)
            expected = _digest(
                {
                    **{key: values[key] for key in values if key != "candidate_id"},
                    "scope": scope,
                }
            )
            if candidate.candidate_id != expected:
                legacy_expected = _digest(
                    {key: values[key] for key in values if key != "candidate_id"}
                )
                if candidate.candidate_id != legacy_expected:
                    raise ValueError("conversation memory candidate digest mismatch")
            candidates[candidate.candidate_id] = candidate
        return tuple(
            candidate
            for candidate in candidates.values()
            if include_decided or candidate.candidate_id not in decided
        )

    def dismiss_candidate(self, selector: str) -> ConversationMemoryCandidate:
        candidate = self._resolve_candidate(selector)
        self._append_candidate_decision(candidate.candidate_id, "dismissed", "")
        return candidate

    def _remember_from_source(
        self,
        value: str,
        *,
        source_ref: str,
        source_sha256: str,
        valid_from: str,
        scope: str,
    ) -> MemoryCard:
        value = self._validate_memory_text(value)
        if scope not in {"user", "workspace"}:
            raise ValueError("conversation memory scope must be user or workspace")
        scope_key = self.user_key if scope == "user" else self.workspace_key
        return self.store.add_card_once_for_source(
            source=EvidenceSource(
                source_type="conversation_event",
                source_ref=source_ref,
                source_sha256=source_sha256,
                origin="user",
            ),
            value=value,
            abstraction=value,
            cue_anchors=self._cue_anchors(value),
            kind="semantic",
            subtype="explicit_conversation_memory",
            scope=scope,
            scope_key=scope_key,
            origin="user",
            authority="inform",
            confidence=0.99,
            importance=0.9,
            valid_from=valid_from,
        )

    @staticmethod
    def _validate_memory_text(value: str) -> str:
        value = value.strip()
        if not value or len(value) > MAX_CANDIDATE_CHARS:
            raise ValueError("memory text must contain 1 to 2000 characters")
        if SecretDetector().contains_secret(value):
            raise ValueError("refusing to persist text that looks like a credential")
        return value

    @staticmethod
    def _cue_anchors(value: str) -> tuple[str, ...]:
        anchors = list(dict.fromkeys(lexical_tokens(value)))[:24]
        if len(value) <= 200:
            anchors.insert(0, value)
        return tuple(dict.fromkeys(anchor for anchor in anchors if anchor.strip()))

    @staticmethod
    def _event_source(
        event: ConversationEvent, *, source_type: str = "conversation_event"
    ) -> EvidenceSource:
        return EvidenceSource(
            source_type=source_type,
            source_ref=event.source_ref,
            source_sha256=event.source_sha256,
            origin="user" if event.role == "user" else "agent",
        )

    @staticmethod
    def _candidate_value(text: str) -> str | None:
        stripped = text.strip()
        if not stripped or len(stripped) > MAX_CANDIDATE_CHARS:
            return None
        patterns = (
            r"^(?:请)?记住[：,:\s]+(.+)$",
            r"^我(?:更)?(?:喜欢|偏好|习惯)[：,:\s]*(.+)$",
            r"^以后请[：,:\s]*(.+)$",
            r"^I\s+(?:prefer|like)\s+(.+)$",
            r"^Please\s+remember(?:\s+that)?\s+(.+)$",
        )
        for pattern in patterns:
            if re.match(pattern, stripped, flags=re.IGNORECASE):
                return stripped
        return None

    def _resolve_candidate(self, selector: str) -> ConversationMemoryCandidate:
        selector = selector.strip().removeprefix("@")
        matches = tuple(
            item
            for item in self.pending_candidates()
            if item.candidate_id.startswith(selector)
        )
        if len(matches) != 1:
            message = (
                "candidate not found" if not matches else "candidate id is ambiguous"
            )
            raise MemorySelectionError(message)
        return matches[0]

    def _append_candidate_decision(
        self, candidate_id: str, decision: str, card_id: str
    ) -> None:
        with exclusive_file_lock(self.lock_path):
            self.ledger.append_unlocked(
                self.candidates_path,
                "candidates",
                {
                    "record_type": "decision",
                    "candidate_id": candidate_id,
                    "decision": decision,
                    "card_id": card_id,
                    "recorded_at": _utc_now(),
                },
            )

    def _workspace_identity(self) -> str:
        try:
            return GitProjectIdentityProvider().identity_sha256(
                self.workspace, create=True
            )
        except RuntimeError:
            return hashlib.sha256(
                f"mca-workspace:{self.workspace}".encode("utf-8")
            ).hexdigest()

    def _user_identity(self) -> str:
        path = self.events_directory / USER_IDENTITY_NAME
        try:
            path.lstat()
        except FileNotFoundError:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            identity = secrets.token_hex(32)
            try:
                descriptor = os.open(path, flags, 0o600)
            except FileExistsError:
                return self._user_identity()
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(descriptor, 0o600)
                os.write(descriptor, identity.encode("ascii"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        else:
            identity = _read_user_identity(path)
        return hashlib.sha256(f"mca-user:{identity}".encode("ascii")).hexdigest()

    def _read_event_rows(self) -> list[dict[str, object]]:
        rows = self.ledger.read(self.events_path, self._event_log_name)
        for index, row in enumerate(rows):
            event = ConversationEvent(**cast(Any, row))
            recreated = ConversationEvent.create(
                host_id=event.host_id,
                conversation_id=event.conversation_id,
                source_ref=event.source_ref,
                sequence=event.sequence,
                role=event.role,
                content=event.content,
                participant_id=event.participant_id,
                created_at=event.created_at,
                metadata=json.loads(event.metadata_json),
            )
            if event != recreated or event.sequence != index:
                raise ValueError("conversation event log failed integrity validation")
        return rows

    def _persisted_next_sequence(self) -> int:
        row = self.ledger.last_payload(self.events_path, self._event_log_name)
        if row is None:
            return 0
        event = ConversationEvent(**cast(Any, row))
        recreated = ConversationEvent.create(
            host_id=event.host_id,
            conversation_id=event.conversation_id,
            source_ref=event.source_ref,
            sequence=event.sequence,
            role=event.role,
            content=event.content,
            participant_id=event.participant_id,
            created_at=event.created_at,
            metadata=json.loads(event.metadata_json),
        )
        if event != recreated:
            raise ValueError("conversation event log failed integrity validation")
        return event.sequence + 1

    @staticmethod
    def _validate_event_payload(row: dict[str, object]) -> None:
        event = ConversationEvent(**cast(Any, row))
        recreated = ConversationEvent.create(
            host_id=event.host_id,
            conversation_id=event.conversation_id,
            source_ref=event.source_ref,
            sequence=event.sequence,
            role=event.role,
            content=event.content,
            participant_id=event.participant_id,
            created_at=event.created_at,
            metadata=json.loads(event.metadata_json),
        )
        if event != recreated:
            raise ValueError("conversation event log failed integrity validation")

    @staticmethod
    def _validate_candidate_payload(row: dict[str, object]) -> None:
        if row.get("record_type") == "decision":
            if not str(row.get("candidate_id", "")):
                raise ValueError("candidate decision is missing an id")
            return
        if row.get("record_type") != "candidate":
            raise ValueError("unknown conversation candidate record")
        if row.get("scope", "workspace") not in {"user", "workspace"}:
            raise ValueError("conversation memory candidate scope is invalid")
        values = {
            key: str(row.get(key, ""))
            for key in (
                "candidate_id",
                "value",
                "source_ref",
                "source_sha256",
                "source_event_id",
                "created_at",
            )
        }
        expected_payload = {key: values[key] for key in values if key != "candidate_id"}
        if "scope" in row:
            expected_payload["scope"] = str(row["scope"])
        if values["candidate_id"] != _digest(expected_payload):
            raise ValueError("conversation memory candidate digest mismatch")
