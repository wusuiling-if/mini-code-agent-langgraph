from __future__ import annotations

from dataclasses import dataclass

MEMORY_KINDS = frozenset({"semantic", "episodic", "procedural", "state"})
MEMORY_ORIGINS = frozenset({"user", "trusted_tool", "agent", "external"})
MEMORY_AUTHORITIES = frozenset({"none", "inform", "act"})
MEMORY_STATUSES = frozenset({"active", "superseded", "disputed", "stale", "tombstoned"})
MEMORY_RELATIONS = frozenset(
    {"derived_from", "supports", "contradicts", "supersedes", "related_to"}
)
MEMORY_CONTROL_OPERATIONS = frozenset(
    {"retrieve", "retrieve_with_warning", "requery", "no_memory"}
)

_AUTHORITY_RANK = {"none": 0, "inform": 1, "act": 2}
_ORIGIN_MAX_AUTHORITY = {
    "external": "none",
    "agent": "inform",
    "trusted_tool": "inform",
    "user": "act",
}


class MemoryError(RuntimeError):
    """Base error for the optional local memory subsystem."""


class MemoryIntegrityError(MemoryError):
    """Stored memory or its evidence failed authentication."""


class MemoryNotInitializedError(MemoryError):
    """The read-only memory store does not exist yet."""


def validate_authority(origin: str, authority: str) -> None:
    if origin not in MEMORY_ORIGINS:
        raise ValueError(f"unsupported memory origin: {origin}")
    if authority not in MEMORY_AUTHORITIES:
        raise ValueError(f"unsupported memory authority: {authority}")
    maximum = _ORIGIN_MAX_AUTHORITY[origin]
    if _AUTHORITY_RANK[authority] > _AUTHORITY_RANK[maximum]:
        raise ValueError(
            f"origin {origin!r} cannot create {authority!r} authority memory"
        )


def validate_derived_authority(
    authority: str, source_authorities: tuple[str, ...]
) -> None:
    """Prevent summaries and transformations from increasing source authority."""

    if not source_authorities:
        return
    if authority not in MEMORY_AUTHORITIES or any(
        item not in MEMORY_AUTHORITIES for item in source_authorities
    ):
        raise ValueError("unsupported memory authority")
    maximum_rank = min(_AUTHORITY_RANK[item] for item in source_authorities)
    if _AUTHORITY_RANK[authority] > maximum_rank:
        raise ValueError("derived memory cannot increase source authority")


@dataclass(frozen=True)
class MemoryCard:
    id: str
    kind: str
    subtype: str
    scope: str
    scope_key: str
    value: str
    abstraction: str
    cue_anchors: tuple[str, ...]
    origin: str
    authority: str
    confidence: float
    importance: float
    valid_from: str | None
    valid_to: str | None
    recorded_at_ns: int
    content_sha256: str
    status: str


@dataclass(frozen=True)
class EvidenceSource:
    source_type: str
    source_ref: str
    source_sha256: str
    origin: str
    id: str = ""
    card_id: str = ""
    recorded_at_ns: int = 0


@dataclass(frozen=True)
class MemoryEdge:
    id: str
    source_id: str
    target_id: str
    relation: str
    recorded_at_ns: int


@dataclass(frozen=True)
class MemorySearchResult:
    id: str
    kind: str
    scope: str
    abstraction: str
    origin: str
    authority: str
    confidence: float
    importance: float
    status: str
    score: float


@dataclass(frozen=True)
class MemoryStoreStatus:
    initialized: bool
    database_path: str
    schema_version: int | None = None
    fts_enabled: bool = False
    cards: int = 0
    sources: int = 0
    edges: int = 0
    status_counts: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class MemoryVerification:
    ok: bool
    checked_cards: int
    checked_sources: int
    checked_edges: int
    checked_events: int
    errors: tuple[str, ...]


@dataclass(frozen=True)
class MemoryStoreHealth:
    as_of: str
    verification_ok: bool
    cards: int
    active_cards: int
    inactive_cards: int
    expired_active_cards: int
    future_active_cards: int
    scopes: int
    database_bytes: int
    verification_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryDecisionRecord:
    id: str
    query_sha256: str
    stage: str
    operation: str
    selected_card_ids: tuple[str, ...]
    expected_utility: float
    reason: str
    shadow: bool
    recorded_at_ns: int


@dataclass(frozen=True)
class MemoryOutcomeRecord:
    id: str
    decision_id: str
    success: bool
    reward: float
    harmful: bool
    token_cost: int
    evidence_type: str
    evidence_ref: str
    evidence_sha256: str
    evidence_origin: str
    recorded_at_ns: int


@dataclass(frozen=True)
class MemoryUtilityStats:
    card_id: str
    uses: int = 0
    successes: int = 0
    failures: int = 0
    harmful_uses: int = 0
    reward_total: float = 0.0

    @property
    def mean_reward(self) -> float:
        return self.reward_total / self.uses if self.uses else 0.0
