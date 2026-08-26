"""运行跨120轮写入的长期记忆纵向四路对照评测。"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from evals.run_memory_comparison import (
    BenchmarkCase,
    EvidenceTemporalSystem,
    MemorySystem,
    NoMemorySystem,
    PureRecallSystem,
    TraditionalThreeLayerSystem,
    _add,
)
from mini_code_agent.memory_models import MemoryCard
from mini_code_agent.memory_retrieval import MemoryScope
from mini_code_agent.memory_store import SQLiteMemoryStore

SUITE_NAME = "memory-longitudinal-v1"
SESSIONS = 120


@dataclass(frozen=True)
class Probe:
    case: BenchmarkCase
    session: int
    age: int
    category: str


@dataclass(frozen=True)
class ProbeResult:
    system: str
    probe: str
    session: int
    age: int
    age_bucket: str
    category: str
    correct: bool
    harmful: bool
    abstained: bool
    selected_count: int
    latency_ms: float


def _age_bucket(age: int) -> str:
    if age <= 2:
        return "short_0_2"
    if age <= 20:
        return "medium_3_20"
    if age <= 60:
        return "long_21_60"
    return "very_long_61_plus"


def _case(
    name: str,
    domain: str,
    query: str,
    scope: MemoryScope | None,
    *,
    expected: MemoryCard | None = None,
    forbidden: tuple[MemoryCard, ...] = (),
    abstain: bool = False,
    markers: tuple[str, ...] = (),
) -> BenchmarkCase:
    return BenchmarkCase(
        name,
        domain,
        query,
        (scope,) if scope else (),
        (expected.id,) if expected else (),
        tuple(card.id for card in forbidden),
        expected_abstain=abstain,
        expected_markers=markers,
    )


def _system_factories(
    store: SQLiteMemoryStore,
) -> tuple[tuple[str, Callable[[], MemorySystem]], ...]:
    return (
        ("no_memory", NoMemorySystem),
        ("pure_recall", lambda: PureRecallSystem(store)),
        (
            "traditional_three_layer",
            lambda: TraditionalThreeLayerSystem(store),
        ),
        (
            "evidence_temporal_hybrid",
            lambda: EvidenceTemporalSystem(store),
        ),
    )


def _evaluate_probe(store: SQLiteMemoryStore, probe: Probe) -> list[ProbeResult]:
    results = []
    for name, factory in _system_factories(store):
        started = time.perf_counter()
        prediction = factory().predict(probe.case)
        latency_ms = (time.perf_counter() - started) * 1000
        selected = set(prediction.selected_ids)
        harmful = bool(selected & set(probe.case.forbidden_ids))
        if probe.case.expected_abstain:
            correct = prediction.abstained
        else:
            correct = bool(selected & set(probe.case.expected_ids)) and not harmful
        results.append(
            ProbeResult(
                system=name,
                probe=probe.case.name,
                session=probe.session,
                age=probe.age,
                age_bucket=_age_bucket(probe.age),
                category=probe.category,
                correct=correct,
                harmful=harmful,
                abstained=prediction.abstained,
                selected_count=len(prediction.selected_ids),
                latency_ms=round(latency_ms, 4),
            )
        )
    return results


def _stable_probes(
    session: int,
    cards: dict[str, MemoryCard],
    scopes: dict[str, MemoryScope],
) -> tuple[Probe, ...]:
    return (
        Probe(
            _case(
                f"retention:{session}:allergy",
                "personal_assistant",
                "用户 u1 对什么食物过敏？",
                scopes["user"],
                expected=cards["allergy"],
                markers=("花生",),
            ),
            session,
            session,
            "retention",
        ),
        Probe(
            _case(
                f"retention:{session}:workflow",
                "coding",
                "repo-a 提交前应该运行什么验证？",
                scopes["workspace"],
                expected=cards["workflow"],
                markers=("pytest -q",),
            ),
            session,
            session,
            "retention",
        ),
        Probe(
            _case(
                f"retention:{session}:citation",
                "research",
                "Atlas 的复现实验记录在哪里？",
                scopes["project"],
                expected=cards["citation"],
                markers=("notebook-17",),
            ),
            session,
            session,
            "retention",
        ),
        Probe(
            _case(
                f"retention:{session}:language",
                "customer_service",
                "Acme 客服应该使用什么语言回复？",
                scopes["tenant"],
                expected=cards["language"],
                markers=("简体中文",),
            ),
            session,
            session,
            "retention",
        ),
    )


def _seed(
    store: SQLiteMemoryStore,
) -> tuple[dict[str, MemoryCard], dict[str, MemoryScope]]:
    scopes = {
        "user": MemoryScope("user", "u1"),
        "workspace": MemoryScope("workspace", "repo-a"),
        "project": MemoryScope("project", "atlas"),
        "tenant": MemoryScope("tenant", "acme"),
    }
    cards = {
        "allergy": _add(
            store,
            "long-allergy",
            "用户 u1 对花生严重过敏。",
            ("u1 食物过敏", "花生"),
            scope="user",
            scope_key="u1",
            origin="user",
            authority="act",
            confidence=1.0,
            importance=1.0,
        ),
        "workflow": _add(
            store,
            "long-workflow",
            "repo-a 提交前必须运行 pytest -q。",
            ("repo-a 提交验证", "pytest -q"),
            kind="procedural",
            scope="workspace",
            scope_key="repo-a",
            importance=0.95,
        ),
        "citation": _add(
            store,
            "long-citation",
            "Atlas 的复现实验记录在 notebook-17。",
            ("Atlas 复现实验", "notebook-17"),
            kind="episodic",
            scope="project",
            scope_key="atlas",
            importance=0.9,
        ),
        "language": _add(
            store,
            "long-language",
            "Acme 客服回复应使用简体中文。",
            ("Acme 回复语言", "简体中文"),
            scope="tenant",
            scope_key="acme",
            importance=0.9,
        ),
    }
    cards["contact_old"] = _add(
        store,
        "long-contact-old",
        "用户 u1 的通知渠道是电子邮件。",
        ("u1 通知渠道", "电子邮件"),
        scope="user",
        scope_key="u1",
        origin="user",
        authority="act",
    )
    cards["meeting_old"] = _add(
        store,
        "long-meeting-old",
        "用户 u1 不希望周四下午安排会议。",
        ("u1 会议时间偏好", "周四下午"),
        scope="user",
        scope_key="u1",
        origin="user",
        authority="act",
    )
    cards["forgettable"] = _add(
        store,
        "long-forgettable",
        "用户 u1 曾偏好酒店安静房间。",
        ("u1 酒店房间偏好", "安静房间"),
        scope="user",
        scope_key="u1",
        origin="user",
        authority="act",
    )
    cards["expired"] = _add(
        store,
        "long-expired",
        "Acme 临时联系电话分机是 7712。",
        ("Acme 联系分机", "7712"),
        scope="tenant",
        scope_key="acme",
        valid_to="2026-06-01T00:00:00Z",
    )
    cards["graph_entity"] = _add(
        store,
        "long-graph-entity",
        "Atlas-L 项目研究长期记忆。",
        ("Atlas-L 项目", "长期记忆研究"),
        scope="project",
        scope_key="atlas",
    )
    cards["graph_result"] = _add(
        store,
        "long-graph-result",
        "分层时间索引在高干扰条件下保持稳定。",
        ("高干扰稳定性", "分层时间索引"),
        kind="episodic",
        scope="project",
        scope_key="atlas",
    )
    store.add_edge(cards["graph_entity"].id, cards["graph_result"].id, "supports")
    return cards, scopes


def _add_distractor(store: SQLiteMemoryStore, session: int) -> None:
    templates = (
        (
            f"噪声租户 noise-{session} 的客服回复语言是英语。",
            ("客服回复语言", "英语"),
            "tenant",
            f"noise-{session}",
            "semantic",
        ),
        (
            f"噪声用户 noise-{session} 对贝类过敏。",
            ("食物过敏", "贝类"),
            "user",
            f"noise-{session}",
            "semantic",
        ),
        (
            f"噪声仓库 noise-{session} 使用 npm test。",
            ("提交验证", "npm test"),
            "workspace",
            f"noise-{session}",
            "procedural",
        ),
        (
            f"噪声项目 noise-{session} 的复现实验记录在 archive-{session}。",
            ("复现实验记录", f"archive-{session}"),
            "project",
            f"noise-{session}",
            "episodic",
        ),
    )
    value, cues, scope, scope_key, kind = templates[session % len(templates)]
    _add(
        store,
        f"long-noise-{session}",
        value,
        cues,
        kind=kind,
        scope=scope,
        scope_key=scope_key,
        confidence=0.95,
        importance=0.99,
    )


def _summary(results: list[ProbeResult], store: SQLiteMemoryStore) -> dict[str, Any]:
    systems = []
    for name in (
        "no_memory",
        "pure_recall",
        "traditional_three_layer",
        "evidence_temporal_hybrid",
    ):
        items = [result for result in results if result.system == name]
        retention = [result for result in items if result.category == "retention"]
        by_age = {}
        for bucket in (
            "short_0_2",
            "medium_3_20",
            "long_21_60",
            "very_long_61_plus",
        ):
            bucket_items = [
                result for result in retention if result.age_bucket == bucket
            ]
            if bucket_items:
                by_age[bucket] = round(
                    sum(result.correct for result in bucket_items) / len(bucket_items),
                    4,
                )
        latencies = sorted(result.latency_ms for result in items)
        p95_index = max(0, math_ceil(0.95 * len(latencies)) - 1)
        systems.append(
            {
                "system": name,
                "metrics": {
                    "overall_accuracy": round(
                        sum(result.correct for result in items) / len(items), 4
                    ),
                    "retention_accuracy": round(
                        sum(result.correct for result in retention) / len(retention), 4
                    ),
                    "retention_by_age": by_age,
                    "harmful_injection_rate": round(
                        sum(result.harmful for result in items) / len(items), 4
                    ),
                    "latency_ms_p50": round(statistics.median(latencies), 4),
                    "latency_ms_p95": round(latencies[p95_index], 4),
                },
                "counts": {
                    "probes": len(items),
                    "correct": sum(result.correct for result in items),
                    "harmful": sum(result.harmful for result in items),
                },
                "failures": [result.probe for result in items if not result.correct],
            }
        )
    status = store.status()
    proposed = next(
        item for item in systems if item["system"] == "evidence_temporal_hybrid"
    )
    baselines = [
        item for item in systems if item["system"] != "evidence_temporal_hybrid"
    ]
    best_baseline = max(item["metrics"]["overall_accuracy"] for item in baselines)
    gain = proposed["metrics"]["overall_accuracy"] - best_baseline
    acceptance = {
        "retention_at_least_95_percent": proposed["metrics"]["retention_accuracy"]
        >= 0.95,
        "harmful_injection_at_most_5_percent": proposed["metrics"][
            "harmful_injection_rate"
        ]
        <= 0.05,
        "overall_not_worse_than_best_baseline": gain >= 0.0,
        "gain_vs_best_baseline": round(gain, 4),
    }
    acceptance["passed"] = all(
        (
            acceptance["retention_at_least_95_percent"],
            acceptance["harmful_injection_at_most_5_percent"],
            acceptance["overall_not_worse_than_best_baseline"],
        )
    )
    return {
        "suite": SUITE_NAME,
        "scope": {
            "sessions": SESSIONS,
            "probes": len(results) // 4,
            "offline": True,
            "automatic_memory_formation": False,
        },
        "store": {
            "cards": status.cards,
            "edges": status.edges,
            "database_bytes": store.database_path.stat().st_size,
        },
        "systems": systems,
        "acceptance": acceptance,
        "probes": [result.__dict__ for result in results],
    }


def math_ceil(value: float) -> int:
    integer = int(value)
    return integer if integer == value else integer + 1


def run_longitudinal() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="mca-memory-longitudinal-") as temporary:
        store = SQLiteMemoryStore(Path(temporary) / "memory")
        cards, scopes = _seed(store)
        results: list[ProbeResult] = []
        for session in range(1, SESSIONS + 1):
            _add_distractor(store, session)
            probes: list[Probe] = []
            if session in {1, 10, 50, 120}:
                probes.extend(_stable_probes(session, cards, scopes))
            if session == 29:
                probes.append(
                    Probe(
                        _case(
                            "update:before-contact",
                            "personal_assistant",
                            "用户 u1 当前使用什么通知渠道？",
                            scopes["user"],
                            expected=cards["contact_old"],
                            markers=("电子邮件",),
                        ),
                        session,
                        29,
                        "update",
                    )
                )
            if session == 30:
                cards["contact_current"] = store.supersede(
                    cards["contact_old"].id,
                    value="用户 u1 当前通知渠道是短信。",
                    abstraction="用户 u1 当前通知渠道是短信。",
                    cue_anchors=("u1 通知渠道", "短信"),
                    kind="semantic",
                    subtype="longitudinal",
                    scope="user",
                    scope_key="u1",
                    origin="user",
                    authority="act",
                    confidence=1.0,
                    importance=0.95,
                    valid_from="2026-07-01T00:00:00Z",
                    sources=(store.sources(cards["contact_old"].id)[0],),
                )
            if session in {31, 80, 120}:
                probes.append(
                    Probe(
                        _case(
                            f"update:{session}:contact",
                            "personal_assistant",
                            "用户 u1 当前使用什么通知渠道？",
                            scopes["user"],
                            expected=cards["contact_current"],
                            forbidden=(cards["contact_old"],),
                            markers=("短信",),
                        ),
                        session,
                        session - 30,
                        "update",
                    )
                )
            if session == 74:
                probes.append(
                    Probe(
                        _case(
                            "update:before-meeting",
                            "personal_assistant",
                            "用户 u1 不希望在什么时间安排会议？",
                            scopes["user"],
                            expected=cards["meeting_old"],
                            markers=("周四下午",),
                        ),
                        session,
                        74,
                        "update",
                    )
                )
            if session == 75:
                cards["meeting_current"] = store.supersede(
                    cards["meeting_old"].id,
                    value="用户 u1 当前不希望周五下午安排会议。",
                    abstraction="用户 u1 当前不希望周五下午安排会议。",
                    cue_anchors=("u1 会议时间偏好", "周五下午"),
                    kind="semantic",
                    subtype="longitudinal",
                    scope="user",
                    scope_key="u1",
                    origin="user",
                    authority="act",
                    confidence=1.0,
                    importance=0.95,
                    valid_from="2026-08-01T00:00:00Z",
                    sources=(store.sources(cards["meeting_old"].id)[0],),
                )
            if session in {76, 120}:
                probes.append(
                    Probe(
                        _case(
                            f"update:{session}:meeting",
                            "personal_assistant",
                            "用户 u1 不希望在什么时间安排会议？",
                            scopes["user"],
                            expected=cards["meeting_current"],
                            forbidden=(cards["meeting_old"],),
                            markers=("周五下午",),
                        ),
                        session,
                        session - 75,
                        "update",
                    )
                )
            if session == 90:
                store.transition(cards["forgettable"].id, "tombstoned")
            if session in {91, 120}:
                probes.append(
                    Probe(
                        _case(
                            f"forget:{session}:hotel",
                            "personal_assistant",
                            "用户 u1 有什么酒店房间偏好？",
                            scopes["user"],
                            forbidden=(cards["forgettable"],),
                            abstain=True,
                        ),
                        session,
                        session - 90,
                        "forgetting",
                    )
                )
            if session == 120:
                probes.extend(
                    (
                        Probe(
                            _case(
                                "graph:very-long-result",
                                "research",
                                "Atlas-L 项目的研究结果是什么？",
                                scopes["project"],
                                expected=cards["graph_result"],
                                markers=("分层时间索引",),
                            ),
                            session,
                            120,
                            "graph",
                        ),
                        Probe(
                            _case(
                                "expiry:very-long-extension",
                                "customer_service",
                                "Acme 当前联系分机是多少？",
                                scopes["tenant"],
                                forbidden=(cards["expired"],),
                                abstain=True,
                            ),
                            session,
                            120,
                            "expiry",
                        ),
                        Probe(
                            _case(
                                "scope:missing-user",
                                "generic",
                                "食物过敏是什么？",
                                None,
                                forbidden=(cards["allergy"],),
                                abstain=True,
                            ),
                            session,
                            120,
                            "scope",
                        ),
                        Probe(
                            _case(
                                "irrelevant:weather",
                                "generic",
                                "明天北京会下雪吗？",
                                scopes["tenant"],
                                abstain=True,
                            ),
                            session,
                            120,
                            "irrelevant",
                        ),
                    )
                )
            for probe in probes:
                results.extend(_evaluate_probe(store, probe))
        return _summary(results, store)


def _print_report(report: dict[str, Any]) -> None:
    print(
        f"suite: {report['suite']}; sessions: {report['scope']['sessions']}; "
        f"cards: {report['store']['cards']}; probes: {report['scope']['probes']}"
    )
    print()
    print("system                         overall  retention  harmful  p50_ms  p95_ms")
    for result in report["systems"]:
        metrics = result["metrics"]
        print(
            f"{result['system']:<30} "
            f"{metrics['overall_accuracy']:>7.1%} "
            f"{metrics['retention_accuracy']:>10.1%} "
            f"{metrics['harmful_injection_rate']:>8.1%} "
            f"{metrics['latency_ms_p50']:>7.2f} "
            f"{metrics['latency_ms_p95']:>7.2f}"
        )
    print()
    print(
        f"gain_vs_best_baseline: {report['acceptance']['gain_vs_best_baseline']:+.1%}"
    )
    print(f"acceptance: {'PASS' if report['acceptance']['passed'] else 'FAIL'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = run_longitudinal()
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    if args.json:
        print(payload)
    else:
        _print_report(report)
    return 0 if report["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
