"""Experimental fact candidates and a non-production shadow journal.

The extractor may propose interpretations of authenticated conversation
evidence, but it cannot mutate the primary memory store.  The journal validates
exact evidence quotes and applies lifecycle operations only to a separate store
used by evaluations.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from langchain_core.messages import HumanMessage

from mini_code_agent.memory_models import EvidenceSource, MemoryCard
from mini_code_agent.memory_retrieval import lexical_tokens
from mini_code_agent.memory_store import SQLiteMemoryStore

_KEY_PATTERN = re.compile(r"[a-z0-9][a-z0-9._:-]{2,119}\Z")
_OPERATIONS = frozenset({"ASSERT", "FORGET"})
_CARDINALITIES = frozenset({"singleton", "multi"})
_USER_ROLE_PREFIXES = ("用户：", "user:", "human:", "customer:")


class CandidateModel(Protocol):
    def invoke(self, messages: list[HumanMessage]) -> Any: ...


@dataclass(frozen=True)
class ShadowSession:
    session_id: str
    text: str
    scope_key: str
    valid_from: str
    is_filler: bool = False


@dataclass(frozen=True)
class FactCandidate:
    candidate_id: str
    session_id: str
    memory_key: str
    operation: str
    cardinality: str
    subject: str
    predicate: str
    object: str
    evidence_quote: str
    confidence: float
    scope_key: str
    valid_from: str


@dataclass(frozen=True)
class JournalEvent:
    candidate_id: str
    session_id: str
    memory_key: str
    proposed_operation: str
    outcome: str
    reason: str
    card_id: str = ""


@dataclass(frozen=True)
class ExtractionBatch:
    candidates: tuple[FactCandidate, ...]
    rejected: tuple[JournalEvent, ...]
    raw_response_sha256: str


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _response_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content
        ).strip()
    return str(content).strip()


def _parse_json_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        decoded = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("extractor did not return a JSON object") from None
        try:
            decoded = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError("extractor returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise TypeError("extractor result must be a JSON object")
    return decoded


def _prompt(sessions: Sequence[ShadowSession]) -> str:
    payload = [{"session_id": item.session_id, "text": item.text} for item in sessions]
    return f"""你是长期记忆的候选抽取器，不是最终写入器。

从下面每个独立会话中，只抽取未来对话确实可能需要的、由用户明确表达或明确确认的稳定事实、偏好、约束、决定和预约。

规则：
1. 普通闲聊、助手自行推测、泛泛描述、临时话题输出为空。
2. memory_key 使用稳定的小写英文路径，例如 user.preferred_name、project.atlas.database；同一属性更新时必须使用相同 key。
3. operation 只能是 ASSERT 或 FORGET。新的不同值仍输出 ASSERT，后续由 journal 判定 supersede。
4. cardinality 只能是 singleton 或 multi。称呼、数据库选择、通知渠道等用 singleton；多个并存预约/过敏原用 multi。
5. evidence_quote 必须逐字摘自对应 session，且尽量短；不得改写或补全。
6. subject、predicate、object 用简短、可直接回答问题的自然语言。FORGET 的 object 可以为空。
7. 只有把握充分才输出，confidence 为 0 到 1。不要根据常识制造事实。
8. 一个 session 可以输出多个候选；没有候选时不要为该 session 生成项目。

只输出严格 JSON，不要 Markdown：
{{"candidates":[{{"session_id":"...","memory_key":"...","operation":"ASSERT","cardinality":"singleton","subject":"用户","predicate":"偏好称呼","object":"Eddy","evidence_quote":"以后请叫我 Eddy","confidence":0.98}}]}}

会话：
{json.dumps(payload, ensure_ascii=False)}
"""


def _quote_has_user_support(session_text: str, quote: str) -> bool:
    lowered_quote = quote.casefold()
    if any(prefix.casefold() in lowered_quote for prefix in _USER_ROLE_PREFIXES):
        return True
    start = session_text.find(quote)
    if start < 0:
        return False
    line_start = session_text.rfind("\n", 0, start) + 1
    line = session_text[line_start:].lstrip().casefold()
    return any(line.startswith(prefix.casefold()) for prefix in _USER_ROLE_PREFIXES)


class StructuredCandidateExtractor:
    """Use a model for proposals and deterministic code for admission checks."""

    def __init__(self, model: CandidateModel, *, minimum_confidence: float = 0.8):
        if not 0 <= minimum_confidence <= 1:
            raise ValueError("minimum confidence must be between zero and one")
        self.model = model
        self.minimum_confidence = minimum_confidence

    def extract(self, sessions: Sequence[ShadowSession]) -> ExtractionBatch:
        indexed = {item.session_id: item for item in sessions}
        if not indexed or len(indexed) != len(sessions):
            raise ValueError("sessions must have unique non-empty identifiers")
        raw = _response_text(
            self.model.invoke([HumanMessage(content=_prompt(sessions))])
        )
        decoded = _parse_json_object(raw)
        rows = decoded.get("candidates")
        if not isinstance(rows, list):
            raise TypeError("extractor candidates must be a list")
        accepted: list[FactCandidate] = []
        rejected: list[JournalEvent] = []
        seen: set[tuple[str, str, str, str]] = set()
        for index, row in enumerate(rows):
            candidate, reason = self._validate_row(row, indexed)
            if candidate is None:
                session_id = (
                    str(row.get("session_id", "")) if isinstance(row, dict) else ""
                )
                rejected.append(
                    JournalEvent(
                        candidate_id=f"rejected:{index}",
                        session_id=session_id,
                        memory_key=(
                            str(row.get("memory_key", ""))
                            if isinstance(row, dict)
                            else ""
                        ),
                        proposed_operation=(
                            str(row.get("operation", ""))
                            if isinstance(row, dict)
                            else ""
                        ),
                        outcome="rejected",
                        reason=reason,
                    )
                )
                continue
            identity = (
                candidate.session_id,
                candidate.memory_key,
                candidate.operation,
                candidate.object.casefold(),
            )
            if identity in seen:
                rejected.append(
                    JournalEvent(
                        candidate_id=candidate.candidate_id,
                        session_id=candidate.session_id,
                        memory_key=candidate.memory_key,
                        proposed_operation=candidate.operation,
                        outcome="rejected",
                        reason="duplicate_candidate",
                    )
                )
                continue
            seen.add(identity)
            accepted.append(candidate)
        return ExtractionBatch(tuple(accepted), tuple(rejected), _sha256(raw))

    def _validate_row(
        self,
        row: Any,
        sessions: dict[str, ShadowSession],
    ) -> tuple[FactCandidate | None, str]:
        if not isinstance(row, dict):
            return None, "candidate_not_object"
        required = (
            "session_id",
            "memory_key",
            "operation",
            "cardinality",
            "subject",
            "predicate",
            "evidence_quote",
            "confidence",
        )
        if any(name not in row for name in required):
            return None, "missing_required_field"
        session_id = str(row["session_id"]).strip()
        session = sessions.get(session_id)
        if session is None:
            return None, "unknown_session"
        memory_key = str(row["memory_key"]).strip().casefold()
        if not _KEY_PATTERN.fullmatch(memory_key):
            return None, "invalid_memory_key"
        operation = str(row["operation"]).strip().upper()
        if operation not in _OPERATIONS:
            return None, "invalid_operation"
        cardinality = str(row["cardinality"]).strip().casefold()
        if cardinality not in _CARDINALITIES:
            return None, "invalid_cardinality"
        subject = str(row["subject"]).strip()
        predicate = str(row["predicate"]).strip()
        object_value = str(row.get("object", "")).strip()
        if not subject or not predicate or (operation == "ASSERT" and not object_value):
            return None, "blank_fact_field"
        quote = str(row["evidence_quote"]).strip()
        if len(quote) < 4 or len(quote) > 400 or quote not in session.text:
            return None, "quote_not_in_evidence"
        if not _quote_has_user_support(session.text, quote):
            return None, "quote_lacks_user_support"
        try:
            confidence = float(row["confidence"])
        except (TypeError, ValueError):
            return None, "invalid_confidence"
        if not 0 <= confidence <= 1:
            return None, "invalid_confidence"
        if confidence < self.minimum_confidence:
            return None, "below_confidence_threshold"
        identity_payload = json.dumps(
            {
                "session_id": session_id,
                "memory_key": memory_key,
                "operation": operation,
                "cardinality": cardinality,
                "subject": subject,
                "predicate": predicate,
                "object": object_value,
                "evidence_quote": quote,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            FactCandidate(
                candidate_id="candidate:" + _sha256(identity_payload),
                session_id=session_id,
                memory_key=memory_key,
                operation=operation,
                cardinality=cardinality,
                subject=subject,
                predicate=predicate,
                object=object_value,
                evidence_quote=quote,
                confidence=confidence,
                scope_key=session.scope_key,
                valid_from=session.valid_from,
            ),
            "",
        )


class ShadowJournal:
    """Apply validated proposals to an isolated evidence-bound store."""

    def __init__(self, store: SQLiteMemoryStore):
        self.store = store
        self.events: list[JournalEvent] = []
        self._active: dict[tuple[str, str], list[MemoryCard]] = {}
        self._cardinalities: dict[tuple[str, str], str] = {}
        self._aliases: dict[tuple[str, str], tuple[str, str]] = {}

    def apply(
        self,
        candidate: FactCandidate,
        session: ShadowSession,
    ) -> JournalEvent:
        if candidate.session_id != session.session_id:
            raise ValueError("candidate and evidence session do not match")
        proposed_family = (candidate.scope_key, candidate.memory_key)
        family = self._resolve_family(candidate, proposed_family)
        resolved_alias = family != proposed_family
        active = self._active.setdefault(family, [])
        known_cardinality = self._cardinalities.get(family)
        if known_cardinality is not None and known_cardinality != candidate.cardinality:
            event = self._event(candidate, "rejected", "cardinality_changed")
            self.events.append(event)
            return event
        self._cardinalities.setdefault(family, candidate.cardinality)
        if candidate.operation == "FORGET":
            if active and self._is_older_than_active(candidate, active):
                event = self._event(candidate, "ignored", "stale_forget")
            else:
                for card in active:
                    self.store.transition(card.id, "tombstoned")
                marker = self._add_forget_marker(candidate, session)
                had_active_fact = bool(active)
                active[:] = [marker]
                event = self._event(
                    candidate,
                    "tombstoned" if had_active_fact else "forget_marker",
                    (
                        "explicit_forget_resolved_alias"
                        if resolved_alias
                        else "explicit_forget"
                        if had_active_fact
                        else "explicit_forget_without_active_fact"
                    ),
                    marker.id,
                )
            self.events.append(event)
            return event

        for card in active:
            if self._same_fact(card, candidate):
                event = self._event(candidate, "ignored", "duplicate_fact", card.id)
                self.events.append(event)
                return event

        value = self._render_fact(candidate)
        cues = tuple(
            dict.fromkeys(
                (
                    candidate.memory_key,
                    *lexical_tokens(
                        f"{candidate.subject} {candidate.predicate} "
                        f"{candidate.object} {candidate.evidence_quote}"
                    ),
                )
            )
        )[:24]
        source = EvidenceSource(
            source_type="shadow_conversation_candidate",
            source_ref=f"conversation:{candidate.session_id}",
            source_sha256=_sha256(session.text),
            origin="user",
        )
        common = {
            "value": value,
            "abstraction": (
                f"{candidate.subject} {candidate.predicate} {candidate.object}"
            ).strip(),
            "cue_anchors": cues,
            "kind": "semantic",
            "subtype": "shadow_extracted_fact",
            "scope": "user",
            "scope_key": candidate.scope_key,
            "origin": "agent",
            "authority": "inform",
            "confidence": candidate.confidence,
            "importance": 0.8,
            "valid_from": candidate.valid_from,
            "sources": (source,),
        }
        if candidate.cardinality == "singleton" and active:
            newest = active[-1]
            if self._is_older_than_active(candidate, active):
                card = self.store.add_card(**common)
                self.store.transition(card.id, "stale")
                event = self._event(
                    candidate, "stale", "out_of_order_singleton", card.id
                )
            else:
                card = self.store.supersede(newest.id, **common)
                active[:] = [card]
                event = self._event(
                    candidate, "superseded", "changed_singleton", card.id
                )
        else:
            card = self.store.add_card(**common)
            active.append(card)
            event = self._event(candidate, "asserted", "new_fact", card.id)
        self.events.append(event)
        return event

    def _resolve_family(
        self,
        candidate: FactCandidate,
        proposed: tuple[str, str],
    ) -> tuple[str, str]:
        if proposed in self._active:
            return proposed
        aliased = self._aliases.get(proposed)
        if aliased is not None:
            return aliased
        if candidate.operation != "FORGET":
            return proposed
        incoming_tokens = self._key_tokens(candidate.memory_key)
        scored: list[tuple[float, tuple[str, str]]] = []
        for family, cardinality in self._cardinalities.items():
            if family[0] != candidate.scope_key or cardinality != candidate.cardinality:
                continue
            existing_tokens = self._key_tokens(family[1])
            union = incoming_tokens | existing_tokens
            score = (
                len(incoming_tokens & existing_tokens) / len(union) if union else 0.0
            )
            if score >= 0.6:
                scored.append((score, family))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if not scored:
            return proposed
        if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.15:
            return proposed
        resolved = scored[0][1]
        self._aliases[proposed] = resolved
        return resolved

    @staticmethod
    def _key_tokens(memory_key: str) -> set[str]:
        return {
            token
            for token in re.split(r"[^a-z0-9]+", memory_key.casefold())
            if len(token) >= 2
        }

    @staticmethod
    def _same_fact(card: MemoryCard, candidate: FactCandidate) -> bool:
        marker = f"object: {candidate.object}"
        return marker.casefold() in card.value.casefold()

    @staticmethod
    def _is_older_than_active(
        candidate: FactCandidate, active: Sequence[MemoryCard]
    ) -> bool:
        incoming = datetime.fromisoformat(candidate.valid_from.replace("Z", "+00:00"))
        active_moments = (
            datetime.fromisoformat(card.valid_from.replace("Z", "+00:00"))
            for card in active
            if card.valid_from
        )
        return any(incoming < moment for moment in active_moments)

    @staticmethod
    def _render_fact(candidate: FactCandidate) -> str:
        return "\n".join(
            (
                "Evidence-bound structured fact (shadow evaluation):",
                (
                    f"fact: {candidate.subject}的{candidate.predicate}是"
                    f"{candidate.object}。"
                ),
                f"memory_key: {candidate.memory_key}",
                f"subject: {candidate.subject}",
                f"predicate: {candidate.predicate}",
                f"object: {candidate.object or '[forgotten]'}",
                f"valid_from: {candidate.valid_from}",
                f"source_quote: {candidate.evidence_quote}",
            )
        )

    def _add_forget_marker(
        self, candidate: FactCandidate, session: ShadowSession
    ) -> MemoryCard:
        source = EvidenceSource(
            source_type="shadow_conversation_candidate",
            source_ref=f"conversation:{candidate.session_id}:forget",
            source_sha256=_sha256(session.text),
            origin="user",
        )
        return self.store.add_card(
            value="\n".join(
                (
                    "Structured memory control (shadow candidate):",
                    f"memory_key: {candidate.memory_key}",
                    f"subject: {candidate.subject}",
                    f"predicate: {candidate.predicate}",
                    "object: [forgotten]",
                    "status: explicitly forgotten by the user",
                    f"valid_from: {candidate.valid_from}",
                    f"source_quote: {candidate.evidence_quote}",
                    "instruction: do not infer this attribute from other memories",
                )
            ),
            abstraction=(
                f"{candidate.subject} {candidate.predicate} 已明确忘记，不得推断"
            ),
            cue_anchors=tuple(
                dict.fromkeys(
                    (
                        candidate.memory_key,
                        *lexical_tokens(
                            f"{candidate.subject} {candidate.predicate} "
                            f"{candidate.evidence_quote} 忘记"
                        ),
                    )
                )
            )[:24],
            kind="state",
            subtype="shadow_forget_marker",
            scope="user",
            scope_key=candidate.scope_key,
            origin="agent",
            authority="inform",
            confidence=candidate.confidence,
            importance=1.0,
            valid_from=candidate.valid_from,
            sources=(source,),
        )

    @staticmethod
    def _event(
        candidate: FactCandidate,
        outcome: str,
        reason: str,
        card_id: str = "",
    ) -> JournalEvent:
        return JournalEvent(
            candidate_id=candidate.candidate_id,
            session_id=candidate.session_id,
            memory_key=candidate.memory_key,
            proposed_operation=candidate.operation,
            outcome=outcome,
            reason=reason,
            card_id=card_id,
        )
