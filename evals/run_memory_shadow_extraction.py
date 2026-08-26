"""Compare raw session memory with model-extracted shadow fact candidates."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from langchain_core.messages import HumanMessage

from evals.memory_shadow_candidates import (
    ShadowJournal,
    ShadowSession,
    StructuredCandidateExtractor,
)
from evals.run_memory_long_conversation import (
    FINAL_MOMENT,
    MAIN_SCOPE,
    OTHER_SCOPE,
    ConversationCase,
    _answer_correct,
    _reader_prompt,
    _response_text,
    build_fixture,
)
from mini_code_agent.cli import _load_runtime_env
from mini_code_agent.memory_retrieval import (
    EvidenceTemporalRetriever,
    MemoryQuery,
    MemoryScope,
)
from mini_code_agent.memory_store import SQLiteMemoryStore
from mini_code_agent.model import create_model

SUITE_NAME = "memory-shadow-extraction-v0"
SCRIPTED_INDEXES = (9, 17, 22, 28, 35, 41, 48, 56, 63, 72, 87, 91, 96)
FILLER_INDEXES = (
    2,
    7,
    13,
    19,
    25,
    31,
    38,
    44,
    52,
    60,
    68,
    77,
    84,
    89,
    99,
    104,
    109,
    113,
    117,
    120,
)
EXPECTED_ACTIVE = {
    "current-preferred-name": ("Eddy",),
    "food-allergy": ("榛子",),
    "workshop-plan": ("苏州", "高铁"),
    "earlier-dentist": ("3月12日", "7月18日"),
    "current-database": ("PostgreSQL",),
    "notification-scope": ("电子邮件",),
    "assistant-side-room": ("Cedar Room",),
}


def _timestamp_from_session(text: str) -> str:
    first_line = text.splitlines()[0]
    start = first_line.find("(")
    end = first_line.rfind(")")
    if start < 0 or end <= start:
        raise ValueError("session timestamp is missing")
    return first_line[start + 1 : end]


def _session_id(text: str) -> str:
    first_line = text.splitlines()[0]
    number = first_line.removeprefix("Session ").split(" ", 1)[0]
    return f"main-{number}"


def _selected_sessions(fixture: Any) -> tuple[ShadowSession, ...]:
    selected = []
    filler = set(FILLER_INDEXES)
    for index in (*SCRIPTED_INDEXES, *FILLER_INDEXES):
        text = fixture.sessions[index - 1]
        selected.append(
            ShadowSession(
                session_id=_session_id(text),
                text=text,
                scope_key=MAIN_SCOPE,
                valid_from=_timestamp_from_session(text),
                is_filler=index in filler,
            )
        )
    other = fixture.labels["notification_other_user"]
    selected.append(
        ShadowSession(
            session_id="other-121",
            text=other.value,
            scope_key=OTHER_SCOPE,
            valid_from=other.valid_from or FINAL_MOMENT,
        )
    )
    return tuple(sorted(selected, key=lambda item: item.valid_from))


def _batches(
    sessions: Sequence[ShadowSession], size: int
) -> tuple[tuple[ShadowSession, ...], ...]:
    return tuple(
        tuple(sessions[index : index + size]) for index in range(0, len(sessions), size)
    )


def _query(case: ConversationCase) -> MemoryQuery:
    return MemoryQuery(
        case.question,
        scopes=(MemoryScope("user", MAIN_SCOPE),),
        as_of=FINAL_MOMENT,
        required_authority="inform",
        limit=3,
    )


def _candidate_recall(store: SQLiteMemoryStore) -> dict[str, Any]:
    active_text = "\n".join(card.value for card in store.list_cards())
    details = []
    found = 0
    expected = 0
    for case_name, markers in EXPECTED_ACTIVE.items():
        matched = tuple(marker for marker in markers if marker in active_text)
        found += len(matched)
        expected += len(markers)
        details.append(
            {
                "case": case_name,
                "expected": len(markers),
                "found": len(matched),
                "markers_found": matched,
            }
        )
    return {
        "found": found,
        "expected": expected,
        "recall": round(found / expected, 4),
        "cases": details,
    }


def _run_reader(
    model: Any,
    fixture: Any,
    raw_retriever: EvidenceTemporalRetriever,
    shadow_retriever: EvidenceTemporalRetriever,
) -> list[dict[str, Any]]:
    systems = {
        "raw_session_memory": raw_retriever,
        "structured_shadow_memory": shadow_retriever,
    }
    results = []
    for name, retriever in systems.items():
        correct = 0
        details = []
        for case in fixture.cases:
            context = retriever.retrieve(_query(case)).render()
            answer = _response_text(
                model.invoke(
                    [HumanMessage(content=_reader_prompt(case.question, context))]
                )
            )
            passed = _answer_correct(case, answer)
            correct += int(passed)
            details.append(
                {
                    "case": case.name,
                    "ability": case.ability,
                    "correct": passed,
                    "answer": answer,
                    "context_chars": len(context),
                }
            )
        results.append(
            {
                "system": name,
                "correct": correct,
                "accuracy": round(correct / len(fixture.cases), 4),
                "cases": details,
            }
        )
    return results


def run_shadow_extraction(
    *,
    model_name: str,
    provider: str = "auto",
    base_url: str | None = None,
    batch_size: int = 7,
) -> dict[str, Any]:
    if batch_size < 1 or batch_size > 20:
        raise ValueError("batch size must be between 1 and 20")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mca-memory-shadow-") as raw:
        root = Path(raw)
        fixture = build_fixture(root / "primary")
        shadow_store = SQLiteMemoryStore(root / "shadow")
        sessions = _selected_sessions(fixture)
        indexed = {item.session_id: item for item in sessions}
        model = create_model(
            model_name,
            provider=provider,  # type: ignore[arg-type]
            base_url=base_url,
            temperature=0.0,
            request_timeout=90.0,
            max_retries=2,
        )
        extractor = StructuredCandidateExtractor(model)
        journal = ShadowJournal(shadow_store)
        extraction_rejections = []
        response_hashes = []
        extracted = []
        extraction_calls = 0
        for batch in _batches(sessions, batch_size):
            result = extractor.extract(batch)
            extraction_calls += 1
            response_hashes.append(result.raw_response_sha256)
            extraction_rejections.extend(result.rejected)
            for candidate in result.candidates:
                extracted.append(candidate)
                journal.apply(candidate, indexed[candidate.session_id])

        active_cards = shadow_store.list_cards()
        inactive_cards = shadow_store.list_cards(include_inactive=True)
        filler_ids = {item.session_id for item in sessions if item.is_filler}
        filler_candidates = [
            item for item in extracted if item.session_id in filler_ids
        ]
        lifecycle_counts: dict[str, int] = {}
        for event in journal.events:
            lifecycle_counts[event.outcome] = lifecycle_counts.get(event.outcome, 0) + 1

        reader_results = _run_reader(
            model,
            fixture,
            EvidenceTemporalRetriever(fixture.store),
            EvidenceTemporalRetriever(shadow_store),
        )
        verification = shadow_store.verify()
        recall = _candidate_recall(shadow_store)
        active_ids = {card.id for card in active_cards}
        hotel_active = any(
            event.session_id == "main-041" and event.card_id in active_ids
            for event in journal.events
        )
        cross_scope_sms_active = any(
            card.scope_key == OTHER_SCOPE and "短信" in card.value
            for card in active_cards
        )
        return {
            "suite": SUITE_NAME,
            "model": model_name,
            "provider": provider,
            "scope": {
                "primary_store_mutated": False,
                "shadow_only": True,
                "embedding": False,
                "sessions_sampled": len(sessions),
                "scripted_sessions": len(SCRIPTED_INDEXES) + 1,
                "filler_sessions": len(FILLER_INDEXES),
                "cases": len(fixture.cases),
            },
            "model_calls": extraction_calls + 2 * len(fixture.cases),
            "extraction_calls": extraction_calls,
            "extraction": {
                "candidates": len(extracted),
                "rejected": len(extraction_rejections),
                "filler_candidates": len(filler_candidates),
                "filler_false_positive_rate": round(
                    len({item.session_id for item in filler_candidates})
                    / len(FILLER_INDEXES),
                    4,
                ),
                "candidate_recall": recall,
                "response_sha256": response_hashes,
                "rejection_reasons": [item.reason for item in extraction_rejections],
                "candidate_actions": [
                    {
                        "session_id": item.session_id,
                        "memory_key": item.memory_key,
                        "operation": item.operation,
                        "cardinality": item.cardinality,
                    }
                    for item in extracted
                ],
            },
            "lifecycle": {
                "outcomes": lifecycle_counts,
                "active_cards": len(active_cards),
                "all_cards": len(inactive_cards),
                "hotel_preference_forgotten": not hotel_active,
                "other_scope_sms_present": cross_scope_sms_active,
                "events": [
                    {
                        "session_id": event.session_id,
                        "memory_key": event.memory_key,
                        "operation": event.proposed_operation,
                        "outcome": event.outcome,
                        "reason": event.reason,
                    }
                    for event in journal.events
                ],
            },
            "reader_results": reader_results,
            "shadow_store_integrity": verification.ok,
            "elapsed_seconds": round(time.monotonic() - started, 4),
            "claims_boundary": (
                "Experimental shadow extraction from a sampled corpus; candidates "
                "do not mutate production or primary memory."
            ),
        }


def _print_report(report: dict[str, Any]) -> None:
    extraction = report["extraction"]
    recall = extraction["candidate_recall"]
    print(
        f"suite: {report['suite']}; model={report['model']}; "
        f"calls={report['model_calls']}"
    )
    print(
        f"extraction: candidates={extraction['candidates']} "
        f"rejected={extraction['rejected']} "
        f"recall={recall['found']}/{recall['expected']} ({recall['recall']:.1%}) "
        f"filler_fp={extraction['filler_false_positive_rate']:.1%}"
    )
    print(
        f"lifecycle: {report['lifecycle']['outcomes']}; "
        f"forgotten={report['lifecycle']['hotel_preference_forgotten']}; "
        f"integrity={report['shadow_store_integrity']}"
    )
    for result in report["reader_results"]:
        print(
            f"reader {result['system']:<28} "
            f"{result['correct']}/{report['scope']['cases']} "
            f"({result['accuracy']:.1%})"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--provider", choices=("auto", "openai", "deepseek"), default="auto"
    )
    parser.add_argument("--base-url")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--batch-size", type=int, default=7)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    _load_runtime_env(args.env_file)
    report = run_shadow_extraction(
        model_name=args.model,
        provider=args.provider,
        base_url=args.base_url,
        batch_size=args.batch_size,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    if args.json:
        print(payload)
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
