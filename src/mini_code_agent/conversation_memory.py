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
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from memory_core.conversation import ConversationEvent
from memory_core.rendering import ContextBudget
from memory_core.security import SecretDetector
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
from mini_code_agent.utils import MAX_STATE_FILE_BYTES

MAX_EVENT_CONTENT_CHARS = 1_000_000
MAX_CANDIDATE_CHARS = 2_000


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ConversationMemoryCandidate:
    candidate_id: str
    value: str
    source_ref: str
    source_sha256: str
    source_event_id: str
    created_at: str


class MemorySelectionError(ValueError):
    """A forget/correct selector did not resolve to exactly one memory."""

    def __init__(self, message: str, matches: tuple[MemoryCard, ...] = ()) -> None:
        super().__init__(message)
        self.matches = matches


class LocalConversationMemory:
    """Bridge an MCA chat session to the local evidence-bound memory store."""

    def __init__(
        self,
        state_root: Path,
        workspace: Path,
        session_path: Path,
        *,
        store: SQLiteMemoryStore | None = None,
    ) -> None:
        self.state_root = Path(state_root).expanduser().resolve()
        self.workspace = Path(workspace).expanduser().resolve()
        self.store = store or SQLiteMemoryStore(self.state_root / "memory")
        if self.store.read_only:
            raise ValueError("conversation memory requires a writable store")
        self.store.initialize()
        self.workspace_key = f"sha256:{self._workspace_identity()}"
        self.conversation_id = _digest(
            {"host": "mca-chat", "session_path": str(Path(session_path).resolve())}
        )
        self.events_directory = self.store.directory / "conversations"
        self._ensure_private_directory(self.events_directory)
        self.events_path = self.events_directory / f"{self.conversation_id}.jsonl"
        self.candidates_path = self.events_directory / "candidates.jsonl"
        self.lock_path = self.events_directory / "conversation-events.lock"
        self._next_sequence = len(self._read_event_rows())

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
            self._append_row_unlocked(self.events_path, asdict(event))
            self._next_sequence = sequence + 1
        return event

    def remember(self, value: str, source_event: ConversationEvent) -> MemoryCard:
        """Persist a user-approved memory bound to its exact chat event."""

        return self._remember_from_source(
            value,
            source_ref=source_event.source_ref,
            source_sha256=source_event.source_sha256,
            valid_from=source_event.created_at,
        )

    def remember_candidate(self, selector: str) -> MemoryCard:
        candidate = self._resolve_candidate(selector)
        card = self._remember_from_source(
            candidate.value,
            source_ref=candidate.source_ref,
            source_sha256=candidate.source_sha256,
            valid_from=candidate.created_at,
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
        ).retrieve(
            MemoryQuery(
                text=query,
                scopes=(MemoryScope("workspace", self.workspace_key),),
                required_authority="none",
                limit=4,
            )
        )
        return pack.render(
            ContextBudget(max_chars=5_000, max_item_chars=2_000, max_items=4)
        )

    def list_memories(self, query: str = "") -> tuple[MemoryCard, ...]:
        cards = self.store.list_cards(
            scope_pairs=(("workspace", self.workspace_key),),
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
                self._append_row_unlocked(
                    self.candidates_path,
                    {"record_type": "candidate", **asdict(candidate)},
                )
        return candidate

    def pending_candidates(
        self, *, include_decided: bool = False
    ) -> tuple[ConversationMemoryCandidate, ...]:
        candidates: dict[str, ConversationMemoryCandidate] = {}
        decided: set[str] = set()
        for row in self._read_rows(self.candidates_path):
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
            candidate = ConversationMemoryCandidate(**values)
            expected = _digest(
                {key: values[key] for key in values if key != "candidate_id"}
            )
            if candidate.candidate_id != expected:
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
    ) -> MemoryCard:
        value = self._validate_memory_text(value)
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
            scope="workspace",
            scope_key=self.workspace_key,
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
            self._append_row_unlocked(
                self.candidates_path,
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

    def _read_event_rows(self) -> list[dict[str, object]]:
        rows = self._read_rows(self.events_path)
        for index, row in enumerate(rows):
            event = ConversationEvent(**row)
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
        row = self._read_last_row(self.events_path)
        if row is None:
            return 0
        event = ConversationEvent(**row)
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
    def _ensure_private_directory(path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("conversation memory path must be a real directory")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("conversation memory path is not owned by this user")
        if os.name != "nt":
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise PermissionError(
                    "conversation memory path permissions are too broad"
                )

    @staticmethod
    def _read_rows(path: Path) -> list[dict[str, object]]:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return []
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("conversation memory log must be a regular file")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("conversation memory log is not owned by this user")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise PermissionError("conversation memory log permissions are too broad")
        if metadata.st_size > MAX_STATE_FILE_BYTES:
            raise ValueError("conversation memory log exceeds the state size limit")
        rows: list[dict[str, object]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise TypeError("conversation memory log rows must be objects")
                rows.append(value)
        return rows

    @staticmethod
    def _read_last_row(path: Path) -> dict[str, object] | None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("conversation memory log must be a regular file")
        if metadata.st_size == 0:
            return None
        if metadata.st_size > MAX_STATE_FILE_BYTES:
            raise ValueError("conversation memory log exceeds the state size limit")
        with path.open("rb") as handle:
            position = metadata.st_size
            buffer = b""
            while position > 0:
                chunk_size = min(8_192, position)
                position -= chunk_size
                handle.seek(position)
                buffer = handle.read(chunk_size) + buffer
                lines = buffer.rstrip(b"\n").splitlines()
                if len(lines) > 1 or position == 0:
                    value = json.loads(lines[-1].decode("utf-8"))
                    if not isinstance(value, dict):
                        raise TypeError("conversation memory log rows must be objects")
                    return value
        return None

    @staticmethod
    def _append_row_unlocked(path: Path, row: dict[str, object]) -> None:
        payload = (_canonical_json(row) + "\n").encode("utf-8")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError("conversation memory log must be a regular file")
            if metadata.st_size + len(payload) > MAX_STATE_FILE_BYTES:
                raise ValueError("conversation memory log exceeds the state size limit")
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
