import json
from pathlib import Path

from evals.memory_shadow_candidates import (
    ShadowJournal,
    StructuredCandidateExtractor,
)
from evals.run_memory_long_conversation import MAIN_SCOPE, build_fixture
from evals.run_memory_shadow_extraction import (
    EXPECTED_ACTIVE,
    _query,
    _selected_sessions,
)
from mini_code_agent.memory_retrieval import EvidenceTemporalRetriever
from mini_code_agent.memory_store import SQLiteMemoryStore


class StaticExtractionModel:
    def __init__(self, candidates):
        self.payload = json.dumps({"candidates": candidates}, ensure_ascii=False)

    def invoke(self, _messages):
        return self.payload


def _candidate(
    session_id: str,
    memory_key: str,
    subject: str,
    predicate: str,
    object_value: str,
    quote: str,
    *,
    operation: str = "ASSERT",
    cardinality: str = "singleton",
):
    return {
        "session_id": session_id,
        "memory_key": memory_key,
        "operation": operation,
        "cardinality": cardinality,
        "subject": subject,
        "predicate": predicate,
        "object": object_value,
        "evidence_quote": quote,
        "confidence": 0.99,
    }


def test_full_shadow_mechanism_handles_updates_forgetting_scope_and_filler(
    tmp_path: Path,
):
    fixture = build_fixture(tmp_path / "primary")
    sessions = _selected_sessions(fixture)
    indexed = {session.session_id: session for session in sessions}
    rows = [
        _candidate(
            "main-009",
            "user.preferred_name",
            "用户",
            "偏好称呼",
            "小林",
            "以后请叫我小林",
        ),
        _candidate(
            "main-017",
            "user.food_allergy",
            "用户",
            "严重过敏原",
            "榛子",
            "我对榛子严重过敏",
            cardinality="multi",
        ),
        _candidate(
            "main-022",
            "project.atlas.database",
            "Atlas 项目",
            "当前数据库",
            "SQLite",
            "Atlas 项目的数据库决定使用 SQLite",
        ),
        _candidate(
            "main-028",
            "event.may_design_workshop.location",
            "五月设计工作坊",
            "地点",
            "苏州工业园区",
            "五月设计工作坊的地点已经确定，在苏州工业园区",
        ),
        _candidate(
            "main-035",
            "user.dentist_appointment",
            "用户",
            "牙医预约",
            "3月12日 上午",
            "我的较早一次牙医预约是 3月12日 上午",
            cardinality="multi",
        ),
        _candidate(
            "main-041",
            "user.hotel.room_preference",
            "用户",
            "酒店房间偏好",
            "远离电梯的安静房间",
            "订酒店时我偏好远离电梯的安静房间",
        ),
        _candidate(
            "main-048",
            "user.notification_channel",
            "用户",
            "通知方式",
            "电子邮件，不发短信",
            "通知方式请固定使用电子邮件，不要发短信",
        ),
        _candidate(
            "main-056",
            "meeting.review.room",
            "评审会议",
            "会议室",
            "Cedar Room",
            "助手：按照我们刚才的确认，我已经预订 Cedar Room 作为评审会议室。\n用户：可以，就用它。",
        ),
        _candidate(
            "main-063",
            "event.may_design_workshop.travel",
            "用户",
            "去五月设计工作坊的出行计划",
            "5月9日上午坐高铁",
            "去五月设计工作坊我打算坐高铁，5月9日上午出发",
        ),
        _candidate(
            "main-072",
            "user.dentist_appointment",
            "用户",
            "牙医预约",
            "7月18日 下午",
            "我后来又约了一次牙医，日期是 7月18日 下午",
            cardinality="multi",
        ),
        _candidate(
            "main-087",
            "user.preferred_name",
            "用户",
            "偏好称呼",
            "Eddy",
            "以后请叫我 Eddy，之前的小林称呼作废",
        ),
        _candidate(
            "main-091",
            "project.atlas.database",
            "Atlas 项目",
            "当前数据库",
            "PostgreSQL",
            "Atlas 的存储决定已经更新为 PostgreSQL，之前的 SQLite 决定作废",
        ),
        _candidate(
            "main-096",
            "user.hotel.room_preference",
            "用户",
            "酒店房间偏好",
            "",
            "请忘掉我的酒店房间偏好，不要再据此推荐",
            operation="FORGET",
        ),
        _candidate(
            "other-121",
            "user.notification_channel",
            "用户",
            "通知方式",
            "短信，不发电子邮件",
            "通知方式只使用短信，不要发电子邮件",
        ),
    ]
    extraction = StructuredCandidateExtractor(StaticExtractionModel(rows)).extract(
        sessions
    )
    store = SQLiteMemoryStore(tmp_path / "shadow")
    journal = ShadowJournal(store)

    for candidate in extraction.candidates:
        journal.apply(candidate, indexed[candidate.session_id])

    assert extraction.rejected == ()
    assert not any(
        session.is_filler
        for session in sessions
        if session.session_id
        in {candidate.session_id for candidate in extraction.candidates}
    )
    assert store.verify().ok is True
    assert {event.outcome for event in journal.events} >= {
        "asserted",
        "superseded",
        "tombstoned",
    }
    active_text = "\n".join(card.value for card in store.list_cards())
    assert "object: 小林" not in active_text
    assert "object: SQLite" not in active_text
    assert "object: 远离电梯的安静房间" not in active_text
    assert "object: Eddy" in active_text
    assert "object: PostgreSQL" in active_text
    assert any(
        card.scope_key != MAIN_SCOPE and "短信" in card.value
        for card in store.list_cards()
    )

    retriever = EvidenceTemporalRetriever(store)
    for case in fixture.cases:
        pack = retriever.retrieve(_query(case))
        if case.expected_abstain:
            if case.name == "forgotten-hotel-preference":
                assert "object: [forgotten]" in pack.render()
            else:
                assert pack.decision.kind == "no_memory", (case.name, pack.render())
            continue
        expected = EXPECTED_ACTIVE[case.name]
        context = pack.render()
        assert all(marker in context for marker in expected), case.name
