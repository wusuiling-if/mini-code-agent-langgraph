"""Long-horizon retention and continuity recall without silent forgetting."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from memory_core.conversation import RecallItem

RETENTION_CLASSES = frozenset({"core", "durable", "episodic", "transient"})
_RETENTION_RANK = {"transient": 0, "episodic": 1, "durable": 2, "core": 3}


@dataclass(frozen=True)
class ContinuityMemory:
    """One active memory considered for retention and session continuity."""

    record_id: str
    text: str
    retention_class: str
    recorded_at_ns: int
    last_used_at_ns: int = 0
    use_count: int = 0
    importance: float = 0.5
    confidence: float = 0.5
    identity_anchor: str = ""
    pinned: bool = False
    source_valid: bool = True
    status: str = "active"
    authority: str = "none"
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.record_id.strip():
            raise ValueError("continuity record id must not be blank")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("continuity memory text must not be blank")
        if self.retention_class not in RETENTION_CLASSES:
            raise ValueError(f"unsupported retention class: {self.retention_class}")
        if self.recorded_at_ns < 0 or self.last_used_at_ns < 0 or self.use_count < 0:
            raise ValueError("continuity timestamps and use count must not be negative")
        if not 0 <= self.importance <= 1 or not 0 <= self.confidence <= 1:
            raise ValueError("continuity importance and confidence must be in [0, 1]")
        if self.authority not in {"none", "inform", "act"}:
            raise ValueError(f"unsupported continuity authority: {self.authority}")

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass(frozen=True)
class LongTermRetentionPolicy:
    max_active_records: int = 2_000
    max_active_chars: int = 2_000_000
    preserve_anchor_coverage: bool = True
    protect_core: bool = True
    protect_durable: bool = True
    protect_pinned: bool = True
    checkpoint_batch_records: int = 8

    def __post_init__(self) -> None:
        if self.max_active_records < 1 or self.max_active_chars < 1:
            raise ValueError("long-term retention capacities must be positive")
        if self.checkpoint_batch_records < 2:
            raise ValueError("checkpoint batch must contain at least two records")


@dataclass(frozen=True)
class RetentionDecision:
    admit: bool
    reason: str
    retire_record_ids: tuple[str, ...]
    compact_record_ids: tuple[str, ...]
    protected_record_ids: tuple[str, ...]
    projected_records: int
    projected_chars: int


def plan_long_term_retention(
    records: Sequence[ContinuityMemory],
    incoming: ContinuityMemory,
    *,
    policy: LongTermRetentionPolicy | None = None,
    now_ns: int,
) -> RetentionDecision:
    """Plan capacity retirement without silently evicting continuity anchors.

    This function only plans state transitions.  A host should atomically mark
    the returned records stale and admit the incoming memory, or apply neither.
    """

    policy = policy or LongTermRetentionPolicy()
    if now_ns < 0:
        raise ValueError("retention clock must not be negative")
    active = tuple(item for item in records if item.status == "active")
    if len({item.record_id for item in active}) != len(active):
        raise ValueError("continuity record ids must be unique")
    if incoming.record_id in {item.record_id for item in active}:
        raise ValueError("incoming continuity record id already exists")
    if not incoming.source_valid:
        return RetentionDecision(
            False,
            "incoming_source_invalid",
            (),
            (),
            (),
            len(active),
            sum(item.chars for item in active),
        )
    if incoming.status != "active":
        return RetentionDecision(
            False,
            "incoming_not_active",
            (),
            (),
            (),
            len(active),
            sum(item.chars for item in active),
        )
    if incoming.chars > policy.max_active_chars:
        return RetentionDecision(
            False,
            "incoming_exceeds_capacity",
            (),
            (),
            (),
            len(active),
            sum(item.chars for item in active),
        )

    protected = _protected_ids(active, policy)
    invalid_ids = {item.record_id for item in active if not item.source_valid}
    retained_ids = {item.record_id for item in active} - invalid_ids
    retained_chars = sum(
        item.chars for item in active if item.record_id in retained_ids
    )
    retirements: list[str] = sorted(invalid_ids)
    compactions: list[str] = []
    candidates = sorted(
        (
            item
            for item in active
            if item.record_id not in protected and item.record_id in retained_ids
        ),
        key=lambda item: (_retention_utility(item, now_ns), item.record_id),
    )
    for candidate in candidates:
        if (
            len(retained_ids) + 1 <= policy.max_active_records
            and retained_chars + incoming.chars <= policy.max_active_chars
        ):
            break
        if candidate.retention_class == "episodic" and candidate.source_valid:
            compactions.append(candidate.record_id)
            retained_ids.remove(candidate.record_id)
            retained_chars -= candidate.chars
            continue
        retained_ids.remove(candidate.record_id)
        retained_chars -= candidate.chars
        retirements.append(candidate.record_id)

    projected_records = len(retained_ids) + 1
    projected_chars = retained_chars + incoming.chars
    if (
        projected_records > policy.max_active_records
        or projected_chars > policy.max_active_chars
    ):
        return RetentionDecision(
            False,
            "protected_capacity_exhausted",
            (),
            (),
            tuple(sorted(protected)),
            len(active),
            sum(item.chars for item in active),
        )
    if compactions:
        minimum = 2 if len(active) + 1 > policy.max_active_records else 1
        if len(compactions) < minimum:
            for candidate in candidates:
                if (
                    candidate.retention_class == "episodic"
                    and candidate.source_valid
                    and candidate.record_id not in compactions
                ):
                    compactions.append(candidate.record_id)
                    if len(compactions) >= minimum:
                        break
        if len(compactions) < minimum:
            return RetentionDecision(
                False,
                "protected_capacity_exhausted",
                (),
                (),
                tuple(sorted(protected)),
                len(active),
                sum(item.chars for item in active),
            )
        compactions = compactions[: policy.checkpoint_batch_records]
        return RetentionDecision(
            False,
            "checkpoint_compaction_required",
            (),
            tuple(compactions),
            tuple(sorted(protected)),
            len(active),
            sum(item.chars for item in active),
        )
    return RetentionDecision(
        True,
        "fits" if not retirements else "retire_invalid_or_lower_continuity_utility",
        tuple(retirements),
        (),
        tuple(sorted(protected)),
        projected_records,
        projected_chars,
    )


def _protected_ids(
    records: Sequence[ContinuityMemory], policy: LongTermRetentionPolicy
) -> set[str]:
    protected = {
        item.record_id
        for item in records
        if item.source_valid
        and (
            (policy.protect_core and item.retention_class == "core")
            or (policy.protect_durable and item.retention_class == "durable")
            or (policy.protect_pinned and item.pinned)
        )
    }
    if not policy.preserve_anchor_coverage:
        return protected
    by_anchor: dict[str, list[ContinuityMemory]] = {}
    for item in records:
        if (
            item.source_valid
            and item.identity_anchor.strip()
            and item.retention_class in {"core", "durable"}
        ):
            by_anchor.setdefault(item.identity_anchor, []).append(item)
    for anchored in by_anchor.values():
        if any(item.record_id in protected for item in anchored):
            continue
        survivor = max(
            anchored,
            key=lambda item: (
                _RETENTION_RANK[item.retention_class],
                item.importance,
                item.confidence,
                item.last_used_at_ns,
                item.recorded_at_ns,
                item.record_id,
            ),
        )
        protected.add(survivor.record_id)
    return protected


def _retention_utility(item: ContinuityMemory, now_ns: int) -> float:
    if not item.source_valid:
        return -10.0
    age_ns = max(0, now_ns - max(item.last_used_at_ns, item.recorded_at_ns))
    age_days = age_ns / 86_400_000_000_000
    recency = 1.0 / (1.0 + math.log1p(age_days))
    usage = min(math.log1p(item.use_count) / math.log(101), 1.0)
    return (
        _RETENTION_RANK[item.retention_class] * 2.0
        + item.importance * 1.5
        + item.confidence
        + usage
        + recency * 0.5
    )


@dataclass(frozen=True)
class ContinuityRecallPolicy:
    max_items: int = 12
    max_chars: int = 3_000
    include_durable: bool = True

    def __post_init__(self) -> None:
        if self.max_items < 1 or self.max_chars < 1:
            raise ValueError("continuity recall budgets must be positive")


@dataclass(frozen=True)
class ContinuitySelection:
    items: tuple[RecallItem, ...]
    selected_record_ids: tuple[str, ...]
    omitted_core_record_ids: tuple[str, ...]
    context_chars: int
    requires_compaction: bool


def select_continuity_context(
    records: Sequence[ContinuityMemory],
    *,
    policy: ContinuityRecallPolicy | None = None,
) -> ContinuitySelection:
    """Select query-independent continuity facts for a new session.

    Core memories are considered before durable memories.  If the prompt budget
    cannot fit every core fact, the result explicitly requests compaction; it
    never reports a complete continuity set while silently dropping core state.
    """

    policy = policy or ContinuityRecallPolicy()
    eligible = [
        item
        for item in records
        if item.status == "active"
        and item.source_valid
        and (
            item.retention_class == "core"
            or policy.include_durable
            and item.retention_class == "durable"
        )
    ]
    eligible.sort(
        key=lambda item: (
            -int(item.pinned),
            -_RETENTION_RANK[item.retention_class],
            -item.importance,
            -item.confidence,
            -item.use_count,
            -item.last_used_at_ns,
            item.record_id,
        )
    )
    selected: list[ContinuityMemory] = []
    context_chars = 0
    for item in eligible:
        separator = 1 if selected else 0
        if len(selected) >= policy.max_items:
            continue
        if context_chars + separator + item.chars > policy.max_chars:
            continue
        selected.append(item)
        context_chars += separator + item.chars
    selected_ids = {item.record_id for item in selected}
    omitted_core = tuple(
        item.record_id
        for item in eligible
        if item.retention_class == "core" and item.record_id not in selected_ids
    )
    return ContinuitySelection(
        items=tuple(
            RecallItem(
                text=item.text,
                kind=f"continuity:{item.retention_class}",
                evidence_refs=item.evidence_refs,
                authority=item.authority,
            )
            for item in selected
        ),
        selected_record_ids=tuple(item.record_id for item in selected),
        omitted_core_record_ids=omitted_core,
        context_chars=context_chars,
        requires_compaction=bool(omitted_core),
    )


def retention_class_counts(
    records: Sequence[ContinuityMemory],
) -> tuple[tuple[str, int], ...]:
    """Small value-free diagnostic suitable for health reports."""

    counts = Counter(
        item.retention_class for item in records if item.status == "active"
    )
    return tuple((name, counts[name]) for name in sorted(RETENTION_CLASSES))
