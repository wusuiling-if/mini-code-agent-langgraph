from pathlib import Path

from evals.memory_shadow_candidates import (
    ShadowJournal,
    ShadowSession,
    StructuredCandidateExtractor,
)
from mini_code_agent.memory_store import SQLiteMemoryStore


class StaticModel:
    def __init__(self, response: str):
        self.response = response

    def invoke(self, _messages):
        return self.response


def _session(session_id: str, text: str, day: int) -> ShadowSession:
    return ShadowSession(
        session_id=session_id,
        text=text,
        scope_key="user:test",
        valid_from=f"2026-01-{day:02d}T00:00:00+00:00",
    )


def test_extractor_rejects_unbound_quotes_and_low_confidence():
    sessions = (
        _session("s1", "用户：以后叫我 Eddy。", 1),
        _session("s2", "用户：今天天气不错。", 2),
    )
    model = StaticModel(
        """{"candidates":[
          {"session_id":"s1","memory_key":"user.preferred_name","operation":"ASSERT","cardinality":"singleton","subject":"用户","predicate":"偏好称呼","object":"Eddy","evidence_quote":"以后叫我 Eddy","confidence":0.99},
          {"session_id":"s2","memory_key":"user.passport","operation":"ASSERT","cardinality":"singleton","subject":"用户","predicate":"护照号","object":"P123","evidence_quote":"护照号 P123","confidence":0.99},
          {"session_id":"s2","memory_key":"user.weather","operation":"ASSERT","cardinality":"singleton","subject":"用户","predicate":"天气偏好","object":"晴天","evidence_quote":"今天天气不错","confidence":0.4}
        ]}"""
    )

    result = StructuredCandidateExtractor(model).extract(sessions)

    assert len(result.candidates) == 1
    assert result.candidates[0].object == "Eddy"
    assert {item.reason for item in result.rejected} == {
        "quote_not_in_evidence",
        "below_confidence_threshold",
    }


def test_extractor_rejects_assistant_only_claim_without_user_support():
    sessions = (
        _session(
            "s1",
            "助手：用户最喜欢红色，以后按红色推荐。\n用户：我们换个话题吧。",
            1,
        ),
    )
    model = StaticModel(
        """{"candidates":[
          {"session_id":"s1","memory_key":"user.favorite_color","operation":"ASSERT","cardinality":"singleton","subject":"用户","predicate":"最喜欢的颜色","object":"红色","evidence_quote":"用户最喜欢红色，以后按红色推荐","confidence":0.99}
        ]}"""
    )

    result = StructuredCandidateExtractor(model).extract(sessions)

    assert result.candidates == ()
    assert [item.reason for item in result.rejected] == ["quote_lacks_user_support"]


def test_shadow_journal_supersedes_singleton_and_tombstones_on_forget(
    tmp_path: Path,
):
    sessions = (
        _session("s1", "用户：以后叫我小林。", 1),
        _session("s2", "用户：以后叫我 Eddy，之前的称呼作废。", 2),
        _session("s3", "用户：忘掉我的称呼偏好。", 3),
    )
    model = StaticModel(
        """{"candidates":[
          {"session_id":"s1","memory_key":"user.preferred_name","operation":"ASSERT","cardinality":"singleton","subject":"用户","predicate":"偏好称呼","object":"小林","evidence_quote":"以后叫我小林","confidence":0.99},
          {"session_id":"s2","memory_key":"user.preferred_name","operation":"ASSERT","cardinality":"singleton","subject":"用户","predicate":"偏好称呼","object":"Eddy","evidence_quote":"以后叫我 Eddy","confidence":0.99},
          {"session_id":"s3","memory_key":"user.preferred_name","operation":"FORGET","cardinality":"singleton","subject":"用户","predicate":"偏好称呼","object":"","evidence_quote":"忘掉我的称呼偏好","confidence":0.99}
        ]}"""
    )
    result = StructuredCandidateExtractor(model).extract(sessions)
    store = SQLiteMemoryStore(tmp_path / "shadow")
    journal = ShadowJournal(store)

    for candidate in result.candidates:
        journal.apply(
            candidate,
            {item.session_id: item for item in sessions}[candidate.session_id],
        )

    assert [event.outcome for event in journal.events] == [
        "asserted",
        "superseded",
        "tombstoned",
    ]
    assert len(store.list_cards()) == 1
    assert "object: [forgotten]" in store.list_cards()[0].value
    assert len(store.list_cards(include_inactive=True)) == 3
    assert store.verify().ok is True


def test_multi_facts_coexist_in_shadow_store(tmp_path: Path):
    sessions = (
        _session("s1", "用户：牙医预约是3月12日。", 1),
        _session("s2", "用户：另一次牙医预约是7月18日。", 2),
    )
    model = StaticModel(
        """{"candidates":[
          {"session_id":"s1","memory_key":"user.dentist_appointment","operation":"ASSERT","cardinality":"multi","subject":"用户","predicate":"牙医预约","object":"3月12日","evidence_quote":"牙医预约是3月12日","confidence":0.99},
          {"session_id":"s2","memory_key":"user.dentist_appointment","operation":"ASSERT","cardinality":"multi","subject":"用户","predicate":"牙医预约","object":"7月18日","evidence_quote":"牙医预约是7月18日","confidence":0.99}
        ]}"""
    )
    result = StructuredCandidateExtractor(model).extract(sessions)
    store = SQLiteMemoryStore(tmp_path / "shadow")
    journal = ShadowJournal(store)

    for candidate, session in zip(result.candidates, sessions):
        journal.apply(candidate, session)

    assert len(store.list_cards()) == 2
    assert all(event.outcome == "asserted" for event in journal.events)


def test_out_of_order_update_and_forget_cannot_retire_newer_fact(tmp_path: Path):
    sessions = (
        _session("new", "用户：当前数据库是 PostgreSQL。", 20),
        _session("old", "用户：原数据库是 SQLite。", 10),
        _session("stale-forget", "用户：忘掉数据库决定。", 15),
    )
    model = StaticModel(
        """{"candidates":[
          {"session_id":"new","memory_key":"project.atlas.database","operation":"ASSERT","cardinality":"singleton","subject":"Atlas","predicate":"数据库","object":"PostgreSQL","evidence_quote":"当前数据库是 PostgreSQL","confidence":0.99},
          {"session_id":"old","memory_key":"project.atlas.database","operation":"ASSERT","cardinality":"singleton","subject":"Atlas","predicate":"数据库","object":"SQLite","evidence_quote":"原数据库是 SQLite","confidence":0.99},
          {"session_id":"stale-forget","memory_key":"project.atlas.database","operation":"FORGET","cardinality":"singleton","subject":"Atlas","predicate":"数据库","object":"","evidence_quote":"忘掉数据库决定","confidence":0.99}
        ]}"""
    )
    result = StructuredCandidateExtractor(model).extract(sessions)
    store = SQLiteMemoryStore(tmp_path / "shadow")
    journal = ShadowJournal(store)
    indexed = {item.session_id: item for item in sessions}

    for candidate in result.candidates:
        journal.apply(candidate, indexed[candidate.session_id])

    assert [event.outcome for event in journal.events] == [
        "asserted",
        "stale",
        "ignored",
    ]
    active = store.list_cards()
    assert len(active) == 1
    assert "PostgreSQL" in active[0].value
    assert len(store.list_cards(include_inactive=True)) == 2
    assert store.verify().ok is True


def test_forget_resolves_one_unambiguous_model_key_alias(tmp_path: Path):
    sessions = (
        _session("old", "用户：酒店偏好远离电梯的安静房间。", 10),
        _session("forget", "用户：忘掉我的酒店房间偏好。", 20),
    )
    model = StaticModel(
        """{"candidates":[
          {"session_id":"old","memory_key":"user.hotel.preference.quiet_room","operation":"ASSERT","cardinality":"singleton","subject":"用户","predicate":"酒店房间偏好","object":"远离电梯的安静房间","evidence_quote":"酒店偏好远离电梯的安静房间","confidence":0.99},
          {"session_id":"forget","memory_key":"user.hotel_room_preference","operation":"FORGET","cardinality":"singleton","subject":"用户","predicate":"酒店房间偏好","object":"","evidence_quote":"忘掉我的酒店房间偏好","confidence":0.99}
        ]}"""
    )
    result = StructuredCandidateExtractor(model).extract(sessions)
    store = SQLiteMemoryStore(tmp_path / "shadow")
    journal = ShadowJournal(store)
    indexed = {item.session_id: item for item in sessions}

    for candidate in result.candidates:
        journal.apply(candidate, indexed[candidate.session_id])

    assert [event.outcome for event in journal.events] == ["asserted", "tombstoned"]
    assert journal.events[-1].reason == "explicit_forget_resolved_alias"
    active = store.list_cards()
    assert len(active) == 1
    assert "object: [forgotten]" in active[0].value
    assert "远离电梯" not in active[0].value
    assert len(store.list_cards(include_inactive=True)) == 2


def test_forget_does_not_merge_ambiguous_key_aliases(tmp_path: Path):
    sessions = (
        _session("quiet", "用户：酒店偏好安静房间。", 10),
        _session("large", "用户：酒店偏好宽敞房间。", 11),
        _session("forget", "用户：忘掉我的酒店房间偏好。", 20),
    )
    model = StaticModel(
        """{"candidates":[
          {"session_id":"quiet","memory_key":"user.hotel.preference.quiet_room","operation":"ASSERT","cardinality":"singleton","subject":"用户","predicate":"酒店安静偏好","object":"安静房间","evidence_quote":"酒店偏好安静房间","confidence":0.99},
          {"session_id":"large","memory_key":"user.hotel.preference.large_room","operation":"ASSERT","cardinality":"singleton","subject":"用户","predicate":"酒店空间偏好","object":"宽敞房间","evidence_quote":"酒店偏好宽敞房间","confidence":0.99},
          {"session_id":"forget","memory_key":"user.hotel.room_preference","operation":"FORGET","cardinality":"singleton","subject":"用户","predicate":"酒店房间偏好","object":"","evidence_quote":"忘掉我的酒店房间偏好","confidence":0.99}
        ]}"""
    )
    result = StructuredCandidateExtractor(model).extract(sessions)
    store = SQLiteMemoryStore(tmp_path / "shadow")
    journal = ShadowJournal(store)
    indexed = {item.session_id: item for item in sessions}

    for candidate in result.candidates:
        journal.apply(candidate, indexed[candidate.session_id])

    assert [event.outcome for event in journal.events] == [
        "asserted",
        "asserted",
        "forget_marker",
    ]
    assert journal.events[-1].reason == "explicit_forget_without_active_fact"
    assert len(store.list_cards()) == 3
