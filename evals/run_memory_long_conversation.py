"""Diagnose long-conversation reading after explicit authenticated ingestion.

This is deliberately not an extraction benchmark: the production chat runtime
does not yet convert free conversation into durable memory. The harness stores
timestamped sessions as user-origin evidence, then measures retrieval and an
optional real-model reader across LongMemEval-style ability categories.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from langchain_core.messages import HumanMessage

from mini_code_agent.memory_models import EvidenceSource, MemoryCard
from mini_code_agent.memory_retrieval import (
    EvidenceTemporalRetriever,
    MemoryQuery,
    MemoryScope,
    lexical_tokens,
)
from mini_code_agent.memory_store import SQLiteMemoryStore
from mini_code_agent.model import create_model

SUITE_NAME = "memory-long-conversation-diagnostic-v0"
MAIN_SCOPE = "user:long-dialogue-main"
OTHER_SCOPE = "user:long-dialogue-other"
FINAL_MOMENT = "2027-01-01T00:00:00+00:00"


@dataclass(frozen=True)
class ConversationCase:
    name: str
    ability: str
    question: str
    expected_markers: tuple[str, ...] = ()
    expected_labels: tuple[str, ...] = ()
    expected_abstain: bool = False


@dataclass(frozen=True)
class ConversationFixture:
    store: SQLiteMemoryStore
    sessions: tuple[str, ...]
    labels: dict[str, MemoryCard]
    cases: tuple[ConversationCase, ...]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _timestamp(index: int) -> str:
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
    return moment.isoformat()


def _session_text(index: int, body: str) -> str:
    return f"Session {index:03d} ({_timestamp(index)}):\n{body}"


def _source(label: str) -> EvidenceSource:
    return EvidenceSource(
        source_type="authenticated_conversation_session",
        source_ref=f"conversation:{label}",
        source_sha256=_sha256(label),
        origin="user",
    )


def _cues(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(lexical_tokens(text)))[:24]


def _add_session(
    store: SQLiteMemoryStore,
    *,
    index: int,
    label: str,
    body: str,
    scope_key: str = MAIN_SCOPE,
) -> tuple[MemoryCard, str]:
    text = _session_text(index, body)
    card = store.add_card(
        value=text,
        abstraction=text,
        cue_anchors=_cues(text),
        kind="episodic",
        subtype="conversation_session",
        scope="user",
        scope_key=scope_key,
        origin="user",
        authority="inform",
        confidence=1.0,
        importance=0.8,
        valid_from=_timestamp(index),
        sources=(_source(label),),
    )
    return card, text


def _supersede_session(
    store: SQLiteMemoryStore,
    old: MemoryCard,
    *,
    index: int,
    label: str,
    body: str,
) -> tuple[MemoryCard, str]:
    text = _session_text(index, body)
    card = store.supersede(
        old.id,
        value=text,
        abstraction=text,
        cue_anchors=_cues(text),
        kind="episodic",
        subtype="conversation_session",
        scope="user",
        scope_key=MAIN_SCOPE,
        origin="user",
        authority="inform",
        confidence=1.0,
        importance=0.8,
        valid_from=_timestamp(index),
        sources=(_source(label),),
    )
    return card, text


def _filler(index: int) -> str:
    topics = (
        "烤面包时如何控制水量和发酵时间",
        "傍晚散步看到的树木、云层和街灯",
        "一本历史书的章节结构与装帧颜色",
        "整理厨房抽屉、杯子和调味罐的过程",
        "周末观看纪录片后记录的镜头与配乐",
        "给阳台植物浇水并观察新叶的日常",
    )
    topic = topics[index % len(topics)]
    detail = "；".join(
        f"第{part}段只是当日闲聊细节{index}-{part}，没有形成需要长期遵循的决定"
        for part in range(1, 9)
    )
    return f"用户：今天聊聊{topic}。{detail}。\n助手：收到，这些是普通闲聊记录。"


def build_fixture(directory: Path) -> ConversationFixture:
    store = SQLiteMemoryStore(directory)
    sessions: list[str] = []
    labels: dict[str, MemoryCard] = {}
    old_name: MemoryCard | None = None
    old_database: MemoryCard | None = None
    old_hotel: MemoryCard | None = None

    scripted = {
        9: (
            "preferred_name_old",
            "用户：关于称呼，以后请叫我小林。\n助手：好的，我会使用小林这个称呼。",
        ),
        17: (
            "allergy",
            "用户：请记住，我对榛子严重过敏，餐食建议必须避开榛子。\n助手：记住了。",
        ),
        22: (
            "database_old",
            "用户：Atlas 项目的数据库决定使用 SQLite。\n助手：已记录 Atlas 的存储决定。",
        ),
        28: (
            "workshop_place",
            "用户：五月设计工作坊的地点已经确定，在苏州工业园区。\n助手：地点是苏州工业园区。",
        ),
        35: (
            "dentist_early",
            "用户：我的较早一次牙医预约是 3月12日 上午。\n助手：已记下这个预约日期。",
        ),
        41: (
            "hotel_old",
            "用户：订酒店时我偏好远离电梯的安静房间。\n助手：我会按安静房间偏好推荐。",
        ),
        48: (
            "notification_main",
            "用户：通知方式请固定使用电子邮件，不要发短信。\n助手：之后使用电子邮件通知。",
        ),
        56: (
            "assistant_room",
            "助手：按照我们刚才的确认，我已经预订 Cedar Room 作为评审会议室。\n用户：可以，就用它。",
        ),
        63: (
            "workshop_travel",
            "用户：去五月设计工作坊我打算坐高铁，5月9日上午出发。\n助手：已记录高铁出行计划。",
        ),
        72: (
            "dentist_late",
            "用户：我后来又约了一次牙医，日期是 7月18日 下午。\n助手：已记录第二次预约。",
        ),
    }

    for index in range(1, 121):
        if index == 87:
            if old_name is None:
                raise RuntimeError("old preferred-name session is missing")
            card, text = _supersede_session(
                store,
                old_name,
                index=index,
                label="preferred_name_current",
                body=(
                    "用户：更新一下称呼，以后请叫我 Eddy，之前的小林称呼作废。\n"
                    "助手：明白，当前称呼更新为 Eddy。"
                ),
            )
            labels["preferred_name_current"] = card
        elif index == 91:
            if old_database is None:
                raise RuntimeError("old database session is missing")
            card, text = _supersede_session(
                store,
                old_database,
                index=index,
                label="database_current",
                body=(
                    "用户：Atlas 的存储决定已经更新为 PostgreSQL，之前的 SQLite 决定作废。\n"
                    "助手：已更新为 PostgreSQL。"
                ),
            )
            labels["database_current"] = card
        elif index == 96:
            if old_hotel is None:
                raise RuntimeError("old hotel session is missing")
            text = _session_text(
                index,
                "用户：请忘掉我的酒店房间偏好，不要再据此推荐。\n助手：已忘记这项偏好。",
            )
            store.transition(old_hotel.id, "tombstoned")
        elif index in scripted:
            label, body = scripted[index]
            card, text = _add_session(
                store,
                index=index,
                label=label,
                body=body,
            )
            labels[label] = card
            if label == "preferred_name_old":
                old_name = card
            elif label == "database_old":
                old_database = card
            elif label == "hotel_old":
                old_hotel = card
        else:
            _card, text = _add_session(
                store,
                index=index,
                label=f"filler-{index}",
                body=_filler(index),
            )
        sessions.append(text)

    other, _text = _add_session(
        store,
        index=121,
        label="notification_other_user",
        body="用户：通知方式只使用短信，不要发电子邮件。",
        scope_key=OTHER_SCOPE,
    )
    labels["notification_other_user"] = other

    cases = (
        ConversationCase(
            "current-preferred-name",
            "knowledge-update",
            "我现在希望你怎么称呼我？",
            ("Eddy",),
            ("preferred_name_current",),
        ),
        ConversationCase(
            "food-allergy",
            "single-session-user",
            "给我推荐餐食时必须避开什么过敏原？",
            ("榛子",),
            ("allergy",),
        ),
        ConversationCase(
            "workshop-plan",
            "multi-session",
            "五月设计工作坊在哪里，我打算怎么去？",
            ("苏州", "高铁"),
            ("workshop_place", "workshop_travel"),
        ),
        ConversationCase(
            "earlier-dentist",
            "temporal-reasoning",
            "两次牙医预约中，较早的一次是哪天？",
            ("3月12日",),
            ("dentist_early", "dentist_late"),
        ),
        ConversationCase(
            "current-database",
            "knowledge-update",
            "Atlas 当前决定使用什么数据库？",
            ("PostgreSQL",),
            ("database_current",),
        ),
        ConversationCase(
            "forgotten-hotel-preference",
            "abstention",
            "我订酒店时有什么房间偏好？",
            expected_abstain=True,
        ),
        ConversationCase(
            "notification-scope",
            "scope-isolation",
            "应该通过什么方式通知我？",
            ("电子邮件",),
            ("notification_main",),
        ),
        ConversationCase(
            "assistant-side-room",
            "single-session-assistant",
            "我们确认的评审会议室叫什么？",
            ("Cedar Room",),
            ("assistant_room",),
        ),
        ConversationCase(
            "unknown-passport",
            "abstention",
            "我的护照号码是多少？",
            expected_abstain=True,
        ),
        ConversationCase(
            "unknown-license-issuer",
            "abstention-holdout",
            "我的驾照签发机关是哪一个？",
            expected_abstain=True,
        ),
    )
    return ConversationFixture(store, tuple(sessions), labels, cases)


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


def _answer_correct(case: ConversationCase, answer: str) -> bool:
    normalized = answer.strip()
    if case.expected_abstain:
        return normalized == "NO_MEMORY"
    folded = normalized.casefold()
    return all(marker.casefold() in folded for marker in case.expected_markers)


def _reader_prompt(question: str, context: str) -> str:
    return f"""你在进行长期对话记忆盲测。只能根据提供的历史或记忆回答。
如果没有足够依据、信息已明确忘记或只存在冲突内容，只输出：NO_MEMORY
有足够依据时，用一句简短中文直接回答；不要提到测试或记忆系统。

上下文：
{context or "（无可用上下文）"}

问题：{question}
"""


def run_diagnostic(
    *,
    model_name: str | None = None,
    provider: str = "auto",
    base_url: str | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mca-memory-long-dialogue-") as raw:
        fixture = build_fixture(Path(raw) / "memory")
        retriever = EvidenceTemporalRetriever(fixture.store)
        retrieval_cases = []
        contexts: dict[str, dict[str, str]] = {
            "recent_window": {},
            "full_history": {},
            "evidence_temporal_memory": {},
        }
        full_history = "\n\n".join(fixture.sessions)
        recent_window = "\n\n".join(fixture.sessions[-8:])
        for case in fixture.cases:
            pack = retriever.retrieve(
                MemoryQuery(
                    case.question,
                    scopes=(MemoryScope("user", MAIN_SCOPE),),
                    as_of=FINAL_MOMENT,
                    required_authority="inform",
                    limit=3,
                )
            )
            selected_ids = {item.card_id for item in pack.items}
            expected_ids = {fixture.labels[label].id for label in case.expected_labels}
            retrieval_correct = (
                pack.decision.kind == "no_memory" and not pack.items
                if case.expected_abstain
                else expected_ids.issubset(selected_ids)
            )
            retrieval_cases.append(
                {
                    "case": case.name,
                    "ability": case.ability,
                    "correct": retrieval_correct,
                    "decision": pack.decision.kind,
                    "reason": pack.decision.reason,
                    "selected": len(pack.items),
                    "expected": len(expected_ids),
                    "context_chars": len(pack.render()),
                }
            )
            contexts["recent_window"][case.name] = recent_window
            contexts["full_history"][case.name] = full_history
            contexts["evidence_temporal_memory"][case.name] = pack.render()

        retrieval_correct = sum(int(case["correct"]) for case in retrieval_cases)
        model_results = []
        model_calls = 0
        if model_name is not None:
            model = create_model(
                model_name,
                provider=provider,  # type: ignore[arg-type]
                base_url=base_url,
                temperature=0.0,
                request_timeout=90.0,
                max_retries=2,
            )
            for system_name, system_contexts in contexts.items():
                details = []
                correct = 0
                for case in fixture.cases:
                    answer = _response_text(
                        model.invoke(
                            [
                                HumanMessage(
                                    content=_reader_prompt(
                                        case.question,
                                        system_contexts[case.name],
                                    )
                                )
                            ]
                        )
                    )
                    passed = _answer_correct(case, answer)
                    correct += int(passed)
                    model_calls += 1
                    details.append(
                        {
                            "case": case.name,
                            "ability": case.ability,
                            "correct": passed,
                            "answer": answer,
                            "context_chars": len(system_contexts[case.name]),
                        }
                    )
                model_results.append(
                    {
                        "system": system_name,
                        "correct": correct,
                        "accuracy": round(correct / len(fixture.cases), 4),
                        "cases": details,
                    }
                )

        verification = fixture.store.verify()
        return {
            "suite": SUITE_NAME,
            "scope": {
                "sessions": len(fixture.sessions),
                "cases": len(fixture.cases),
                "explicit_authenticated_ingestion": True,
                "automatic_conversation_extraction": False,
                "embedding": False,
                "real_model": model_name is not None,
                "longmemeval_style_abilities": True,
            },
            "model": model_name,
            "provider": provider if model_name else None,
            "model_calls": model_calls,
            "retrieval": {
                "correct": retrieval_correct,
                "accuracy": round(retrieval_correct / len(fixture.cases), 4),
                "cases": retrieval_cases,
            },
            "reader_results": model_results,
            "store_integrity": verification.ok,
            "elapsed_seconds": round(time.monotonic() - started, 4),
            "claims_boundary": (
                "Tests reading after explicit session ingestion; does not test or claim "
                "production free-conversation extraction."
            ),
        }


def _print_report(report: dict[str, Any]) -> None:
    scope = report["scope"]
    retrieval = report["retrieval"]
    print(
        f"suite: {report['suite']}; sessions={scope['sessions']}; "
        f"cases={scope['cases']}"
    )
    print(
        f"retrieval: {retrieval['correct']}/{scope['cases']} "
        f"({retrieval['accuracy']:.1%}); integrity={report['store_integrity']}"
    )
    for item in retrieval["cases"]:
        print(
            f"  {item['case']:<28} {item['correct']!s:<5} "
            f"{item['decision']}:{item['reason']} selected={item['selected']}"
        )
    for result in report["reader_results"]:
        print(
            f"reader {result['system']:<28} "
            f"{result['correct']}/{scope['cases']} ({result['accuracy']:.1%})"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model")
    parser.add_argument(
        "--provider", choices=("auto", "openai", "deepseek"), default="auto"
    )
    parser.add_argument("--base-url")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run_diagnostic(
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
        _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
