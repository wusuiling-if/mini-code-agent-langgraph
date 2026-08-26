"""Domain-neutral, evidence-aware memory retrieval and abstention policy."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from memory_core.contracts import (
    ContextEntry,
    SemanticCandidateProvider,
    SemanticDocument,
)
from memory_core.rendering import ContextBudget, render_context
from mini_code_agent.memory_models import MemoryCard, MemoryIntegrityError
from mini_code_agent.memory_store import SQLiteMemoryStore

MemoryDecisionKind = Literal["use_memory", "no_memory"]
MemoryAuthority = Literal["none", "inform", "act"]

_AUTHORITY_RANK = {"none": 0, "inform": 1, "act": 2}
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "what",
        "when",
        "where",
        "which",
        "with",
        "了",
        "什么",
        "如何",
        "怎么",
        "我们",
        "我的",
        "你的",
        "他的",
        "她的",
        "它的",
        "当前",
        "这个",
        "那个",
        "需要",
    }
)


@dataclass(frozen=True)
class MemoryScope:
    name: str
    key: str


@dataclass(frozen=True)
class MemoryQuery:
    text: str
    scopes: tuple[MemoryScope, ...] = ()
    as_of: str | None = None
    required_authority: MemoryAuthority = "none"
    limit: int = 3


@dataclass(frozen=True)
class ScenarioMemoryPolicy:
    """Small policy surface shared by domain-specific strategy presets."""

    name: str = "generic"
    min_confidence: float = 0.55
    min_score: float = 0.42
    min_margin: float = 0.035
    max_candidates: int = 24
    graph_depth: int = 1
    require_explicit_scope: bool = True
    trusted_evidence_origins: tuple[str, ...] = (
        "user",
        "trusted_tool",
        "agent",
        "external",
    )


SCENARIO_POLICIES = {
    "generic": ScenarioMemoryPolicy(),
    "coding": ScenarioMemoryPolicy(
        name="coding", min_confidence=0.6, min_score=0.44, min_margin=0.04
    ),
    "research": ScenarioMemoryPolicy(
        name="research", min_confidence=0.5, min_score=0.39, min_margin=0.025
    ),
    "personal_assistant": ScenarioMemoryPolicy(
        name="personal_assistant",
        min_confidence=0.65,
        min_score=0.46,
        min_margin=0.05,
    ),
    "customer_service": ScenarioMemoryPolicy(
        name="customer_service",
        min_confidence=0.65,
        min_score=0.46,
        min_margin=0.05,
    ),
}


@dataclass(frozen=True)
class MemoryContextItem:
    card_id: str
    value: str
    abstraction: str
    kind: str
    subtype: str
    scope: str
    scope_key: str
    authority: str
    confidence: float
    content_sha256: str
    score: float
    routes: tuple[str, ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class MemoryUseDecision:
    kind: MemoryDecisionKind
    reason: str
    top_score: float
    score_margin: float
    considered: int
    eligible: int


@dataclass(frozen=True)
class MemoryContextPack:
    query: MemoryQuery
    policy_name: str
    decision: MemoryUseDecision
    items: tuple[MemoryContextItem, ...] = ()

    def render(self, budget: ContextBudget | None = None) -> str:
        """Render a bounded, provenance-bearing context block for a model."""

        if self.decision.kind == "no_memory":
            return ""
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
            for item in self.items
        )
        return render_context(
            entries,
            query=self.query.text,
            budget=budget,
        ).text

    def audit_record(
        self, *, include_query_fingerprint: bool = False
    ) -> dict[str, Any]:
        """Return a value-free retrieval audit suitable for durable logs."""

        record: dict[str, Any] = {
            "schema_version": 1,
            "policy": self.policy_name,
            "query_chars": len(self.query.text),
            "scope_names": sorted({scope.name for scope in self.query.scopes}),
            "required_authority": self.query.required_authority,
            "decision": {
                "kind": self.decision.kind,
                "reason": self.decision.reason,
                "top_score": round(self.decision.top_score, 6),
                "score_margin": round(self.decision.score_margin, 6),
                "considered": self.decision.considered,
                "eligible": self.decision.eligible,
            },
            "selected": [
                {
                    "content_sha256": item.content_sha256,
                    "kind": item.kind,
                    "scope": item.scope,
                    "authority": item.authority,
                    "score": item.score,
                    "routes": list(item.routes),
                    "evidence_count": len(item.evidence_refs),
                }
                for item in self.items
            ],
        }
        if include_query_fingerprint:
            record["query_sha256"] = hashlib.sha256(
                self.query.text.encode("utf-8")
            ).hexdigest()
        return record


@dataclass(frozen=True)
class _RankedCard:
    card: MemoryCard
    score: float
    routes: tuple[str, ...]


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def lexical_tokens(text: str) -> tuple[str, ...]:
    """Tokenize Latin text and CJK text without optional model dependencies."""

    normalized = _normalize(text)
    tokens: list[str] = []
    tokens.extend(re.findall(r"[a-z0-9][a-z0-9_.:/+\-]*", normalized))
    for span in re.findall(r"[\u3400-\u9fff]+", normalized):
        if len(span) == 1:
            tokens.append(span)
            continue
        tokens.extend(span[index : index + 2] for index in range(len(span) - 1))
    return tuple(token for token in tokens if token not in _STOPWORDS)


def _card_text(card: MemoryCard) -> str:
    return " ".join((card.abstraction, *card.cue_anchors))


def _query_content(query: MemoryQuery) -> str:
    """Remove explicit scope identifiers already represented as metadata.

    A tenant or repository name is useful for filtering, but it must not make
    every memory in that scope appear relevant to an otherwise unmatched query.
    """

    content = _normalize(query.text)
    for scope in query.scopes:
        key = _normalize(scope.key)
        if key:
            content = content.replace(key, " ")
    return " ".join(content.split())


def _direct_similarity(query: MemoryQuery, card: MemoryCard) -> float:
    """Return a corpus-independent similarity for active/retired comparison."""

    content = _query_content(query)
    for anchor in card.cue_anchors:
        normalized_anchor = _normalize(anchor)
        if len(normalized_anchor) >= 2 and (
            normalized_anchor in content or content in normalized_anchor
        ):
            return 1.0
    left = Counter(lexical_tokens(content))
    right = Counter(lexical_tokens(_card_text(card)))
    numerator = sum(left[token] * right[token] for token in left)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def _parse_time(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("memory query as_of must include a UTC offset")
    return parsed


def _valid_at(card: MemoryCard, moment: datetime) -> bool:
    if card.valid_from and _parse_time(card.valid_from) > moment:
        return False
    return not (card.valid_to and _parse_time(card.valid_to) <= moment)


def _scope_matches(
    card: MemoryCard, query: MemoryQuery, policy: ScenarioMemoryPolicy
) -> bool:
    if card.scope == "global":
        return True
    if not query.scopes:
        return not policy.require_explicit_scope
    return any(
        scope.name == card.scope and scope.key == card.scope_key
        for scope in query.scopes
    )


def _bm25_ranking(
    query_tokens: tuple[str, ...], cards: tuple[MemoryCard, ...]
) -> list[tuple[str, float]]:
    if not query_tokens or not cards:
        return []
    documents = [Counter(lexical_tokens(_card_text(card))) for card in cards]
    lengths = [sum(document.values()) for document in documents]
    average_length = sum(lengths) / len(lengths) if lengths else 1.0
    document_frequency = Counter()
    for document in documents:
        document_frequency.update(set(document))
    query_terms = set(query_tokens)
    ranked: list[tuple[str, float]] = []
    for card, document, length in zip(cards, documents, lengths):
        score = 0.0
        for token in query_terms:
            frequency = document[token]
            if not frequency:
                continue
            inverse_frequency = math.log(
                1.0
                + (len(cards) - document_frequency[token] + 0.5)
                / (document_frequency[token] + 0.5)
            )
            denominator = frequency + 1.2 * (
                1.0 - 0.75 + 0.75 * length / max(average_length, 1.0)
            )
            score += inverse_frequency * frequency * 2.2 / denominator
        if score > 0:
            ranked.append((card.id, score))
    return sorted(ranked, key=lambda item: (-item[1], item[0]))


class EvidenceTemporalRetriever:
    """Hybrid retrieval with hard eligibility filters and explicit abstention."""

    def __init__(
        self,
        store: SQLiteMemoryStore,
        *,
        policy: ScenarioMemoryPolicy | None = None,
        semantic_provider: SemanticCandidateProvider | None = None,
    ):
        self.store = store
        self.policy = policy or SCENARIO_POLICIES["generic"]
        self.semantic_provider = semantic_provider

    def retrieve(self, query: MemoryQuery) -> MemoryContextPack:
        if not query.text.strip():
            raise ValueError("memory query text must not be blank")
        if query.limit < 1 or query.limit > 20:
            raise ValueError("memory query limit must be between 1 and 20")
        if query.required_authority not in _AUTHORITY_RANK:
            raise ValueError("unsupported required memory authority")

        scope_pairs = tuple((scope.name, scope.key) for scope in query.scopes)
        all_cards = self.store.list_cards(
            include_inactive=True,
            scope_pairs=(
                scope_pairs
                if query.scopes or self.policy.require_explicit_scope
                else None
            ),
        )
        moment = _parse_time(query.as_of)
        scoped_candidates: list[MemoryCard] = []
        eligible: list[MemoryCard] = []
        for card in all_cards:
            if card.confidence < self.policy.min_confidence:
                continue
            if not _scope_matches(card, query, self.policy):
                continue
            if (
                _AUTHORITY_RANK[card.authority]
                < _AUTHORITY_RANK[query.required_authority]
            ):
                continue
            sources = self.store.sources(card.id)
            if not sources or not any(
                source.origin in self.policy.trusted_evidence_origins
                for source in sources
            ):
                continue
            scoped_candidates.append(card)
            if card.status != "active" or not _valid_at(card, moment):
                continue
            eligible.append(card)

        ranked = self._rank(query, tuple(eligible))
        eligible_ids = {card.id for card in eligible}
        excluded = [card for card in scoped_candidates if card.id not in eligible_ids]
        best_excluded = max(
            excluded, key=lambda card: _direct_similarity(query, card), default=None
        )
        excluded_similarity = (
            _direct_similarity(query, best_excluded) if best_excluded else 0.0
        )
        active_similarity = max(
            (_direct_similarity(query, card) for card in eligible), default=0.0
        )
        if (
            best_excluded is not None
            and excluded_similarity >= 0.35
            and excluded_similarity > active_similarity + self.policy.min_margin
        ):
            return self._no_memory(
                query,
                "best_candidate_inactive_or_invalid",
                len(all_cards),
                len(eligible),
                excluded_similarity,
                excluded_similarity - active_similarity,
            )
        if not ranked:
            return self._no_memory(
                query, "no_relevant_candidate", len(all_cards), len(eligible)
            )

        top_score = ranked[0].score
        second_score = ranked[1].score if len(ranked) > 1 else 0.0
        margin = top_score - second_score
        top_has_exact = "exact_anchor" in ranked[0].routes
        graph_requested = bool(
            re.search(
                r"\b(result|reason|relationship|related|evidence)\b|结果|结论|原因|关系|证据",
                _normalize(query.text),
            )
        )
        has_graph_chain = graph_requested and any(
            "graph" in item.routes for item in ranked
        )
        if top_score < self.policy.min_score:
            return self._no_memory(
                query,
                "confidence_below_threshold",
                len(all_cards),
                len(eligible),
                top_score,
                margin,
            )
        if (
            len(ranked) > 1
            and margin < self.policy.min_margin
            and not top_has_exact
            and not has_graph_chain
        ):
            return self._no_memory(
                query,
                "ambiguous_candidates",
                len(all_cards),
                len(eligible),
                top_score,
                margin,
            )

        item_floor = max(0.25, top_score * 0.55)
        selected = [
            item
            for item in ranked
            if (
                not has_graph_chain
                and item.score >= item_floor
                or has_graph_chain
                and (
                    "graph_seed" in item.routes
                    or "graph" in item.routes
                    and item.score >= 0.25
                )
            )
        ][: query.limit]
        items = []
        for ranked_card in selected:
            sources = self.store.sources(ranked_card.card.id)
            items.append(
                MemoryContextItem(
                    card_id=ranked_card.card.id,
                    value=ranked_card.card.value,
                    abstraction=ranked_card.card.abstraction,
                    kind=ranked_card.card.kind,
                    subtype=ranked_card.card.subtype,
                    scope=ranked_card.card.scope,
                    scope_key=ranked_card.card.scope_key,
                    authority=ranked_card.card.authority,
                    confidence=ranked_card.card.confidence,
                    content_sha256=ranked_card.card.content_sha256,
                    score=round(ranked_card.score, 6),
                    routes=ranked_card.routes,
                    evidence_refs=tuple(source.source_ref for source in sources),
                )
            )
        return MemoryContextPack(
            query=query,
            policy_name=self.policy.name,
            decision=MemoryUseDecision(
                "use_memory",
                "eligible_high_confidence",
                top_score,
                margin,
                len(all_cards),
                len(eligible),
            ),
            items=tuple(items),
        )

    def _rank(
        self, query: MemoryQuery, cards: tuple[MemoryCard, ...]
    ) -> tuple[_RankedCard, ...]:
        if not cards:
            return ()
        query_normalized = _query_content(query)
        query_tokens = lexical_tokens(query_normalized)
        by_id = {card.id: card for card in cards}
        routes: dict[str, list[str]] = {}
        strengths: dict[str, dict[str, float]] = defaultdict(dict)

        bm25 = _bm25_ranking(query_tokens, cards)
        if bm25:
            routes["lexical_bm25"] = [card_id for card_id, _ in bm25]
            maximum = bm25[0][1]
            for card_id, score in bm25:
                strengths[card_id]["lexical_bm25"] = score / maximum

        if self.semantic_provider is not None:
            semantic = self.semantic_provider.rank(
                query_normalized,
                tuple(SemanticDocument(card.id, _card_text(card)) for card in cards),
                limit=self.policy.max_candidates,
            )
            semantic_values = [
                (card_id, float(score))
                for card_id, score in semantic
                if card_id in by_id and 0 < float(score) <= 1
            ]
            semantic_values.sort(key=lambda item: (-item[1], item[0]))
            if semantic_values:
                routes["semantic"] = [card_id for card_id, _ in semantic_values]
                for card_id, score in semantic_values:
                    strengths[card_id]["semantic"] = score

        exact: list[tuple[str, float]] = []
        cue: list[tuple[str, float]] = []
        query_set = set(query_tokens)
        for card in cards:
            normalized_anchors = tuple(
                _normalize(anchor) for anchor in card.cue_anchors
            )
            exact_matches = [
                anchor
                for anchor in normalized_anchors
                if len(anchor) >= 2
                and anchor not in _STOPWORDS
                and (anchor in query_normalized or query_normalized in anchor)
            ]
            if exact_matches:
                exact.append(
                    (
                        card.id,
                        max(
                            min(len(anchor) / max(len(query_normalized), 1), 1.0)
                            for anchor in exact_matches
                        ),
                    )
                )
            card_tokens = set(lexical_tokens(_card_text(card)))
            overlap = len(query_set & card_tokens) / max(len(query_set), 1)
            if overlap:
                cue.append((card.id, overlap))
        for route_name, values in (("exact_anchor", exact), ("cue_overlap", cue)):
            values.sort(key=lambda item: (-item[1], item[0]))
            if values:
                routes[route_name] = [card_id for card_id, _ in values]
                for card_id, score in values:
                    strengths[card_id][route_name] = score

        direct_seeds = []
        for ranking in routes.values():
            for card_id in ranking[:3]:
                if card_id not in direct_seeds:
                    direct_seeds.append(card_id)
        graph_values: dict[str, float] = {}
        graph_seeds: list[str] = []
        frontier = tuple(direct_seeds)
        visited = set(direct_seeds)
        for depth in range(self.policy.graph_depth):
            next_frontier: list[str] = []
            for seed_id in frontier:
                for edge in self.store.relations(seed_id):
                    neighbor = (
                        edge.target_id if edge.source_id == seed_id else edge.source_id
                    )
                    if neighbor not in by_id or neighbor in visited:
                        continue
                    visited.add(neighbor)
                    next_frontier.append(neighbor)
                    if seed_id not in graph_seeds:
                        graph_seeds.append(seed_id)
                    graph_values[neighbor] = max(
                        graph_values.get(neighbor, 0.0), 0.65 / (depth + 1)
                    )
            frontier = tuple(next_frontier)
        if graph_values:
            graph_ranked = sorted(
                graph_values, key=lambda card_id: (-graph_values[card_id], card_id)
            )
            routes["graph"] = graph_ranked
            for card_id in graph_ranked:
                strengths[card_id]["graph"] = graph_values[card_id]
            routes["graph_seed"] = graph_seeds
            for card_id in graph_seeds:
                strengths[card_id]["graph_seed"] = 0.5

        reciprocal_scores: dict[str, float] = defaultdict(float)
        for ranking in routes.values():
            for rank, card_id in enumerate(ranking[: self.policy.max_candidates], 1):
                reciprocal_scores[card_id] += 1.0 / (60.0 + rank)

        ranked_cards: list[_RankedCard] = []
        max_rrf = max(reciprocal_scores.values(), default=1.0)
        for card_id, rrf_score in reciprocal_scores.items():
            card = by_id[card_id]
            card_strengths = strengths[card_id]
            direct_strength = max(
                (value for route, value in card_strengths.items() if route != "graph"),
                default=0.0,
            )
            graph_strength = card_strengths.get("graph", 0.0)
            evidence_strength = 0.55 * direct_strength + 0.25 * graph_strength
            fused_strength = 0.1 * (rrf_score / max_rrf)
            quality_strength = 0.07 * card.confidence + 0.03 * card.importance
            score = min(1.0, evidence_strength + fused_strength + quality_strength)
            ranked_cards.append(
                _RankedCard(
                    card=card,
                    score=score,
                    routes=tuple(sorted(card_strengths)),
                )
            )
        return tuple(sorted(ranked_cards, key=lambda item: (-item.score, item.card.id)))

    def _no_memory(
        self,
        query: MemoryQuery,
        reason: str,
        considered: int,
        eligible: int,
        top_score: float = 0.0,
        margin: float = 0.0,
    ) -> MemoryContextPack:
        return MemoryContextPack(
            query=query,
            policy_name=self.policy.name,
            decision=MemoryUseDecision(
                "no_memory", reason, top_score, margin, considered, eligible
            ),
        )


def retrieve_workspace_context(
    state_root: Path,
    workspace: Path,
    task: str,
    *,
    limit: int = 3,
    scope_key: str | None = None,
    semantic_provider: SemanticCandidateProvider | None = None,
) -> MemoryContextPack | None:
    """Read original-architecture memory for an explicit opt-in workspace run."""

    store = SQLiteMemoryStore(
        Path(state_root).expanduser().resolve() / "memory",
        read_only=True,
    )
    if not store.initialized:
        return None
    if not store.verify().ok:
        raise MemoryIntegrityError("memory store integrity verification failed")
    resolved_scope_key = scope_key or (
        "sha256:" + hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()
    )
    return EvidenceTemporalRetriever(
        store,
        policy=SCENARIO_POLICIES["coding"],
        semantic_provider=semantic_provider,
    ).retrieve(
        MemoryQuery(
            task,
            scopes=(MemoryScope("workspace", resolved_scope_key),),
            required_authority="inform",
            limit=limit,
        )
    )
