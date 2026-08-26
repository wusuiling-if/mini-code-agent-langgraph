"""运行四路、跨场景、完全离线的记忆架构对照评测。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from mini_code_agent.memory_models import EvidenceSource, MemoryCard
from mini_code_agent.memory_retrieval import (
    SCENARIO_POLICIES,
    EvidenceTemporalRetriever,
    MemoryQuery,
    MemoryScope,
    lexical_tokens,
)
from mini_code_agent.memory_store import SQLiteMemoryStore

SUITE_NAME = "memory-architecture-comparison-v1"
AS_OF = "2026-08-17T00:00:00Z"


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    domain: str
    query: str
    scopes: tuple[MemoryScope, ...]
    expected_ids: tuple[str, ...] = ()
    forbidden_ids: tuple[str, ...] = ()
    expected_abstain: bool = False
    required_authority: str = "none"
    tags: tuple[str, ...] = ()
    expected_markers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Prediction:
    selected_ids: tuple[str, ...]
    abstained: bool
    reason: str


class MemorySystem(Protocol):
    name: str

    def predict(self, case: BenchmarkCase) -> Prediction: ...


def _source(label: str, *, origin: str = "trusted_tool") -> EvidenceSource:
    return EvidenceSource(
        source_type="comparison_fixture",
        source_ref=f"benchmark:{label}",
        source_sha256=hashlib.sha256(f"benchmark:{label}".encode()).hexdigest(),
        origin=origin,
    )


def _add(
    store: SQLiteMemoryStore,
    label: str,
    value: str,
    cues: tuple[str, ...],
    *,
    kind: str = "semantic",
    scope: str,
    scope_key: str,
    origin: str = "agent",
    authority: str = "inform",
    confidence: float = 0.9,
    importance: float = 0.7,
    valid_from: str = "2026-01-01T00:00:00Z",
    valid_to: str | None = None,
) -> MemoryCard:
    return store.add_card(
        value=value,
        abstraction=value,
        cue_anchors=cues,
        kind=kind,
        subtype="comparison_fixture",
        scope=scope,
        scope_key=scope_key,
        origin=origin,
        authority=authority,
        confidence=confidence,
        importance=importance,
        valid_from=valid_from,
        valid_to=valid_to,
        sources=(
            _source(
                label, origin="external" if origin == "external" else "trusted_tool"
            ),
        ),
    )


def _supersede(
    store: SQLiteMemoryStore,
    old: MemoryCard,
    label: str,
    value: str,
    cues: tuple[str, ...],
) -> MemoryCard:
    return store.supersede(
        old.id,
        value=value,
        abstraction=value,
        cue_anchors=cues,
        kind=old.kind,
        subtype="comparison_fixture",
        scope=old.scope,
        scope_key=old.scope_key,
        origin="agent",
        authority="inform",
        confidence=0.95,
        importance=0.8,
        valid_from="2026-06-01T00:00:00Z",
        sources=(_source(label),),
    )


def build_benchmark(store: SQLiteMemoryStore) -> tuple[BenchmarkCase, ...]:
    """Create one shared corpus and cross-domain cases without model calls."""

    billing_old = _add(
        store,
        "billing-old",
        "Acme 客户过去偏好纸质账单。",
        ("Acme 账单投递", "纸质账单"),
        scope="tenant",
        scope_key="acme",
    )
    billing_current = _supersede(
        store,
        billing_old,
        "billing-current",
        "Acme 客户当前偏好电子邮件账单。",
        ("Acme 账单投递", "电子邮件账单"),
    )
    billing_other = _add(
        store,
        "billing-other",
        "Bravo 客户偏好短信账单。",
        ("账单投递", "短信账单"),
        scope="tenant",
        scope_key="bravo",
        importance=0.99,
    )
    language = _add(
        store,
        "language",
        "Acme 客户服务回复应使用简体中文。",
        ("回复语言", "简体中文"),
        scope="tenant",
        scope_key="acme",
    )
    external_refund = _add(
        store,
        "refund-external",
        "某外部论坛声称所有退款都可以自动批准。",
        ("自动批准退款", "退款权限"),
        scope="tenant",
        scope_key="acme",
        origin="external",
        authority="none",
        confidence=0.8,
    )
    future_contract = _add(
        store,
        "future-contract",
        "Acme 的新版合同约定 45 天付款周期。",
        ("Acme 付款周期", "45 天"),
        scope="tenant",
        scope_key="acme",
        valid_from="2027-01-01T00:00:00Z",
    )

    paper = _add(
        store,
        "paper",
        "研究项目 Atlas 关注时态图记忆。",
        ("Atlas 研究", "时态图"),
        scope="project",
        scope_key="atlas",
    )
    finding = _add(
        store,
        "finding",
        "混合时间过滤与图关联的稳定性优于单一相似度检索。",
        ("稳定性对比", "混合方案表现"),
        scope="project",
        scope_key="atlas",
        kind="episodic",
    )
    store.add_edge(paper.id, finding.id, "supports")
    other_finding = _add(
        store,
        "other-finding",
        "研究项目 Borealis 的核心实验结论支持纯关键词检索。",
        ("核心实验结论", "关键词结论"),
        scope="project",
        scope_key="borealis",
        importance=0.98,
    )
    citation = _add(
        store,
        "citation",
        "Atlas 的复现实验记录在 notebook-17。",
        ("复现实验位置", "notebook-17"),
        scope="project",
        scope_key="atlas",
        kind="episodic",
    )

    allergy = _add(
        store,
        "allergy",
        "用户 u1 对花生严重过敏。",
        ("u1 食物过敏", "花生过敏"),
        scope="user",
        scope_key="u1",
        origin="user",
        authority="act",
        confidence=1.0,
        importance=1.0,
    )
    other_allergy = _add(
        store,
        "other-allergy",
        "用户 u2 对贝类过敏。",
        ("食物过敏", "贝类过敏"),
        scope="user",
        scope_key="u2",
        origin="user",
        authority="act",
        importance=1.0,
    )
    meeting_old = _add(
        store,
        "meeting-old",
        "用户 u1 过去不希望周四下午安排会议。",
        ("u1 会议时间偏好", "周四下午"),
        scope="user",
        scope_key="u1",
        origin="user",
        authority="act",
    )
    meeting_current = store.supersede(
        meeting_old.id,
        value="用户 u1 当前不希望周五下午安排会议。",
        abstraction="用户 u1 当前不希望周五下午安排会议。",
        cue_anchors=("u1 会议时间偏好", "周五下午"),
        kind="semantic",
        subtype="comparison_fixture",
        scope="user",
        scope_key="u1",
        origin="user",
        authority="act",
        confidence=1.0,
        importance=0.95,
        valid_from="2026-07-01T00:00:00Z",
        sources=(_source("meeting-current", origin="user"),),
    )

    tests_a = _add(
        store,
        "tests-a",
        "代码仓库 repo-a 的提交前验证命令是 pytest -q。",
        ("repo-a 验证命令", "pytest -q"),
        scope="workspace",
        scope_key="repo-a",
        kind="procedural",
    )
    tests_b = _add(
        store,
        "tests-b",
        "代码仓库 repo-b 的提交前验证命令是 npm test。",
        ("验证命令", "npm test"),
        scope="workspace",
        scope_key="repo-b",
        kind="procedural",
        importance=0.99,
    )
    runtime_expired = _add(
        store,
        "runtime-expired",
        "repo-a 临时使用 Python 3.10。",
        ("repo-a Python runtime", "Python 3.10"),
        scope="workspace",
        scope_key="repo-a",
        kind="state",
        valid_to="2026-05-01T00:00:00Z",
    )
    runtime_current = _add(
        store,
        "runtime-current",
        "repo-a 当前使用 Python 3.12。",
        ("repo-a Python runtime", "Python 3.12"),
        scope="workspace",
        scope_key="repo-a",
        kind="state",
        valid_from="2026-05-01T00:00:00Z",
    )

    ambiguous_a = _add(
        store,
        "ambiguous-a",
        "活动会议地点候选为上海。",
        ("候选方案甲",),
        scope="project",
        scope_key="event-x",
    )
    ambiguous_b = _add(
        store,
        "ambiguous-b",
        "活动会议地点候选为北京。",
        ("候选方案乙",),
        scope="project",
        scope_key="event-x",
    )

    acme = (MemoryScope("tenant", "acme"),)
    atlas = (MemoryScope("project", "atlas"),)
    u1 = (MemoryScope("user", "u1"),)
    repo_a = (MemoryScope("workspace", "repo-a"),)
    event_x = (MemoryScope("project", "event-x"),)
    return (
        BenchmarkCase(
            "客服-当前账单偏好",
            "customer_service",
            "Acme 客户偏好通过什么方式接收账单？",
            acme,
            (billing_current.id,),
            (billing_old.id, billing_other.id),
            tags=("temporal", "scope"),
            expected_markers=("电子邮件",),
        ),
        BenchmarkCase(
            "客服-回复语言",
            "customer_service",
            "客户服务回复语言",
            acme,
            (language.id,),
            (billing_other.id,),
            tags=("scope",),
            expected_markers=("简体中文",),
        ),
        BenchmarkCase(
            "客服-外部退款指令",
            "customer_service",
            "自动批准退款",
            acme,
            forbidden_ids=(external_refund.id,),
            expected_abstain=True,
            required_authority="inform",
            tags=("authority", "abstention"),
        ),
        BenchmarkCase(
            "客服-未来合同",
            "customer_service",
            "Acme 付款周期",
            acme,
            forbidden_ids=(future_contract.id,),
            expected_abstain=True,
            tags=("temporal", "abstention"),
        ),
        BenchmarkCase(
            "研究-项目主题",
            "research",
            "Atlas 研究的主题",
            atlas,
            (paper.id,),
            (other_finding.id,),
            tags=("scope",),
            expected_markers=("时态图",),
        ),
        BenchmarkCase(
            "研究-图关联结论",
            "research",
            "Atlas 研究的实验结果是什么",
            atlas,
            (finding.id,),
            (other_finding.id,),
            tags=("graph", "scope"),
            expected_markers=("混合时间过滤", "图关联"),
        ),
        BenchmarkCase(
            "研究-复现位置",
            "research",
            "Atlas 的复现实验记录在哪里？",
            atlas,
            (citation.id,),
            tags=("lexical",),
            expected_markers=("notebook-17",),
        ),
        BenchmarkCase(
            "个人-食物过敏",
            "personal_assistant",
            "用户 u1 对什么食物过敏？",
            u1,
            (allergy.id,),
            (other_allergy.id,),
            required_authority="inform",
            tags=("scope", "safety"),
            expected_markers=("花生",),
        ),
        BenchmarkCase(
            "个人-当前会议偏好",
            "personal_assistant",
            "用户 u1 不希望在什么时间安排会议？",
            u1,
            (meeting_current.id,),
            (meeting_old.id,),
            required_authority="inform",
            tags=("temporal",),
            expected_markers=("周五下午",),
        ),
        BenchmarkCase(
            "编码-验证命令",
            "coding",
            "repo-a 验证命令",
            repo_a,
            (tests_a.id,),
            (tests_b.id,),
            tags=("scope",),
            expected_markers=("pytest -q",),
        ),
        BenchmarkCase(
            "编码-当前运行时",
            "coding",
            "repo-a 当前使用哪个 Python 版本？",
            repo_a,
            (runtime_current.id,),
            (runtime_expired.id,),
            tags=("temporal",),
            expected_markers=("3.12",),
        ),
        BenchmarkCase(
            "冲突-地点未决",
            "generic",
            "活动会议地点",
            event_x,
            forbidden_ids=(ambiguous_a.id, ambiguous_b.id),
            expected_abstain=True,
            tags=("ambiguity", "abstention"),
        ),
        BenchmarkCase(
            "无关-天气",
            "generic",
            "明天上海会下雨吗",
            acme,
            expected_abstain=True,
            tags=("irrelevant", "abstention"),
        ),
        BenchmarkCase(
            "无关-足球",
            "generic",
            "昨晚足球比赛比分",
            atlas,
            expected_abstain=True,
            tags=("irrelevant", "abstention"),
        ),
        BenchmarkCase(
            "无关-税率",
            "generic",
            "今年个人所得税率",
            u1,
            expected_abstain=True,
            tags=("irrelevant", "abstention"),
        ),
        BenchmarkCase(
            "无作用域-租户事实",
            "generic",
            "账单投递方式",
            (),
            forbidden_ids=(billing_current.id, billing_other.id),
            expected_abstain=True,
            tags=("scope", "abstention"),
        ),
    )


def _scope_match(card: MemoryCard, case: BenchmarkCase) -> bool:
    return card.scope == "global" or any(
        scope.name == card.scope and scope.key == card.scope_key
        for scope in case.scopes
    )


def _cosine(query: str, card: MemoryCard) -> float:
    left = Counter(lexical_tokens(query))
    right = Counter(lexical_tokens(" ".join((card.abstraction, *card.cue_anchors))))
    numerator = sum(left[token] * right[token] for token in left)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


class NoMemorySystem:
    name = "no_memory"

    def predict(self, case: BenchmarkCase) -> Prediction:
        return Prediction((), True, "memory_disabled")


class PureRecallSystem:
    """Top-k similarity only: no scope, lifecycle, evidence, or abstention gate."""

    name = "pure_recall"

    def __init__(self, store: SQLiteMemoryStore, limit: int = 3):
        self.cards = store.list_cards(include_inactive=True)
        self.limit = limit

    def predict(self, case: BenchmarkCase) -> Prediction:
        ranked = sorted(
            self.cards,
            key=lambda card: (-_cosine(case.query, card), -card.importance, card.id),
        )
        return Prediction(
            tuple(card.id for card in ranked[: self.limit]), False, "top_k"
        )


class TraditionalThreeLayerSystem:
    """Working + episodic + semantic layers with lexical retrieval.

    This fairer baseline honors explicit scope and active status, but has no
    validity window, evidence authority, graph expansion, or ambiguity gate.
    """

    name = "traditional_three_layer"

    def __init__(self, store: SQLiteMemoryStore, limit: int = 3):
        self.cards = store.list_cards(include_inactive=False)
        self.limit = limit

    def predict(self, case: BenchmarkCase) -> Prediction:
        scoped = [card for card in self.cards if _scope_match(card, case)]
        if not scoped:
            return Prediction((), True, "empty_scope")
        recent_ids = {
            card.id
            for card in sorted(scoped, key=lambda item: -item.recorded_at_ns)[:3]
        }
        ranked = []
        for card in scoped:
            similarity = _cosine(case.query, card)
            layer_boost = 0.03 if card.kind == "episodic" else 0.02
            recency_boost = 0.01 if card.id in recent_ids else 0.0
            ranked.append((similarity + layer_boost + recency_boost, card))
        ranked.sort(key=lambda item: (-item[0], -item[1].importance, item[1].id))
        positive = [card for score, card in ranked if score > 0.06]
        if not positive:
            return Prediction((), True, "no_lexical_match")
        return Prediction(
            tuple(card.id for card in positive[: self.limit]), False, "three_layer"
        )


class EvidenceTemporalSystem:
    name = "evidence_temporal_hybrid"

    def __init__(self, store: SQLiteMemoryStore):
        self.store = store

    def predict(self, case: BenchmarkCase) -> Prediction:
        retriever = EvidenceTemporalRetriever(
            self.store, policy=SCENARIO_POLICIES[case.domain]
        )
        pack = retriever.retrieve(
            MemoryQuery(
                case.query,
                scopes=case.scopes,
                as_of=AS_OF,
                required_authority=case.required_authority,  # type: ignore[arg-type]
                limit=3,
            )
        )
        return Prediction(
            tuple(item.card_id for item in pack.items),
            pack.decision.kind == "no_memory",
            pack.decision.reason,
        )


def _score_system(
    system: MemorySystem, cases: tuple[BenchmarkCase, ...]
) -> dict[str, Any]:
    details = []
    correct = 0
    top1 = 0
    answerable = 0
    answered_targets = 0
    harmful_cases = 0
    selected_total = 0
    relevant_selected = 0
    abstain_expected = 0
    abstain_correct = 0
    for case in cases:
        prediction = system.predict(case)
        selected = set(prediction.selected_ids)
        forbidden = bool(selected & set(case.forbidden_ids))
        if case.expected_abstain:
            abstain_expected += 1
            case_correct = prediction.abstained
            abstain_correct += int(case_correct)
            top1_correct = prediction.abstained
        else:
            answerable += 1
            hit = bool(selected & set(case.expected_ids))
            answered_targets += int(hit)
            case_correct = hit and not forbidden
            top1_correct = bool(
                prediction.selected_ids
                and prediction.selected_ids[0] in case.expected_ids
            )
        correct += int(case_correct)
        top1 += int(top1_correct)
        harmful_cases += int(forbidden)
        selected_total += len(prediction.selected_ids)
        relevant_selected += len(selected & set(case.expected_ids))
        details.append(
            {
                "case": case.name,
                "domain": case.domain,
                "tags": list(case.tags),
                "correct": case_correct,
                "harmful": forbidden,
                "abstained": prediction.abstained,
                "reason": prediction.reason,
                "selected_ids": list(prediction.selected_ids),
            }
        )
    total = len(cases)
    return {
        "system": system.name,
        "metrics": {
            "decision_accuracy": round(correct / total, 4),
            "top1_or_correct_abstain": round(top1 / total, 4),
            "answerable_recall_at_3": round(answered_targets / answerable, 4),
            "expected_abstention_recall": round(abstain_correct / abstain_expected, 4),
            "harmful_injection_rate": round(harmful_cases / total, 4),
            "context_precision": round(relevant_selected / selected_total, 4)
            if selected_total
            else 0.0,
            "average_memories_injected": round(selected_total / total, 4),
        },
        "counts": {
            "cases": total,
            "correct": correct,
            "harmful_cases": harmful_cases,
            "answerable": answerable,
            "expected_abstain": abstain_expected,
        },
        "cases": details,
    }


def run_comparison() -> dict[str, Any]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mca-memory-comparison-") as temporary:
        store = SQLiteMemoryStore(Path(temporary) / "memory")
        cases = build_benchmark(store)
        systems: tuple[MemorySystem, ...] = (
            NoMemorySystem(),
            PureRecallSystem(store),
            TraditionalThreeLayerSystem(store),
            EvidenceTemporalSystem(store),
        )
        results = [_score_system(system, cases) for system in systems]
        by_name = {result["system"]: result for result in results}
        proposed = by_name["evidence_temporal_hybrid"]["metrics"]
        baselines = [
            by_name[name]["metrics"]
            for name in ("no_memory", "pure_recall", "traditional_three_layer")
        ]
        best_baseline_accuracy = max(item["decision_accuracy"] for item in baselines)
        improvement = proposed["decision_accuracy"] - best_baseline_accuracy
        acceptance: dict[str, Any] = {
            "proposed_beats_best_baseline": improvement >= 0.15,
            "decision_accuracy_gain_vs_best_baseline": round(improvement, 4),
            "proposed_harmful_injection_at_most_5_percent": proposed[
                "harmful_injection_rate"
            ]
            <= 0.05,
            "proposed_abstention_recall_at_least_80_percent": proposed[
                "expected_abstention_recall"
            ]
            >= 0.8,
        }
        acceptance["passed"] = all(
            (
                acceptance["proposed_beats_best_baseline"],
                acceptance["proposed_harmful_injection_at_most_5_percent"],
                acceptance["proposed_abstention_recall_at_least_80_percent"],
            )
        )
        return {
            "suite": SUITE_NAME,
            "scope": {
                "offline": True,
                "deterministic": True,
                "domains": sorted({case.domain for case in cases}),
                "cases": len(cases),
                "shared_corpus": True,
                "model_calls": 0,
            },
            "systems": results,
            "acceptance": acceptance,
            "elapsed_seconds": round(time.monotonic() - started, 4),
        }


def _print_report(report: dict[str, Any]) -> None:
    print(f"suite: {report['suite']}")
    print(
        f"cases: {report['scope']['cases']}; domains: {', '.join(report['scope']['domains'])}"
    )
    print()
    print(
        "system                         accuracy  recall@3  abstain  harmful  precision"
    )
    for result in report["systems"]:
        metrics = result["metrics"]
        print(
            f"{result['system']:<30} "
            f"{metrics['decision_accuracy']:>8.1%} "
            f"{metrics['answerable_recall_at_3']:>9.1%} "
            f"{metrics['expected_abstention_recall']:>8.1%} "
            f"{metrics['harmful_injection_rate']:>8.1%} "
            f"{metrics['context_precision']:>9.1%}"
        )
    print()
    acceptance = report["acceptance"]
    print(
        "gain_vs_best_baseline: "
        f"{acceptance['decision_accuracy_gain_vs_best_baseline']:+.1%}"
    )
    print(f"acceptance: {'PASS' if acceptance['passed'] else 'FAIL'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON。")
    parser.add_argument("--output", type=Path, help="同时把 JSON 报告写入文件。")
    args = parser.parse_args(argv)
    report = run_comparison()
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
