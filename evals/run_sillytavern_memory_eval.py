"""Exercise the portable SillyTavern adapter with real-model shadow extraction."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from evals.memory_shadow_candidates import (
    ShadowJournal,
    ShadowSession,
    StructuredCandidateExtractor,
)
from memory_core.adapters.sillytavern import import_sillytavern_chat
from memory_core.conversation import FormationPolicy, plan_formation
from mini_code_agent.cli import _load_runtime_env
from mini_code_agent.memory_store import SQLiteMemoryStore
from mini_code_agent.model import create_model

SUITE_NAME = "sillytavern-portable-memory-v0"
SCOPE_KEY = "user:sillytavern-eval"
EXPECTED_ACTIVE = {
    "preferred_name": ("Eddy",),
    "allergy": ("榛子",),
    "database": ("PostgreSQL",),
    "notification": ("电子邮件",),
}


class ScriptedExtractionModel:
    """Deterministic extraction oracle for end-to-end wiring regression."""

    def invoke(self, messages):
        prompt = str(messages[-1].content)
        candidates = []

        def add(
            memory_key: str,
            operation: str,
            object_value: str,
            quote: str,
            predicate: str,
        ) -> None:
            candidates.append(
                {
                    "session_id": _session_id_from_prompt(prompt),
                    "memory_key": memory_key,
                    "operation": operation,
                    "cardinality": "singleton",
                    "subject": "用户",
                    "predicate": predicate,
                    "object": object_value,
                    "evidence_quote": quote,
                    "confidence": 0.99,
                }
            )

        if "以后请叫我小林" in prompt:
            add("user.preferred_name", "ASSERT", "小林", "以后请叫我小林", "偏好称呼")
        if "我对榛子过敏" in prompt:
            add("user.allergy.hazelnut", "ASSERT", "榛子", "我对榛子过敏", "过敏原")
        if "数据库先用SQLite" in prompt:
            add(
                "project.atlas.database",
                "ASSERT",
                "SQLite",
                "Atlas项目当前数据库先用SQLite",
                "Atlas数据库",
            )
        if "River Hotel，请记住" in prompt:
            add(
                "user.hotel_preference",
                "ASSERT",
                "River Hotel",
                "我常住酒店偏好River Hotel，请记住",
                "住宿偏好",
            )
        if "以后请叫我Eddy" in prompt:
            add(
                "user.preferred_name",
                "ASSERT",
                "Eddy",
                "以后请叫我Eddy，小林这个称呼作废",
                "偏好称呼",
            )
        if "数据库改为PostgreSQL" in prompt:
            add(
                "project.atlas.database",
                "ASSERT",
                "PostgreSQL",
                "Atlas项目数据库改为PostgreSQL，SQLite决定作废",
                "Atlas数据库",
            )
        if "所有提醒只通过电子邮件发送" in prompt:
            add(
                "user.notification_channel",
                "ASSERT",
                "电子邮件",
                "所有提醒只通过电子邮件发送",
                "提醒渠道",
            )
        if "忘掉我对River Hotel" in prompt:
            add(
                "user.hotel_preference",
                "FORGET",
                "",
                "忘掉我对River Hotel的住宿偏好",
                "住宿偏好",
            )
        return json.dumps({"candidates": candidates}, ensure_ascii=False)


def _session_id_from_prompt(prompt: str) -> str:
    marker = '"session_id": "'
    start = prompt.find(marker)
    if start < 0:
        raise ValueError("scripted extractor prompt has no session id")
    start += len(marker)
    end = prompt.find('"', start)
    return prompt[start:end]


def _message(
    name: str,
    text: str,
    *,
    user: bool,
    index: int,
    system: bool = False,
    summary: str = "",
) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if summary:
        extra["memory"] = summary
    return {
        "name": name,
        "is_user": user,
        "is_system": system,
        "send_date": f"2026-08-19T10:{index:02d}:00Z",
        "mes": text,
        "extra": extra,
    }


def build_sillytavern_fixture() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "chat_metadata": {"integrity": "eval-chat"},
            "user_name": "unused",
            "character_name": "unused",
        }
    ]
    turns = (
        ("Eddy", "以后请叫我小林。", True, False, ""),
        ("Alice", "好的，我会这样称呼你。", False, False, ""),
        ("Eddy", "今天天气不错，我们随便聊聊。", True, False, ""),
        ("Alice", "确实很适合散步。", False, False, ""),
        ("Eddy", "我对榛子过敏，这是长期医疗约束。", True, False, ""),
        (
            "Alice",
            "收到。",
            False,
            False,
            "用户希望被称为小林，对榛子过敏，并且最喜欢蓝色。",
        ),
        ("Eddy", "Atlas项目当前数据库先用SQLite。", True, False, ""),
        ("Alice", "我记下这个项目决定。", False, False, ""),
        ("Eddy", "我常住酒店偏好River Hotel，请记住。", True, False, ""),
        ("Alice", "好的。", False, False, ""),
        ("Eddy", "午饭吃了面条，这不是长期偏好。", True, False, ""),
        (
            "Alice",
            "继续吧。",
            False,
            False,
            "Atlas使用SQLite；用户偏好River Hotel。",
        ),
        ("Eddy", "更正一下，以后请叫我Eddy，小林这个称呼作废。", True, False, ""),
        ("Alice", "明白，已更正。", False, False, ""),
        ("Eddy", "Atlas项目数据库改为PostgreSQL，SQLite决定作废。", True, False, ""),
        ("Alice", "项目决定已更新。", False, False, ""),
        ("Eddy", "所有提醒只通过电子邮件发送。", True, True, ""),
        (
            "Alice",
            "收到。",
            False,
            False,
            "用户称呼为Eddy；Atlas使用PostgreSQL；提醒走电子邮件。",
        ),
        ("Eddy", "忘掉我对River Hotel的住宿偏好。", True, False, ""),
        ("Alice", "好的，这项偏好不再保留。", False, False, ""),
        ("Alice", "我猜你的护照号是P123，应该长期保存。", False, False, ""),
        ("Eddy", "我们换个话题，聊聊电影吧。", True, False, ""),
    )
    rows.extend(
        _message(
            name,
            text,
            user=user,
            index=index,
            system=system,
            summary=summary,
        )
        for index, (name, text, user, system, summary) in enumerate(turns)
    )
    return rows


def _formation_sessions(events) -> tuple[ShadowSession, ...]:
    sessions = []
    cursor = None
    policy = FormationPolicy(
        message_interval=5,
        protect_recent_messages=2,
        max_messages_per_batch=5,
    )
    while True:
        plan = plan_formation(events, policy=policy, after_event_id=cursor)
        if not plan.should_form:
            break
        lines = []
        for event in plan.selected:
            prefix = {
                "user": "用户：",
                "assistant": "助手：",
                "system": "系统：",
                "narrator": "旁白：",
                "tool": "工具：",
            }[event.role]
            lines.append(prefix + event.content)
        session_id = f"st-batch-{len(sessions) + 1}"
        sessions.append(
            ShadowSession(
                session_id=session_id,
                text="\n".join(lines),
                scope_key=SCOPE_KEY,
                valid_from=plan.selected[-1].created_at,
            )
        )
        cursor = plan.selected[-1].event_id
    return tuple(sessions)


def run_eval(
    *,
    model_name: str,
    provider: str = "auto",
    base_url: str | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    imported = import_sillytavern_chat(
        build_sillytavern_fixture(),
        conversation_id="st-eval-chat",
        character_id="alice",
        user_id="eddy",
        namespace="eval",
    )
    sessions = _formation_sessions(imported.events)
    indexed = {item.session_id: item for item in sessions}
    model = (
        ScriptedExtractionModel()
        if model_name == "scripted"
        else create_model(
            model_name,
            provider=provider,  # type: ignore[arg-type]
            base_url=base_url,
            temperature=0.0,
            request_timeout=90.0,
            max_retries=2,
        )
    )
    with tempfile.TemporaryDirectory(prefix="mca-st-memory-") as raw:
        store = SQLiteMemoryStore(Path(raw) / "shadow")
        journal = ShadowJournal(store)
        candidates = []
        rejections = []
        response_hashes = []
        for session in sessions:
            extraction = StructuredCandidateExtractor(model).extract((session,))
            candidates.extend(extraction.candidates)
            rejections.extend(extraction.rejected)
            response_hashes.append(extraction.raw_response_sha256)
            for candidate in extraction.candidates:
                journal.apply(candidate, indexed[candidate.session_id])

        active_cards = store.list_cards()
        all_cards = store.list_cards(include_inactive=True)
        active_text = "\n".join(card.value for card in active_cards)
        expected = {
            name: all(marker in active_text for marker in markers)
            for name, markers in EXPECTED_ACTIVE.items()
        }
        hotel_forgotten = not any(
            card.subtype == "shadow_extracted_fact" and "River Hotel" in card.value
            for card in active_cards
        ) and any(card.subtype == "shadow_forget_marker" for card in active_cards)
        assistant_passport_rejected = "P123" not in active_text
        hallucinated_summary_rejected = "蓝色" not in active_text
        integrity = store.verify().ok
        passed = (
            all(expected.values())
            and hotel_forgotten
            and assistant_passport_rejected
            and hallucinated_summary_rejected
            and integrity
        )
        return {
            "suite": SUITE_NAME,
            "model": model_name,
            "provider": provider,
            "passed": passed,
            "scope": {
                "raw_messages": len(imported.events),
                "imported_checkpoints": len(imported.checkpoints),
                "formation_batches": len(sessions),
                "protected_recent_messages": 2,
                "embedding": False,
                "shadow_only": True,
            },
            "extraction": {
                "model_calls": len(sessions),
                "candidates": len(candidates),
                "rejected": len(rejections),
                "response_sha256": response_hashes,
                "actions": [
                    {
                        "session_id": item.session_id,
                        "memory_key": item.memory_key,
                        "operation": item.operation,
                        "cardinality": item.cardinality,
                    }
                    for item in candidates
                ],
                "rejection_reasons": [item.reason for item in rejections],
            },
            "outcomes": {
                "expected_active": expected,
                "hotel_forgotten": hotel_forgotten,
                "assistant_passport_rejected": assistant_passport_rejected,
                "hallucinated_summary_rejected": hallucinated_summary_rejected,
                "active_cards": len(active_cards),
                "all_cards": len(all_cards),
                "store_integrity": integrity,
                "journal": [
                    {
                        "session_id": item.session_id,
                        "memory_key": item.memory_key,
                        "operation": item.proposed_operation,
                        "outcome": item.outcome,
                        "reason": item.reason,
                    }
                    for item in journal.events
                ],
            },
            "elapsed_seconds": round(time.monotonic() - started, 4),
            "claims_boundary": (
                "Synthetic SillyTavern-format chat, "
                + (
                    "deterministic extraction oracle"
                    if model_name == "scripted"
                    else "real model extraction"
                )
                + ", and isolated shadow storage; no production memory was mutated."
            ),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--provider", choices=("auto", "openai", "deepseek"), default="auto"
    )
    parser.add_argument("--base-url")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    _load_runtime_env(args.env_file)
    report = run_eval(
        model_name=args.model,
        provider=args.provider,
        base_url=args.base_url,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    if args.json:
        print(payload)
    else:
        print(
            f"suite: {report['suite']}; model={report['model']}; "
            f"passed={report['passed']}; calls={report['extraction']['model_calls']}"
        )
        print(f"expected_active: {report['outcomes']['expected_active']}")
        print(
            "guards: "
            f"forgotten={report['outcomes']['hotel_forgotten']} "
            f"assistant_only_rejected={report['outcomes']['assistant_passport_rejected']} "
            f"summary_hallucination_rejected={report['outcomes']['hallucinated_summary_rejected']} "
            f"integrity={report['outcomes']['store_integrity']}"
        )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
