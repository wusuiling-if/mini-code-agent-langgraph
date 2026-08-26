"""Run a deterministic memory intervention through the production Agent loop.

This is an integration experiment, not a model-quality benchmark. The adaptive
model stub reads the same advisory context a real model would receive, while the
production agent, executor, verification gate and real fixture tests remain in
the loop.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

from evals.run_evals import (
    REPOSITORY_ROOT,
    PlannedResponse,
    ScriptedEvalModel,
    _executor,
    _response,
    _scenario_state_directory,
)
from mini_code_agent.agent import MiniCodeAgent
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

SUITE_NAME = "memory-agent-intervention-v0"
TASK = "Fix triple() with the smallest verified correction."
QUERY = "triple repair procedure"
SCOPE_KEY = "memory-intervention"


@dataclass(frozen=True)
class Intervention:
    name: str
    context: str
    injected_items: int
    harmful_items: int
    operation: str


class AdvisoryAwareEvalModel:
    """Choose a fixed tool plan from the advisory context seen on first call."""

    def __init__(self) -> None:
        self._delegate: ScriptedEvalModel | None = None

    def bind_tools(self, tools: list[Any]) -> AdvisoryAwareEvalModel:
        return self

    def invoke(self, messages: list[Any]) -> AIMessage:
        if self._delegate is None:
            visible = "\n".join(str(message.content) for message in messages)
            self._delegate = ScriptedEvalModel(self._plan(visible))
        return self._delegate.invoke(messages)

    @staticmethod
    def _plan(visible: str) -> tuple[PlannedResponse, ...]:
        if "PATCH_WRONG" in visible:
            return (
                _response("Reproduce the failure.", ("run_tests", {})),
                _response(
                    "Follow the retrieved but harmful attempt.",
                    (
                        "apply_patch",
                        {
                            "path": "transform.py",
                            "old": "return value * 2",
                            "new": "return value + 3",
                        },
                    ),
                ),
                _response("Verify the attempted correction.", ("run_tests", {})),
                _response(
                    "Recover with the correct multiplication.",
                    (
                        "apply_patch",
                        {
                            "path": "transform.py",
                            "old": "return value + 3",
                            "new": "return value * 3",
                        },
                    ),
                ),
                _response("Verify recovery.", ("run_tests", {})),
                _response(
                    "Submit the verified result.",
                    ("submit", {"summary": "Verified triple() repair."}),
                ),
            )
        if "PATCH_CORRECT" in visible:
            return (
                _response("Reproduce the failure.", ("run_tests", {})),
                _response(
                    "Apply the evidence-backed correction.",
                    (
                        "apply_patch",
                        {
                            "path": "transform.py",
                            "old": "return value * 2",
                            "new": "return value * 3",
                        },
                    ),
                ),
                _response("Verify the correction.", ("run_tests", {})),
                _response(
                    "Submit the verified result.",
                    ("submit", {"summary": "Verified triple() repair."}),
                ),
            )
        return (
            _response("Reproduce the failure.", ("run_tests", {})),
            _response("Inspect the implementation.", ("read_file", {"path": "transform.py"})),
            _response(
                "Apply the inferred correction.",
                (
                    "apply_patch",
                    {
                        "path": "transform.py",
                        "old": "return value * 2",
                        "new": "return value * 3",
                    },
                ),
            ),
            _response("Verify the correction.", ("run_tests", {})),
            _response(
                "Submit the verified result.",
                ("submit", {"summary": "Verified triple() repair."}),
            ),
        )


def _source(label: str) -> EvidenceSource:
    return EvidenceSource(
        source_type="memory_intervention_eval",
        source_ref=f"eval:{label}",
        source_sha256=hashlib.sha256(label.encode()).hexdigest(),
        origin="trusted_tool",
    )


def _card(store: SQLiteMemoryStore, label: str, value: str):
    return store.add_card(
        value=value,
        abstraction=f"{label}: {QUERY}",
        cue_anchors=(QUERY,),
        kind="procedural",
        subtype="workflow",
        scope="workspace",
        scope_key=SCOPE_KEY,
        origin="agent",
        authority="inform",
        confidence=0.9,
        importance=0.8,
        valid_from="2026-01-01T00:00:00Z",
        sources=(_source(label),),
    )


def _feedback(
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
            reason="intervention_fixture",
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


def _query() -> MemoryQuery:
    return MemoryQuery(
        QUERY,
        scopes=(MemoryScope("workspace", SCOPE_KEY),),
        as_of="2026-08-18T00:00:00Z",
    )


def _interventions(store: SQLiteMemoryStore) -> tuple[Intervention, ...]:
    static = EvidenceTemporalRetriever(store).retrieve(_query())
    controlled = EvidenceGroundedMemoryController(store).decide(
        MemoryControlContext(query=_query(), stage="working")
    )
    return (
        Intervention("no_memory", "", 0, 0, "no_memory"),
        Intervention(
            "static_retrieval",
            static.render(),
            len(static.items),
            sum("PATCH_WRONG" in item.value for item in static.items),
            static.decision.kind,
        ),
        Intervention(
            "controlled_memory",
            controlled.render(),
            len(controlled.items),
            sum("PATCH_WRONG" in item.value for item in controlled.items),
            controlled.operation,
        ),
    )


def _run_intervention(
    intervention: Intervention,
    root: Path,
    *,
    model: Any | None = None,
    max_steps: int = 8,
) -> dict[str, Any]:
    fixture = REPOSITORY_ROOT / "evals" / "fixtures" / "failure_recovery"
    workspace = root / intervention.name / "workspace"
    state = root / intervention.name / "state"
    shutil.copytree(fixture, workspace)
    executor = _executor(workspace)
    with _scenario_state_directory(state):
        audit = MiniCodeAgent(
            model if model is not None else AdvisoryAwareEvalModel(),
            executor,
            max_steps=max_steps,
            quiet=True,
        ).run(TASK, advisory_context=intervention.context)
    tools = [
        event for event in audit["events"] if event.get("type") == "tool"
    ]
    edits = [event for event in tools if event.get("tool") == "apply_patch"]
    tests = [event for event in tools if event.get("tool") == "run_tests"]
    first_edit_index = next(
        (index for index, event in enumerate(tools) if event.get("tool") == "apply_patch"),
        len(tools),
    )
    failed_after_edit = sum(
        event.get("tool") == "run_tests" and int(event.get("returncode", 0)) != 0
        for event in tools[first_edit_index + 1 :]
    )
    final_source = (workspace / "transform.py").read_text(encoding="utf-8")
    return {
        "system": intervention.name,
        "operation": intervention.operation,
        "injected_items": intervention.injected_items,
        "harmful_items": intervention.harmful_items,
        "context_chars": len(intervention.context),
        "submitted": audit["exit_status"] == "Submitted",
        "verification_status": audit["verification_status"],
        "correct_file": "return value * 3" in final_source,
        "steps": int(audit["steps"]),
        "model_calls": sum(
            event.get("type") == "model" for event in audit["events"]
        ),
        "tool_calls": len(tools),
        "edit_attempts": len(edits),
        "test_runs": len(tests),
        "failed_tests_after_edit": failed_after_edit,
    }


def run_intervention_eval() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="mca-memory-intervention-") as raw:
        root = Path(raw)
        store = SQLiteMemoryStore(root / "memory")
        helpful = _card(
            store,
            "helpful",
            "PATCH_CORRECT: change `return value * 2` to `return value * 3`.",
        )
        harmful = _card(
            store,
            "harmful",
            "PATCH_WRONG: change `return value * 2` to `return value + 3`.",
        )
        _feedback(store, helpful.id, "helpful", success=True, harmful=False)
        _feedback(store, harmful.id, "harmful", success=False, harmful=True)
        results = [
            _run_intervention(intervention, root)
            for intervention in _interventions(store)
        ]
        by_name = {result["system"]: result for result in results}
        no_memory = by_name["no_memory"]
        static = by_name["static_retrieval"]
        controlled = by_name["controlled_memory"]
        checks = {
            "all_verified": all(
                result["submitted"]
                and result["verification_status"] == "passed"
                and result["correct_file"]
                for result in results
            ),
            "static_exposes_harm": static["harmful_items"] == 1,
            "control_suppresses_harm": controlled["harmful_items"] == 0,
            "control_avoids_failed_edit": (
                static["failed_tests_after_edit"] == 1
                and controlled["failed_tests_after_edit"] == 0
            ),
            "control_reduces_steps": (
                controlled["steps"] < no_memory["steps"] < static["steps"]
            ),
        }
        return {
            "suite": SUITE_NAME,
            "scope": {
                "offline": True,
                "deterministic": True,
                "production_agent_loop": True,
                "real_fixture_tests": True,
                "model_calls": 0,
                "claims_model_quality": False,
            },
            "results": results,
            "checks": checks,
            "acceptance": {"passed": all(checks.values()) and store.verify().ok},
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run_intervention_eval()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"suite: {report['suite']}")
        for result in report["results"]:
            print(
                f"{result['system']}: steps={result['steps']} "
                f"edits={result['edit_attempts']} "
                f"failed_after_edit={result['failed_tests_after_edit']} "
                f"harmful_items={result['harmful_items']}"
            )
        print(
            "acceptance: "
            f"{'PASS' if report['acceptance']['passed'] else 'FAIL'}"
        )
    return 0 if report["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
