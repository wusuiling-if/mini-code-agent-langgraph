"""Small dependency-inversion surface shared by memory hosts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class EvidenceReference:
    source_type: str
    source_ref: str
    source_sha256: str
    origin: str


@dataclass(frozen=True)
class VerifiedCheck:
    name: str
    command_sha256: str


@dataclass(frozen=True)
class VerifiedExperience:
    """Host-authenticated facts from which the core may form memory."""

    evidence: EvidenceReference
    scope: str
    scope_key: str
    valid_from: str
    task: str
    checks: tuple[VerifiedCheck, ...]
    artifact_text: str | None = None
    artifact_size_bytes: int = 0
    artifact_binary: bool = False


@dataclass(frozen=True)
class MemoryDraft:
    value: str
    abstraction: str
    cue_anchors: tuple[str, ...]
    kind: str
    subtype: str
    scope: str
    scope_key: str
    origin: str
    authority: str
    confidence: float
    importance: float
    valid_from: str
    valid_to: str | None = None
    identity_anchor: str | None = None


@dataclass(frozen=True)
class ContextEntry:
    content_sha256: str
    value: str
    scope: str
    scope_key: str
    authority: str
    evidence_refs: tuple[str, ...]
    score: float = 0.0


@dataclass(frozen=True)
class RetrievalAudit:
    decision: str
    reason: str
    selected_content_sha256: tuple[str, ...]
    context_chars: int
    truncated: bool


@dataclass(frozen=True)
class SemanticDocument:
    document_id: str
    text: str


class SemanticCandidateProvider(Protocol):
    """Optional embedding/reranker boundary; hosts choose the implementation."""

    def rank(
        self,
        query: str,
        documents: Sequence[SemanticDocument],
        *,
        limit: int,
    ) -> Sequence[tuple[str, float]]: ...


class EvidenceProvider(Protocol):
    def resolve(self, reference: str) -> VerifiedExperience: ...


class ProjectIdentityProvider(Protocol):
    def identity_sha256(self, project: Path, *, create: bool) -> str: ...


class MemoryRepository(Protocol):
    def admit(self, draft: MemoryDraft, evidence: EvidenceReference) -> str: ...

    def retrieve(
        self,
        query: str,
        *,
        scope: str,
        scope_key: str,
        limit: int,
    ) -> Sequence[ContextEntry]: ...


class ContextSink(Protocol):
    def deliver(self, context: str, audit: RetrievalAudit) -> None: ...
