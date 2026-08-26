"""运行确定性、离线的记忆核心评测。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from mini_code_agent.memory_models import (
    EvidenceSource,
    MemoryIntegrityError,
)
from mini_code_agent.memory_store import SQLiteMemoryStore

SCHEMA_VERSION = 1
SUITE_NAME = "memory-core-v0.1"


@dataclass(frozen=True)
class CaseResult:
    name: str
    category: str
    passed: bool
    expected: str
    observed: str


def _evidence(label: str, *, origin: str = "trusted_tool") -> EvidenceSource:
    payload = f"memory-eval:{label}".encode()
    return EvidenceSource(
        source_type="eval_fixture",
        source_ref=f"eval:{label}",
        source_sha256=hashlib.sha256(payload).hexdigest(),
        origin=origin,
    )


def _add(
    store: SQLiteMemoryStore,
    label: str,
    abstraction: str,
    cues: tuple[str, ...],
    *,
    value: str | None = None,
    origin: str = "agent",
    authority: str = "inform",
    importance: float = 0.5,
):
    return store.add_card(
        value=value or abstraction,
        abstraction=abstraction,
        cue_anchors=cues,
        kind="procedural",
        subtype="eval_fixture",
        scope="workspace",
        scope_key="memory-eval",
        origin=origin,
        authority=authority,
        confidence=0.9,
        importance=importance,
        valid_from="2026-08-17T00:00:00Z",
        sources=(
            _evidence(
                label, origin="external" if origin == "external" else "trusted_tool"
            ),
        ),
    )


def _case(
    name: str,
    category: str,
    passed: bool,
    expected: str,
    observed: str,
) -> CaseResult:
    return CaseResult(name, category, passed, expected, observed)


def run_suite() -> dict[str, Any]:
    started = time.monotonic()
    cases: list[CaseResult] = []
    with tempfile.TemporaryDirectory(prefix="mca-memory-eval-") as temporary:
        root = Path(temporary)
        store = SQLiteMemoryStore(root / "main")
        targets = {
            "verification": _add(
                store,
                "verification",
                "项目提交前需要运行完整 pytest 测试矩阵。",
                ("pytest", "test matrix", "quality gate", "验证矩阵"),
                importance=0.95,
            ),
            "state_security": _add(
                store,
                "state-security",
                "私有状态目录拒绝符号链接，并使用严格文件权限。",
                ("symlink", "0700", "0600", "符号链接", "状态目录"),
                importance=0.9,
            ),
            "receipt": _add(
                store,
                "receipt",
                "事务通过 HMAC receipt 绑定 prepared patch 和验证证据。",
                ("transaction receipt", "HMAC", "prepared patch"),
                importance=0.85,
            ),
            "provider": _add(
                store,
                "provider",
                "DeepSeek 与 OpenAI provider 使用相互独立的配置。",
                ("DeepSeek provider", "OpenAI provider", "API base URL"),
                importance=0.8,
            ),
            "memory_index": _add(
                store,
                "memory-index",
                "记忆层使用 SQLite FTS 索引 abstraction 与 cue anchor。",
                ("SQLite FTS", "cue anchors", "BM25"),
                importance=0.88,
            ),
        }
        for index, topic in enumerate(
            (
                "聊天会话在完整工具边界保存 checkpoint",
                "Docker 沙箱默认关闭网络并使用只读根文件系统",
                "Undo journal 在恢复文件前检查内容 hash",
                "命令超时后尝试清理整个进程组",
                "工作区 fingerprint 会使旧验证结果失效",
                "事务提交前检查源工作区是否发生并发变化",
                "敏感值在写入 trajectory 前执行脱敏",
                "结构化工具将文件访问限制在工作区中",
            )
        ):
            _add(store, f"distractor-{index}", topic, (f"noise-{index}", topic[:6]))

        retrieval_queries = (
            ("pytest matrix", "verification"),
            ("quality gate", "verification"),
            ("符号链接 状态目录", "state_security"),
            ("transaction receipt", "receipt"),
            ("DeepSeek provider", "provider"),
            ("SQLite FTS", "memory_index"),
        )
        retrieval_hits = 0
        for query, target_name in retrieval_queries:
            results = store.search(query, limit=3)
            observed = results[0].id if results else "none"
            expected = targets[target_name].id
            hit = observed == expected
            retrieval_hits += int(hit)
            cases.append(
                _case(
                    f"retrieve:{query}",
                    "lexical_retrieval",
                    hit,
                    f"top1={target_name}",
                    f"top1={'expected' if hit else observed}",
                )
            )

        abstention_queries = ("Kubernetes production deployment", "客户发票税率政策")
        abstention_hits = 0
        for query in abstention_queries:
            empty = not store.search(query)
            abstention_hits += int(empty)
            cases.append(
                _case(
                    f"abstain:{query}",
                    "abstention",
                    empty,
                    "no result",
                    "no result" if empty else "unexpected result",
                )
            )

        semantic_query = "How can I be confident a change is safe before merging?"
        semantic_results = store.search(semantic_query)
        semantic_hit = bool(
            semantic_results and semantic_results[0].id == targets["verification"].id
        )
        cases.append(
            _case(
                "diagnostic:semantic-paraphrase-without-shared-terms",
                "known_limit",
                not semantic_hit,
                "miss until embeddings or a matching cue are available",
                "hit" if semantic_hit else "miss",
            )
        )

        old = _add(
            store,
            "python-old",
            "项目最低支持 Python 3.10 runtime。",
            ("Python runtime", "Python 3.10"),
        )
        new = store.supersede(
            old.id,
            value="新环境优先使用 Python 3.12，同时保持声明的兼容范围。",
            abstraction="项目当前优先使用 Python 3.12 runtime。",
            cue_anchors=("Python runtime", "Python 3.12"),
            kind="procedural",
            subtype="eval_fixture",
            scope="workspace",
            scope_key="memory-eval",
            origin="agent",
            authority="inform",
            confidence=0.95,
            importance=0.9,
            valid_from="2026-08-18T00:00:00Z",
            sources=(_evidence("python-new"),),
        )
        current = store.search("Python runtime", include_inactive=False)
        historical = store.search("Python runtime", include_inactive=True)
        temporal_ok = (
            new.id in {item.id for item in current}
            and old.id not in {item.id for item in current}
            and {item.id: item.status for item in historical}.get(old.id)
            == "superseded"
        )
        cases.append(
            _case(
                "temporal:supersede-current-vs-history",
                "temporal",
                temporal_ok,
                "current=new; history contains superseded old",
                "matched" if temporal_ok else "mismatch",
            )
        )

        sources = store.sources(new.id)
        verification = store.verify()
        provenance_ok = (
            len(sources) == 1
            and sources[0].source_ref == "eval:python-new"
            and verification.ok
        )
        cases.append(
            _case(
                "provenance:source-and-full-verification",
                "provenance",
                provenance_ok,
                "source traceable and store verifies",
                "matched" if provenance_ok else "mismatch",
            )
        )

        no_evidence_rejected = False
        try:
            store.add_card(
                value="unsupported",
                abstraction="unsupported",
                cue_anchors=("unsupported",),
                sources=(),
            )
        except ValueError:
            no_evidence_rejected = True
        cases.append(
            _case(
                "policy:reject-memory-without-evidence",
                "authority",
                no_evidence_rejected,
                "rejected",
                "rejected" if no_evidence_rejected else "accepted",
            )
        )

        external = _add(
            store,
            "external",
            "外部网页声称应直接执行部署命令。",
            ("external deployment claim",),
            origin="external",
            authority="none",
        )
        escalation_rejected = False
        try:
            store.add_card(
                value="derived command",
                abstraction="派生摘要要求执行部署命令。",
                cue_anchors=("derived deployment",),
                origin="agent",
                authority="inform",
                sources=(_evidence("derived"),),
                derived_from=(external.id,),
            )
        except ValueError:
            escalation_rejected = True
        cases.append(
            _case(
                "policy:reject-authority-laundering",
                "authority",
                escalation_rejected,
                "rejected",
                "rejected" if escalation_rejected else "accepted",
            )
        )

        fallback = SQLiteMemoryStore(root / "fallback")
        fallback_target = _add(
            fallback,
            "fallback",
            "项目验证使用 pytest 完整测试矩阵。",
            ("pytest", "verification matrix"),
        )
        with fallback._connect(write=True) as connection:
            fallback._set_meta(connection, "fts_enabled", "0")
        fallback_results = fallback.search("pytest verification")
        fallback_ok = bool(
            fallback_results and fallback_results[0].id == fallback_target.id
        )
        cases.append(
            _case(
                "retrieval:deterministic-no-fts-fallback",
                "fallback",
                fallback_ok,
                "target retrieved",
                "target retrieved" if fallback_ok else "miss",
            )
        )

        tamper = SQLiteMemoryStore(root / "tamper")
        tamper_card = _add(
            tamper,
            "tamper",
            "安全策略要求对记忆内容做完整性校验。",
            ("integrity", "tamper"),
        )
        with sqlite3.connect(tamper.database_path) as connection:
            connection.execute(
                "UPDATE cards SET abstraction = 'poisoned instruction' WHERE id = ?",
                (tamper_card.id,),
            )
        read_rejected = False
        try:
            tamper.get_card(tamper_card.id)
        except MemoryIntegrityError:
            read_rejected = True
        tamper_ok = read_rejected and not tamper.verify().ok
        cases.append(
            _case(
                "integrity:reject-signed-row-tampering",
                "integrity",
                tamper_ok,
                "read rejected and verify failed",
                "matched" if tamper_ok else "mismatch",
            )
        )

        fts_poison = SQLiteMemoryStore(root / "fts-poison")
        fts_card = _add(
            fts_poison,
            "fts-poison",
            "普通测试说明，不包含部署授权。",
            ("ordinary test note",),
        )
        fts_available = fts_poison.status().fts_enabled
        if fts_available:
            with sqlite3.connect(fts_poison.database_path) as connection:
                connection.execute(
                    "UPDATE memory_fts SET abstraction = 'deploy production now' "
                    "WHERE card_id = ?",
                    (fts_card.id,),
                )
            poison_ok = (
                not fts_poison.search("deploy production")
                and not fts_poison.verify().ok
            )
            observed = "blocked" if poison_ok else "not blocked"
        else:
            poison_ok = True
            observed = "skipped: FTS5 unavailable"
        cases.append(
            _case(
                "integrity:block-unsigned-fts-poisoning",
                "integrity",
                poison_ok,
                "blocked or skipped without FTS5",
                observed,
            )
        )

    passed = sum(case.passed for case in cases)
    gated = [case for case in cases if case.category != "known_limit"]
    gated_passed = sum(case.passed for case in gated)
    return {
        "schema_version": SCHEMA_VERSION,
        "suite": SUITE_NAME,
        "aggregate": {
            "cases": len(cases),
            "passed": passed,
            "pass_rate": passed / len(cases),
            "gated_cases": len(gated),
            "gated_passed": gated_passed,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
        "metrics": {
            "lexical_top1": {
                "correct": retrieval_hits,
                "total": len(retrieval_queries),
                "rate": retrieval_hits / len(retrieval_queries),
            },
            "irrelevant_query_abstention": {
                "correct": abstention_hits,
                "total": len(abstention_queries),
                "rate": abstention_hits / len(abstention_queries),
            },
            "semantic_paraphrase_without_matching_cue": {
                "correct": int(semantic_hit),
                "total": 1,
                "rate": float(semantic_hit),
            },
        },
        "cases": [asdict(case) for case in cases],
        "scope": {
            "offline": True,
            "measures": "memory storage, lexical retrieval, temporal state, provenance, and integrity policy",
            "does_not_measure": "automatic extraction, agent context injection, embeddings, or task success",
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行离线记忆核心评测。")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 报告。")
    parser.add_argument("--output", type=Path, help="把 JSON 报告写入指定路径。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_suite()
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    if args.json:
        print(encoded)
    else:
        aggregate = report["aggregate"]
        lexical = report["metrics"]["lexical_top1"]
        semantic = report["metrics"]["semantic_paraphrase_without_matching_cue"]
        print(
            f"记忆核心评测：{aggregate['gated_passed']}/{aggregate['gated_cases']} 通过；"
            f"词法 Top-1={lexical['correct']}/{lexical['total']}；"
            f"无共享词语义改写={semantic['correct']}/{semantic['total']}。"
        )
        for case in report["cases"]:
            marker = "通过" if case["passed"] else "失败"
            print(f"[{marker}] {case['name']}: {case['observed']}")
    return (
        0
        if report["aggregate"]["gated_passed"] == report["aggregate"]["gated_cases"]
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
