"""Capacity policy that retires old records without deleting audit history."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapacityPolicy:
    max_active_records_per_scope: int = 64
    max_active_chars_per_scope: int = 1_000_000

    def __post_init__(self) -> None:
        if self.max_active_records_per_scope < 1:
            raise ValueError("active record capacity must be positive")
        if self.max_active_chars_per_scope < 1:
            raise ValueError("active character capacity must be positive")


@dataclass(frozen=True)
class LifecycleRecord:
    record_id: str
    recorded_at_ns: int
    chars: int


def select_retirements(
    records: tuple[LifecycleRecord, ...],
    *,
    incoming_chars: int,
    policy: CapacityPolicy,
) -> tuple[str, ...]:
    """Return oldest ids to mark stale so the incoming record fits."""

    if incoming_chars < 0:
        raise ValueError("incoming character count must not be negative")
    retained = sorted(records, key=lambda item: item.recorded_at_ns, reverse=True)
    retire: list[str] = []
    while retained and (
        len(retained) + 1 > policy.max_active_records_per_scope
        or sum(item.chars for item in retained) + incoming_chars
        > policy.max_active_chars_per_scope
    ):
        retire.append(retained.pop().record_id)
    return tuple(retire)
