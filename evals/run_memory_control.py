"""Evaluate the outcome-aware memory controller against static retrieval."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from mini_code_agent.memory_control import (
    EvidenceGroundedMemoryController,
    MemoryControlContext,
)
from mini_code_agent.memory_models import EvidenceSource
from mini_code_agent.memory_retrieval import (
    EvidenceTemporalRetriever,
    MemoryQuery,
    MemoryScope,
)
from mini_code_agent.memory_store import SQLiteMemoryStore

SUITE_NAME = "memory-control-v0"


def _source(label: str) -> EvidenceSource:
    return EvidenceSource(
        source_type="control_eval",
        source_ref=f"eval:{label}",
        source_sha256=hashlib.sha256(label.encode()).hexdigest(),
        origin="trusted_tool",
    )


def _card(
    store: SQLiteMemoryStore,
    label: str,
    cue: str,
    *,
    subtype: str = "workflow",
    value: str | None = None,
):
    return store.add_card(
        value=value or f"Use {label} for this workflow.",
        abstraction=f"{label}: {cue}",
        cue_anchors=(cue,),
        kind="procedural",
        subtype=subtype,
        scope="workspace",
        scope_key="control-eval",
        origin="agent",
        authority="inform",
        confidence=0.9,
        importance=0.8,
        valid_from="2026-01-01T00:00:00Z",
        sources=(_source(label),),
    )


def _query(text: str) -> MemoryQuery:
    return MemoryQuery(
        text,
        scopes=(MemoryScope("workspace", "control-eval"),),
        as_of="2026-08-18T00:00:00Z",
    )


def _seed_feedback(
    store: SQLiteMemoryStore,
    card_id: str,
    label: str,
    *,
    success: bool,
    harmful: bool,
) -> None:
    for index in range(3):
        decision = store.record_memory_decision(
            query_sha256=hashlib.sha256(f"{label}-{index}".encode()).hexdigest(),
            stage="working",
            operation="retrieve",
            selected_card_ids=(card_id,),
            expected_utility=0.7,
            reason="controlled_feedback",
            shadow=False,
        )
        store.record_memory_outcome(
            decision.id,
            success=success,
            reward=1.0 if success else -1.0,
            harmful=harmful,
            token_cost=20,
            evidence=_source(f"{label}-outcome-{index}"),
        )


def run_control_eval() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []

    def record(name: str, category: str, passed: bool) -> None:
        cases.append({"name": name, "category": category, "passed": passed})

    with tempfile.TemporaryDirectory(prefix="mca-memory-control-") as raw:
        store = SQLiteMemoryStore(Path(raw) / "memory")
        helpful = _card(store, "helpful", "release verification")
        harmful = _card(store, "harmful", "release verification")
        _card(store, "normal-lockfile", "lockfile verification")
        contraindication = _card(
            store,
            "lockfile-exception",
            "lockfile verification",
            subtype="contraindication",
            value="Do not rely on the fast check after lockfile changes.",
        )
        _seed_feedback(
            store, helpful.id, "helpful", success=True, harmful=False
        )
        _seed_feedback(
            store, harmful.id, "harmful", success=False, harmful=True
        )

        static = EvidenceTemporalRetriever(store).retrieve(
            _query("release verification")
        )
        static_ids = {item.card_id for item in static.items}
        record(
            "static-retrieval-exposes-harmful-candidate",
            "baseline",
            helpful.id in static_ids and harmful.id in static_ids,
        )

        controller = EvidenceGroundedMemoryController(store)
        controlled = controller.decide(
            MemoryControlContext(query=_query("release verification"))
        )
        controlled_ids = {item.card_id for item in controlled.items}
        record(
            "outcome-feedback-suppresses-harmful-candidate",
            "feedback",
            controlled.operation == "retrieve"
            and helpful.id in controlled_ids
            and harmful.id not in controlled_ids,
        )

        warning = controller.decide(
            MemoryControlContext(
                query=_query("lockfile verification"),
                stage="config_changed",
            )
        )
        warning_by_id = {item.card_id: item for item in warning.items}
        record(
            "contraindication-is-explicit-warning",
            "counter_memory",
            warning.operation == "retrieve_with_warning"
            and warning_by_id[contraindication.id].role == "contraindication",
        )

        ordinary = controller.decide(
            MemoryControlContext(query=_query("Mars weather"))
        )
        stuck = controller.decide(
            MemoryControlContext(
                query=_query("Mars weather"), stage="stuck", recent_failures=2
            )
        )
        record(
            "irrelevant-query-abstains",
            "abstention",
            ordinary.operation == "no_memory" and not ordinary.items,
        )
        record(
            "stuck-agent-requests-requery",
            "adaptive_control",
            stuck.operation == "requery" and not stuck.items,
        )

        before_shadow = {
            item.card_id: item.uses for item in store.memory_utility_stats()
        }
        shadow = controller.decide(
            MemoryControlContext(
                query=_query("release verification"), shadow=True, record=True
            )
        )
        controller.record_outcome(
            shadow.decision_id,
            success=True,
            reward=1.0,
            token_cost=10,
            evidence=_source("shadow-outcome"),
        )
        after_shadow = {
            item.card_id: item.uses for item in store.memory_utility_stats()
        }
        record(
            "shadow-policy-neither-injects-nor-learns",
            "counterfactual",
            shadow.render() == "" and before_shadow == after_shadow,
        )

        passed = sum(int(case["passed"]) for case in cases)
        return {
            "suite": SUITE_NAME,
            "scope": {
                "offline": True,
                "deterministic": True,
                "model_calls": 0,
                "claims_generalization": False,
            },
            "aggregate": {
                "cases": len(cases),
                "passed": passed,
                "pass_rate": round(passed / len(cases), 4),
            },
            "metrics": {
                "static_harmful_candidates": int(harmful.id in static_ids),
                "controlled_harmful_candidates": int(
                    harmful.id in controlled_ids
                ),
                "store_integrity": store.verify().ok,
            },
            "cases": cases,
            "acceptance": {"passed": passed == len(cases) and store.verify().ok},
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run_control_eval()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"suite: {report['suite']}")
        print(
            f"cases: {report['aggregate']['passed']}/{report['aggregate']['cases']}"
        )
        for case in report["cases"]:
            print(f"{'PASS' if case['passed'] else 'FAIL'} {case['name']}")
    return 0 if report["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
