"""Run a repeated real-model memory A/B on a multi-file checkout repair."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from evals.run_evals import REPOSITORY_ROOT, _executor, _scenario_state_directory
from evals.run_memory_intervention import Intervention
from mini_code_agent.agent import MiniCodeAgent
from mini_code_agent.cli import _load_runtime_env
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
from mini_code_agent.model import create_model

SUITE_NAME = "memory-complex-agent-intervention-v0-real-model"
CONDITIONS = ("no_memory", "static_retrieval", "controlled_memory")
TASK = (
    "Fix the checkout total regression across the implementation modules. "
    "Preserve public function signatures and input validation, do not edit tests, "
    "and submit only after the complete configured test suite passes."
)
QUERY = "checkout settlement rule regression"
SCOPE_KEY = "memory-complex-intervention"
EXPECTED_FILES = frozenset({"invoice.py", "pricing.py", "shipping.py"})


def _source(label: str) -> EvidenceSource:
    return EvidenceSource(
        source_type="memory_complex_intervention_eval",
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
            reason="complex_intervention_fixture",
            shadow=False,
        )
        store.record_memory_outcome(
            decision.id,
            success=success,
            reward=1.0 if success else -1.0,
            harmful=harmful,
            token_cost=80,
            evidence=_source(f"{label}-outcome-{index}"),
        )


def _interventions(store: SQLiteMemoryStore) -> dict[str, Intervention]:
    query = MemoryQuery(
        QUERY,
        scopes=(MemoryScope("workspace", SCOPE_KEY),),
        as_of="2026-08-18T00:00:00Z",
    )
    static = EvidenceTemporalRetriever(store).retrieve(query)
    controlled = EvidenceGroundedMemoryController(store).decide(
        MemoryControlContext(query=query, stage="working", token_budget=2_000)
    )
    return {
        "no_memory": Intervention("no_memory", "", 0, 0, "no_memory"),
        "static_retrieval": Intervention(
            "static_retrieval",
            static.render(),
            len(static.items),
            sum("OUTDATED_RULE" in item.value for item in static.items),
            static.decision.kind,
        ),
        "controlled_memory": Intervention(
            "controlled_memory",
            controlled.render(),
            len(controlled.items),
            sum("OUTDATED_RULE" in item.value for item in controlled.items),
            controlled.operation,
        ),
    }


def _changed_files(changes: dict[str, list[str]]) -> list[str]:
    names = set()
    for category, paths in changes.items():
        if category not in {"modified", "created", "deleted"}:
            continue
        names.update(Path(path).as_posix() for path in paths)
    return sorted(names)


def _run_condition(
    *,
    intervention: Intervention,
    root: Path,
    model: Any,
    repeat: int,
    max_steps: int,
    fixture_name: str = "memory_complex",
    task: str = TASK,
    expected_files: frozenset[str] = EXPECTED_FILES,
) -> dict[str, Any]:
    fixture = REPOSITORY_ROOT / "evals" / "fixtures" / fixture_name
    run_root = root / f"repeat-{repeat}" / intervention.name
    workspace = run_root / "workspace"
    shutil.copytree(fixture, workspace)
    executor = _executor(workspace)
    before = executor.workspace_fingerprint()
    with _scenario_state_directory(run_root / "state"):
        audit = MiniCodeAgent(
            model,
            executor,
            max_steps=max_steps,
            quiet=True,
        ).run(task, advisory_context=intervention.context)
    changes = _changed_files(before.diff(executor.workspace_fingerprint()))
    tools = [
        event for event in audit["events"] if event.get("type") == "tool"
    ]
    edit_names = {"apply_patch", "replace_lines", "write_file"}
    edits = [event for event in tools if event.get("tool") in edit_names]
    first_edit = next(
        (
            index
            for index, event in enumerate(tools)
            if event.get("tool") in edit_names
        ),
        len(tools),
    )
    failed_after_edit = sum(
        event.get("tool") == "run_tests" and int(event.get("returncode", 0)) != 0
        for event in tools[first_edit + 1 :]
    )
    submitted = audit["exit_status"] == "Submitted"
    verified = audit["verification_status"] == "passed"
    outcome_payload = {
        "exit_status": audit["exit_status"],
        "verification_status": audit["verification_status"],
        "changed_files": changes,
        "tools": [
            {
                "tool": event.get("tool"),
                "returncode": event.get("returncode"),
                "tests_run": event.get("tests_run"),
            }
            for event in tools
        ],
    }
    return {
        "repeat": repeat,
        "system": intervention.name,
        "operation": intervention.operation,
        "injected_items": intervention.injected_items,
        "harmful_items": intervention.harmful_items,
        "context_chars": len(intervention.context),
        "submitted": submitted,
        "verification_status": audit["verification_status"],
        "expected_files_only": set(changes) == expected_files,
        "changed_files": changes,
        "steps": int(audit["steps"]),
        "model_calls": sum(
            event.get("type") == "model" for event in audit["events"]
        ),
        "tool_calls": len(tools),
        "read_calls": sum(event.get("tool") == "read_file" for event in tools),
        "edit_attempts": len(edits),
        "test_runs": sum(event.get("tool") == "run_tests" for event in tools),
        "failed_tests_after_edit": failed_after_edit,
        "verified_success": submitted and verified and set(changes) == expected_files,
        "outcome_sha256": hashlib.sha256(
            json.dumps(
                outcome_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    }


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        grouped[result["system"]].append(result)
    systems = []
    for condition in CONDITIONS:
        runs = grouped[condition]
        if not runs:
            continue
        systems.append(
            {
                "system": condition,
                "runs": len(runs),
                "verified_successes": sum(run["verified_success"] for run in runs),
                "mean_steps": round(sum(run["steps"] for run in runs) / len(runs), 4),
                "mean_tool_calls": round(
                    sum(run["tool_calls"] for run in runs) / len(runs), 4
                ),
                "mean_read_calls": round(
                    sum(run["read_calls"] for run in runs) / len(runs), 4
                ),
                "mean_edit_attempts": round(
                    sum(run["edit_attempts"] for run in runs) / len(runs), 4
                ),
                "failed_tests_after_edit": sum(
                    run["failed_tests_after_edit"] for run in runs
                ),
            }
        )
    return {
        "runs": len(results),
        "verified_successes": sum(result["verified_success"] for result in results),
        "systems": systems,
    }


def run_complex_intervention(
    *,
    model_name: str,
    provider: str = "auto",
    base_url: str | None = None,
    conditions: tuple[str, ...] = CONDITIONS,
    repeats: int = 1,
    max_steps: int = 20,
) -> dict[str, Any]:
    unknown = sorted(set(conditions) - set(CONDITIONS))
    if unknown:
        raise ValueError(f"unknown intervention condition(s): {', '.join(unknown)}")
    if not conditions or repeats < 1 or max_steps < 1:
        raise ValueError("conditions, repeats and max_steps must be positive")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mca-memory-complex-model-") as raw:
        root = Path(raw)
        store = SQLiteMemoryStore(root / "memory")
        helpful = _card(
            store,
            "verified-checkout-rule",
            (
                "VERIFIED_RULE: apply the customer tier discount first; standard "
                "shipping is free only when the discounted subtotal is at least "
                "$100; expedited always adds $12 to standard shipping; calculate "
                "tax last on discounted subtotal plus shipping; round the final total."
            ),
        )
        harmful = _card(
            store,
            "outdated-checkout-rule",
            (
                "OUTDATED_RULE: decide free shipping from the original subtotal, "
                "waive expedited shipping above the threshold, and tax only the "
                "discounted merchandise subtotal."
            ),
        )
        _feedback(store, helpful.id, "helpful", success=True, harmful=False)
        _feedback(store, harmful.id, "harmful", success=False, harmful=True)
        interventions = _interventions(store)
        results = []
        selected = [name for name in CONDITIONS if name in conditions]
        for repeat_index in range(repeats):
            offset = repeat_index % len(selected)
            balanced_order = selected[offset:] + selected[:offset]
            for condition in balanced_order:
                model = create_model(
                    model_name,
                    provider=provider,  # type: ignore[arg-type]
                    base_url=base_url,
                    temperature=0.0,
                    request_timeout=90.0,
                    max_retries=2,
                )
                results.append(
                    _run_condition(
                        intervention=interventions[condition],
                        root=root,
                        model=model,
                        repeat=repeat_index + 1,
                        max_steps=max_steps,
                    )
                )
        integrity_ok = store.verify().ok
    aggregate = _aggregate(results)
    return {
        "suite": SUITE_NAME,
        "model": model_name,
        "provider": provider,
        "conditions": list(conditions),
        "repeats": repeats,
        "model_calls": sum(result["model_calls"] for result in results),
        "elapsed_seconds": round(time.monotonic() - started, 4),
        "results": results,
        "aggregate": aggregate,
        "store_integrity": integrity_ok,
        "scope": {
            "real_model": True,
            "production_agent_loop": True,
            "real_fixture_tests": True,
            "multi_file_task": True,
            "balanced_condition_order": True,
            "claims_generalization": False,
        },
        "sanitization": {
            "responses_omitted": True,
            "memory_values_omitted": True,
            "tool_outputs_omitted": True,
            "local_paths_omitted": True,
            "credentials_omitted": True,
        },
    }


def _print_report(report: dict[str, Any]) -> None:
    print(f"suite: {report['suite']}")
    print(
        f"model: {report['model']} ({report['provider']}); "
        f"runs: {report['aggregate']['runs']}"
    )
    for result in report["aggregate"]["systems"]:
        print(
            f"{result['system']}: success={result['verified_successes']}/"
            f"{result['runs']} mean_steps={result['mean_steps']:.2f} "
            f"mean_reads={result['mean_read_calls']:.2f} "
            f"mean_edits={result['mean_edit_attempts']:.2f} "
            f"failed_after_edit={result['failed_tests_after_edit']}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--provider", choices=("auto", "openai", "deepseek"), default="auto"
    )
    parser.add_argument("--base-url")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--condition", action="append", choices=CONDITIONS)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    _load_runtime_env(args.env_file)
    report = run_complex_intervention(
        model_name=args.model,
        provider=args.provider,
        base_url=args.base_url,
        conditions=tuple(args.condition or CONDITIONS),
        repeats=args.repeats,
        max_steps=args.max_steps,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    if args.json:
        print(payload)
    else:
        _print_report(report)
    expected = report["aggregate"]["runs"]
    return 0 if report["aggregate"]["verified_successes"] == expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
