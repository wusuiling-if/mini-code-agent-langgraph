"""Compare memory policies using experience formed from a real verified run.

Unlike the mechanism fixtures, this evaluator does not hand-write memory
content or seed helpful/harmful outcomes. A real model first repairs a training
fixture without memory. The verified implementation diff is then converted into
an evidence-bound experience card and applied to an unseen transfer fixture.
Controller feedback comes only from the transfer run's verification outcome.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from evals.run_evals import REPOSITORY_ROOT
from evals.run_memory_complex_intervention_model import (
    EXPECTED_FILES as TRAINING_FILES,
)
from evals.run_memory_complex_intervention_model import (
    TASK as TRAINING_TASK,
)
from evals.run_memory_complex_intervention_model import _run_condition
from evals.run_memory_intervention import Intervention
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
    lexical_tokens,
)
from mini_code_agent.memory_store import SQLiteMemoryStore
from mini_code_agent.model import create_model

SUITE_NAME = "memory-natural-experience-transfer-v0-real-model"
CONDITIONS = ("no_memory", "original_retriever", "outcome_controller")
HOLDOUT_TASK = (
    "Fix the checkout settlement regression across the implementation modules. "
    "Preserve public function signatures and input validation, do not edit tests, "
    "and submit only after the complete configured test suite passes."
)
HOLDOUT_FILES = frozenset({"delivery.py", "discounts.py", "statement.py"})
SCOPE_KEY = "checkout-family"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _verified_diff(
    original: Path,
    repaired: Path,
    files: frozenset[str],
) -> str:
    parts = []
    for name in sorted(files):
        before = (original / name).read_text(encoding="utf-8").splitlines(
            keepends=True
        )
        after = (repaired / name).read_text(encoding="utf-8").splitlines(
            keepends=True
        )
        parts.extend(
            difflib.unified_diff(
                before,
                after,
                fromfile=f"before/{name}",
                tofile=f"after/{name}",
            )
        )
    diff = "".join(parts)
    if not diff.strip():
        raise ValueError("verified training run produced no implementation diff")
    return diff


def _automatic_cues(task: str) -> tuple[str, ...]:
    tokens = [
        normalized
        for token in lexical_tokens(task)
        if len(normalized := token.strip("._:/+-")) >= 5
    ]
    return tuple(dict.fromkeys(tokens))[:24]


def _form_experience(
    store: SQLiteMemoryStore,
    *,
    task: str,
    diff: str,
    training_result: dict[str, Any],
):
    if not training_result["verified_success"]:
        raise ValueError("cannot form experience from an unverified training run")
    evidence_payload = {
        "task_sha256": _sha256(task.encode()),
        "diff_sha256": _sha256(diff.encode()),
        "outcome_sha256": training_result["outcome_sha256"],
        "changed_files": training_result["changed_files"],
        "verification_status": training_result["verification_status"],
    }
    evidence_bytes = json.dumps(
        evidence_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    evidence_sha = _sha256(evidence_bytes)
    return store.add_card(
        value=(
            "Automatically captured verified repair experience.\n"
            f"Training task SHA-256: {evidence_payload['task_sha256']}\n"
            "Verified implementation diff:\n"
            f"{diff}"
        ),
        abstraction=task,
        cue_anchors=_automatic_cues(task),
        kind="episodic",
        subtype="verified_repair",
        scope="workspace",
        scope_key=SCOPE_KEY,
        origin="trusted_tool",
        authority="inform",
        confidence=1.0,
        importance=0.8,
        valid_from="2026-08-18T00:00:00Z",
        sources=(
            EvidenceSource(
                source_type="verified_agent_trajectory",
                source_ref=f"trajectory:{evidence_sha}",
                source_sha256=evidence_sha,
                origin="trusted_tool",
            ),
        ),
    )


def _holdout_query() -> MemoryQuery:
    return MemoryQuery(
        HOLDOUT_TASK,
        scopes=(MemoryScope("workspace", SCOPE_KEY),),
        as_of="2026-08-18T00:00:00Z",
        limit=3,
    )


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        grouped[result["system"]].append(result)
    systems = []
    for condition in CONDITIONS:
        runs = grouped[condition]
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


def run_natural_intervention(
    *,
    model_name: str,
    provider: str = "auto",
    base_url: str | None = None,
    repeats: int = 3,
    max_steps: int = 20,
) -> dict[str, Any]:
    if repeats < 1 or max_steps < 1:
        raise ValueError("repeats and max_steps must be positive")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mca-memory-natural-model-") as raw:
        root = Path(raw)
        training_model = create_model(
            model_name,
            provider=provider,  # type: ignore[arg-type]
            base_url=base_url,
            temperature=0.0,
            request_timeout=90.0,
            max_retries=2,
        )
        training = _run_condition(
            intervention=Intervention("training", "", 0, 0, "no_memory"),
            root=root,
            model=training_model,
            repeat=0,
            max_steps=max_steps,
            fixture_name="memory_complex",
            task=TRAINING_TASK,
            expected_files=TRAINING_FILES,
        )
        if not training["verified_success"]:
            raise RuntimeError("real-model training run did not produce a verified repair")
        repaired = root / "repeat-0" / "training" / "workspace"
        original = REPOSITORY_ROOT / "evals" / "fixtures" / "memory_complex"
        diff = _verified_diff(original, repaired, TRAINING_FILES)
        store = SQLiteMemoryStore(root / "memory")
        experience = _form_experience(
            store,
            task=TRAINING_TASK,
            diff=diff,
            training_result=training,
        )
        original_pack = EvidenceTemporalRetriever(store).retrieve(_holdout_query())
        if original_pack.decision.kind != "use_memory":
            raise RuntimeError("automatically formed experience was not retrievable")
        results = []
        for repeat_index in range(repeats):
            offset = repeat_index % len(CONDITIONS)
            order = CONDITIONS[offset:] + CONDITIONS[:offset]
            for condition in order:
                decision_id = ""
                if condition == "no_memory":
                    intervention = Intervention(condition, "", 0, 0, "no_memory")
                elif condition == "original_retriever":
                    intervention = Intervention(
                        condition,
                        original_pack.render(),
                        len(original_pack.items),
                        0,
                        original_pack.decision.kind,
                    )
                else:
                    controlled = EvidenceGroundedMemoryController(store).decide(
                        MemoryControlContext(
                            query=_holdout_query(),
                            stage="working",
                            token_budget=4_000,
                            record=True,
                        )
                    )
                    decision_id = controlled.decision_id
                    intervention = Intervention(
                        condition,
                        controlled.render(),
                        len(controlled.items),
                        0,
                        controlled.operation,
                    )
                model = create_model(
                    model_name,
                    provider=provider,  # type: ignore[arg-type]
                    base_url=base_url,
                    temperature=0.0,
                    request_timeout=90.0,
                    max_retries=2,
                )
                result = _run_condition(
                    intervention=intervention,
                    root=root,
                    model=model,
                    repeat=repeat_index + 1,
                    max_steps=max_steps,
                    fixture_name="memory_natural_holdout",
                    task=HOLDOUT_TASK,
                    expected_files=HOLDOUT_FILES,
                )
                results.append(result)
                if decision_id:
                    EvidenceGroundedMemoryController(store).record_outcome(
                        decision_id,
                        success=result["verified_success"],
                        reward=1.0 if result["verified_success"] else -1.0,
                        harmful=False,
                        token_cost=max(1, len(intervention.context) // 4),
                        evidence=EvidenceSource(
                            source_type="verified_transfer_outcome",
                            source_ref=(
                                f"eval:holdout:{repeat_index + 1}:"
                                f"{result['outcome_sha256']}"
                            ),
                            source_sha256=result["outcome_sha256"],
                            origin="trusted_tool",
                        ),
                    )
        utility = store.memory_utility_stats((experience.id,))[0]
        memory_evidence_count = len(store.sources(experience.id))
        automatic_cue_count = len(experience.cue_anchors)
        integrity_ok = store.verify().ok
    aggregate = _aggregate(results)
    return {
        "suite": SUITE_NAME,
        "model": model_name,
        "provider": provider,
        "repeats": repeats,
        "model_calls": training["model_calls"]
        + sum(result["model_calls"] for result in results),
        "elapsed_seconds": round(time.monotonic() - started, 4),
        "formation": {
            "human_written_memory": False,
            "seeded_outcomes": False,
            "source": "real_verified_training_diff",
            "training_verified": training["verified_success"],
            "training_steps": training["steps"],
            "training_changed_files": training["changed_files"],
            "memory_content_sha256": experience.content_sha256,
            "memory_evidence_count": memory_evidence_count,
            "automatic_cue_count": automatic_cue_count,
        },
        "results": results,
        "aggregate": aggregate,
        "controller_feedback": {
            "uses": utility.uses,
            "successes": utility.successes,
            "failures": utility.failures,
            "harmful_uses": utility.harmful_uses,
        },
        "store_integrity": integrity_ok,
        "scope": {
            "real_model": True,
            "production_agent_loop": True,
            "real_fixture_tests": True,
            "unseen_transfer_fixture": True,
            "same_automatically_formed_memory": True,
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
        f"training_verified={report['formation']['training_verified']}"
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
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    _load_runtime_env(args.env_file)
    report = run_natural_intervention(
        model_name=args.model,
        provider=args.provider,
        base_url=args.base_url,
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
    return 0 if report["aggregate"]["verified_successes"] == report["aggregate"]["runs"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
