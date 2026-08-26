"""Map an MCA retrieval pack into a bounded host-neutral context."""

from __future__ import annotations

from typing import Any

from memory_core.contracts import ContextEntry, RetrievalAudit
from memory_core.rendering import ContextBudget, RenderedContext, render_context


def render_memory_pack(
    pack: Any,
    *,
    budget: ContextBudget | None = None,
) -> tuple[RenderedContext, RetrievalAudit]:
    if pack is None or pack.decision.kind == "no_memory":
        reason = "uninitialized" if pack is None else pack.decision.reason
        empty = RenderedContext("", (), False)
        return empty, RetrievalAudit("no_memory", reason, (), 0, False)
    entries = tuple(
        ContextEntry(
            content_sha256=item.content_sha256,
            value=item.value,
            scope=item.scope,
            scope_key=item.scope_key,
            authority=item.authority,
            evidence_refs=item.evidence_refs,
            score=item.score,
        )
        for item in pack.items
    )
    rendered = render_context(entries, query=pack.query.text, budget=budget)
    decision = "use_memory" if rendered.text else "no_memory"
    reason = pack.decision.reason if rendered.text else "context_budget_exhausted"
    audit = RetrievalAudit(
        decision=decision,
        reason=reason,
        selected_content_sha256=rendered.selected_content_sha256,
        context_chars=len(rendered.text),
        truncated=rendered.truncated,
    )
    return rendered, audit
