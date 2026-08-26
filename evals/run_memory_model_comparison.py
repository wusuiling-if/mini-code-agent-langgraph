"""使用真实模型运行四路记忆上下文端到端对照；API Key 仅从环境读取。"""

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

from langchain_core.messages import HumanMessage

from evals.run_memory_comparison import (
    BenchmarkCase,
    EvidenceTemporalSystem,
    MemorySystem,
    NoMemorySystem,
    PureRecallSystem,
    TraditionalThreeLayerSystem,
    build_benchmark,
)
from mini_code_agent.memory_store import SQLiteMemoryStore
from mini_code_agent.model import create_model

SUITE_NAME = "memory-model-comparison-v1"


def _content_text(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                parts.append(str(item))
        return "\n".join(parts).strip()
    return str(content).strip()


def response_is_correct(case: BenchmarkCase, response: str) -> bool:
    normalized = response.strip()
    if case.expected_abstain:
        return normalized == "NO_MEMORY"
    return bool(case.expected_markers) and all(
        marker.casefold() in normalized.casefold() for marker in case.expected_markers
    )


def _prompt(case: BenchmarkCase, memories: tuple[str, ...]) -> str:
    context = "\n".join(f"- {memory}" for memory in memories)
    if not context:
        context = "（没有可用的长期记忆）"
    return f"""你正在参加记忆系统盲测。只能依据下面的记忆上下文回答问题。
如果上下文不足、互相冲突或没有答案，只输出：NO_MEMORY
如果足够，请用一句中文直接回答，不要补充常识，也不要提及测试。

记忆上下文：
{context}

问题：{case.query}
"""


def run_model_comparison(
    *,
    model_name: str,
    provider: str = "auto",
    base_url: str | None = None,
    max_cases: int | None = None,
) -> dict[str, Any]:
    """Run an intentionally optional, paid/non-deterministic answer evaluation."""

    started = time.monotonic()
    model = create_model(
        model_name,
        provider=provider,  # type: ignore[arg-type]
        base_url=base_url,
        temperature=0.0,
        request_timeout=60.0,
        max_retries=2,
    )
    with tempfile.TemporaryDirectory(prefix="mca-memory-model-eval-") as temporary:
        store = SQLiteMemoryStore(Path(temporary) / "memory")
        all_cases = build_benchmark(store)
        cases = all_cases[:max_cases] if max_cases is not None else all_cases
        cards = {card.id: card for card in store.list_cards(include_inactive=True)}
        systems: tuple[MemorySystem, ...] = (
            NoMemorySystem(),
            PureRecallSystem(store),
            TraditionalThreeLayerSystem(store),
            EvidenceTemporalSystem(store),
        )
        results = []
        for system in systems:
            details = []
            correct = 0
            for case in cases:
                prediction = system.predict(case)
                memories = tuple(
                    cards[card_id].value for card_id in prediction.selected_ids
                )
                response = _content_text(
                    model.invoke([HumanMessage(content=_prompt(case, memories))])
                )
                passed = response_is_correct(case, response)
                correct += int(passed)
                details.append(
                    {
                        "case": case.name,
                        "correct": passed,
                        "retrieval_reason": prediction.reason,
                        "memory_count": len(memories),
                        "response": response,
                    }
                )
            results.append(
                {
                    "system": system.name,
                    "answer_accuracy": round(correct / len(cases), 4),
                    "correct": correct,
                    "cases": details,
                }
            )
    by_name = {result["system"]: result for result in results}
    proposed = by_name["evidence_temporal_hybrid"]["answer_accuracy"]
    best_baseline = max(
        by_name[name]["answer_accuracy"]
        for name in ("no_memory", "pure_recall", "traditional_three_layer")
    )
    return {
        "suite": SUITE_NAME,
        "model": model_name,
        "provider": provider,
        "cases": len(cases),
        "model_calls": len(cases) * len(results),
        "results": results,
        "proposed_gain_vs_best_baseline": round(proposed - best_baseline, 4),
        "elapsed_seconds": round(time.monotonic() - started, 4),
        "notes": [
            "真实模型评测非确定性，离线检索评测仍是回归门。",
            "响应按预注册关键词或严格 NO_MEMORY 评分，不使用模型自评。",
        ],
    }


def _print_report(report: dict[str, Any]) -> None:
    print(f"suite: {report['suite']}")
    print(f"model: {report['model']} ({report['provider']})")
    print(f"cases: {report['cases']}; model_calls: {report['model_calls']}")
    for result in report["results"]:
        print(f"{result['system']:<30} {result['answer_accuracy']:>7.1%}")
    print(f"gain_vs_best_baseline: {report['proposed_gain_vs_best_baseline']:+.1%}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="模型名。")
    parser.add_argument(
        "--provider", choices=("auto", "openai", "deepseek"), default="auto"
    )
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.max_cases is not None and args.max_cases < 1:
        parser.error("--max-cases must be greater than zero")
    report = run_model_comparison(
        model_name=args.model,
        provider=args.provider,
        base_url=args.base_url,
        max_cases=args.max_cases,
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
