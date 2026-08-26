"""Host-neutral orchestration over injected evidence, identity and storage ports."""

from __future__ import annotations

from pathlib import Path

from memory_core.contracts import (
    ContextSink,
    EvidenceProvider,
    MemoryRepository,
    ProjectIdentityProvider,
    RetrievalAudit,
)
from memory_core.experience import ExperienceFactory
from memory_core.rendering import ContextBudget, RenderedContext, render_context


class MemoryRuntime:
    def __init__(
        self,
        *,
        evidence_provider: EvidenceProvider,
        identity_provider: ProjectIdentityProvider,
        repository: MemoryRepository,
        factory: ExperienceFactory | None = None,
        context_sink: ContextSink | None = None,
    ) -> None:
        self.evidence_provider = evidence_provider
        self.identity_provider = identity_provider
        self.repository = repository
        self.factory = factory or ExperienceFactory()
        self.context_sink = context_sink

    def form(self, evidence_reference: str) -> tuple[str, ...]:
        experience = self.evidence_provider.resolve(evidence_reference)
        drafts = [self.factory.workflow(experience)]
        repair = self.factory.repair(experience)
        if repair is not None:
            drafts.append(repair)
        return tuple(
            self.repository.admit(draft, experience.evidence) for draft in drafts
        )

    def context(
        self,
        project: Path,
        query: str,
        *,
        scope: str = "project",
        limit: int = 3,
        budget: ContextBudget | None = None,
    ) -> tuple[RenderedContext, RetrievalAudit]:
        identity = self.identity_provider.identity_sha256(project, create=True)
        entries = tuple(
            self.repository.retrieve(
                query,
                scope=scope,
                scope_key=f"sha256:{identity}",
                limit=limit,
            )
        )
        rendered = render_context(entries, query=query, budget=budget)
        audit = RetrievalAudit(
            decision="use_memory" if rendered.text else "no_memory",
            reason="selected" if rendered.text else "no_relevant_candidate",
            selected_content_sha256=rendered.selected_content_sha256,
            context_chars=len(rendered.text),
            truncated=rendered.truncated,
        )
        if self.context_sink is not None:
            self.context_sink.deliver(rendered.text, audit)
        return rendered, audit
