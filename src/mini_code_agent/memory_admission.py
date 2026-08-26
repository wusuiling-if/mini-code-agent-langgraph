"""Runtime-owned admission for evidence-bound durable memories.

The SQLite store is a persistence primitive. This module is the trust boundary:
untrusted extractors provide content, while the runtime resolves evidence and
assigns scope, origin, authority, kind, and validity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory_core.contracts import EvidenceProvider, EvidenceReference
from memory_core.experience import ExperienceFactory
from memory_core.lifecycle import CapacityPolicy, LifecycleRecord, select_retirements
from memory_core.security import SecretDetector
from mini_code_agent.memory_adapters.transaction import TransactionEvidenceAdapter
from mini_code_agent.memory_models import EvidenceSource, MemoryCard
from mini_code_agent.memory_retrieval import lexical_tokens
from mini_code_agent.memory_store import SQLiteMemoryStore
from mini_code_agent.receipt import ReceiptError
from mini_code_agent.transaction import TransactionError, TransactionStore


class MemoryAdmissionError(RuntimeError):
    """A memory candidate cannot be bound to admissible runtime evidence."""


@dataclass(frozen=True)
class ProceduralMemoryCandidate:
    """Untrusted extracted content with no caller-controlled trust metadata."""

    value: str
    abstraction: str
    cue_anchors: tuple[str, ...] = ()
    subtype: str = "verified_workflow"
    confidence: float = 0.5
    importance: float = 0.5
    valid_to: str | None = None


class MemoryAdmissionService:
    """Admit memories only after resolving evidence through the runtime."""

    def __init__(
        self,
        state_root: Path,
        store: SQLiteMemoryStore | None = None,
        *,
        evidence_provider: EvidenceProvider | None = None,
        experience_factory: ExperienceFactory | None = None,
        capacity_policy: CapacityPolicy | None = None,
    ) -> None:
        self.state_root = Path(state_root).expanduser().resolve()
        self.store = store or SQLiteMemoryStore(self.state_root / "memory")
        if self.store.read_only:
            raise ValueError("memory admission requires a writable store")
        self.transactions = TransactionStore(self.state_root)
        self.evidence_provider = evidence_provider or TransactionEvidenceAdapter(
            self.state_root
        )
        self.experience_factory = experience_factory or ExperienceFactory()
        self.capacity_policy = capacity_policy or CapacityPolicy()

    def admit_verified_procedure(
        self,
        transaction_id: str,
        candidate: ProceduralMemoryCandidate,
        *,
        derived_from: tuple[str, ...] = (),
    ) -> MemoryCard:
        """Create an informative procedural card from a verified transaction.

        The candidate cannot choose trust-bearing fields. The authenticated
        receipt fixes the evidence reference, workspace scope and start time;
        runtime policy fixes the card to agent/inform authority.
        """

        receipt = self._validated_receipt(transaction_id)
        return self._admit_receipt_candidate(
            transaction_id, receipt, candidate, derived_from=derived_from
        )

    def admit_verification_workflow(self, transaction_id: str) -> MemoryCard:
        """Deterministically remember the authenticated verification matrix."""

        experience = self._verified_experience(transaction_id)
        try:
            draft = self.experience_factory.workflow(experience)
        except ValueError as exc:
            raise MemoryAdmissionError(str(exc)) from exc
        if draft.identity_anchor is None:
            raise MemoryAdmissionError("workflow draft has no identity anchor")
        return self.store.upsert_scoped_singleton(
            identity_anchor=draft.identity_anchor,
            value=draft.value,
            abstraction=draft.abstraction,
            cue_anchors=draft.cue_anchors,
            kind=draft.kind,
            subtype=draft.subtype,
            scope=draft.scope,
            scope_key=draft.scope_key,
            origin=draft.origin,
            authority=draft.authority,
            confidence=draft.confidence,
            importance=draft.importance,
            valid_from=draft.valid_from,
            valid_to=draft.valid_to,
            sources=(self._source(experience.evidence),),
        )

    def admit_verified_repair(self, transaction_id: str) -> MemoryCard | None:
        """Capture the authenticated patch as an opt-in episodic experience."""

        experience = self._verified_experience(transaction_id)
        draft = self.experience_factory.repair(experience)
        if (
            draft is None
            or len(draft.value) > self.capacity_policy.max_active_chars_per_scope
        ):
            return None
        card = self.store.add_card_once_for_source(
            source=self._source(experience.evidence),
            value=draft.value,
            abstraction=draft.abstraction,
            cue_anchors=draft.cue_anchors,
            kind=draft.kind,
            subtype=draft.subtype,
            scope=draft.scope,
            scope_key=draft.scope_key,
            origin=draft.origin,
            authority=draft.authority,
            confidence=draft.confidence,
            importance=draft.importance,
            valid_from=draft.valid_from,
            valid_to=draft.valid_to,
        )
        self._enforce_repair_capacity(card)
        return card

    @staticmethod
    def _automatic_task_cues(task: str) -> tuple[str, ...]:
        cues = tuple(
            dict.fromkeys(token for token in lexical_tokens(task) if len(token) >= 2)
        )
        return cues[:24]

    @staticmethod
    def _contains_likely_secret(text: str) -> bool:
        return SecretDetector().contains_secret(text)

    def _verified_experience(self, transaction_id: str):
        try:
            return self.evidence_provider.resolve(transaction_id)
        except (FileNotFoundError, ReceiptError, TransactionError, ValueError) as exc:
            raise MemoryAdmissionError(str(exc)) from exc

    @staticmethod
    def _source(reference: EvidenceReference) -> EvidenceSource:
        return EvidenceSource(
            source_type=reference.source_type,
            source_ref=reference.source_ref,
            source_sha256=reference.source_sha256,
            origin=reference.origin,
        )

    def _enforce_repair_capacity(self, incoming: MemoryCard) -> None:
        records = tuple(
            LifecycleRecord(card.id, card.recorded_at_ns, len(card.value))
            for card in self.store.list_cards()
            if card.id != incoming.id
            and card.subtype == "verified_repair"
            and card.scope == incoming.scope
            and card.scope_key == incoming.scope_key
        )
        retirements = select_retirements(
            records,
            incoming_chars=len(incoming.value),
            policy=self.capacity_policy,
        )
        for card_id in retirements:
            self.store.transition(card_id, "stale", related_card_id=incoming.id)

    def _validated_receipt(self, transaction_id: str) -> dict[str, Any]:
        try:
            return self.transactions.validated_receipt(transaction_id)
        except (FileNotFoundError, ReceiptError, TransactionError, ValueError) as exc:
            raise MemoryAdmissionError(str(exc)) from exc

    def _admit_receipt_candidate(
        self,
        transaction_id: str,
        receipt: dict[str, Any],
        candidate: ProceduralMemoryCandidate,
        *,
        derived_from: tuple[str, ...] = (),
    ) -> MemoryCard:
        self._passing_checks(receipt)
        payload = receipt["payload"]

        workspace_identity = self._workspace_identity(receipt)
        issued_at_ns = payload.get("issued_at_ns")
        if not isinstance(issued_at_ns, int) or issued_at_ns < 0:
            raise MemoryAdmissionError("transaction receipt issue time is invalid")
        valid_from = datetime.fromtimestamp(
            issued_at_ns / 1_000_000_000, tz=timezone.utc
        ).isoformat()
        source = self._receipt_source(transaction_id, receipt)
        return self.store.add_card(
            value=candidate.value,
            abstraction=candidate.abstraction,
            cue_anchors=candidate.cue_anchors,
            kind="procedural",
            subtype=candidate.subtype,
            scope="workspace",
            scope_key=f"sha256:{workspace_identity.lower()}",
            origin="agent",
            authority="inform",
            confidence=candidate.confidence,
            importance=candidate.importance,
            valid_from=valid_from,
            valid_to=candidate.valid_to,
            sources=(source,),
            derived_from=derived_from,
        )

    @staticmethod
    def _passing_checks(receipt: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        verification = receipt["payload"].get("verification", {})
        raw_checks = verification.get("checks", ())
        if verification.get("status") != "passed" or not raw_checks:
            raise MemoryAdmissionError(
                "transaction receipt has no passing verification evidence"
            )
        if not isinstance(raw_checks, (list, tuple)) or any(
            not isinstance(check, dict)
            or check.get("returncode") != 0
            or bool(check.get("blocked", False))
            or not bool(check.get("approved", True))
            for check in raw_checks
        ):
            raise MemoryAdmissionError(
                "transaction receipt contains an ineligible verification check"
            )
        return tuple(raw_checks)

    @staticmethod
    def _workspace_identity(receipt: dict[str, Any]) -> str:
        workspace = receipt["payload"].get("workspace", {})
        workspace_identity = str(workspace.get("identity_sha256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", workspace_identity):
            raise MemoryAdmissionError(
                "transaction receipt has no valid workspace identity"
            )
        return workspace_identity

    @staticmethod
    def _receipt_source(transaction_id: str, receipt: dict[str, Any]) -> EvidenceSource:
        receipt_id = str(receipt["receipt_id"])
        return EvidenceSource(
            source_type="transaction_receipt",
            source_ref=f"state:transaction-receipt:{transaction_id}:{receipt_id}",
            source_sha256=receipt_id,
            origin="trusted_tool",
        )


def form_committed_transaction_memory(
    state_root: Path,
    transaction_id: str,
    *,
    store: SQLiteMemoryStore | None = None,
) -> MemoryCard | None:
    """Apply the opt-in lifecycle gate after a transaction has committed."""

    resolved_root = Path(state_root).expanduser().resolve()
    manifest = TransactionStore(resolved_root).load(transaction_id)
    mode = manifest.get("memory_mode", "off")
    if mode == "off":
        return None
    if mode != "local":
        raise MemoryAdmissionError(f"unsupported transaction memory mode: {mode}")
    if manifest.get("status") != "committed":
        raise MemoryAdmissionError(
            "transaction memory can only form after successful commit"
        )
    return MemoryAdmissionService(resolved_root, store).admit_verification_workflow(
        transaction_id
    )


def form_committed_transaction_memories(
    state_root: Path,
    transaction_id: str,
    *,
    store: SQLiteMemoryStore | None = None,
) -> tuple[MemoryCard, ...]:
    """Form the original workflow and verified-repair memories after commit."""

    resolved_root = Path(state_root).expanduser().resolve()
    manifest = TransactionStore(resolved_root).load(transaction_id)
    if manifest.get("memory_mode", "off") == "off":
        return ()
    if manifest.get("memory_mode") != "local" or manifest.get("status") != "committed":
        raise MemoryAdmissionError("transaction memory requires a committed local mode")
    service = MemoryAdmissionService(resolved_root, store)
    cards = [service.admit_verification_workflow(transaction_id)]
    repair = service.admit_verified_repair(transaction_id)
    if repair is not None:
        cards.append(repair)
    return tuple(cards)
