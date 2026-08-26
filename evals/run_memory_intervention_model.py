"""Run the memory intervention A/B with a real tool-calling model.

Credentials are read by the normal provider adapter from environment variables.
The emitted report omits model responses, memory values, tool outputs, local
paths and credentials.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from evals.run_memory_intervention import (
    SUITE_NAME,
    _card,
    _feedback,
    _interventions,
    _run_intervention,
)
from mini_code_agent.cli import _load_runtime_env
from mini_code_agent.memory_store import SQLiteMemoryStore
from mini_code_agent.model import create_model

REAL_SUITE_NAME = f"{SUITE_NAME}-real-model"
CONDITIONS = ("no_memory", "static_retrieval", "controlled_memory")


def run_real_intervention(
    *,
    model_name: str,
    provider: str = "auto",
    base_url: str | None = None,
    conditions: tuple[str, ...] = CONDITIONS,
    max_steps: int = 12,
) -> dict[str, Any]:
    unknown = sorted(set(conditions) - set(CONDITIONS))
    if unknown:
        raise ValueError(f"unknown intervention condition(s): {', '.join(unknown)}")
    if not conditions:
        raise ValueError("at least one intervention condition is required")
    if max_steps < 1:
        raise ValueError("max_steps must be positive")

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mca-memory-real-model-") as raw:
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
        interventions = {
            item.name: item for item in _interventions(store)
        }
        results = []
        for condition in conditions:
            model = create_model(
                model_name,
                provider=provider,  # type: ignore[arg-type]
                base_url=base_url,
                temperature=0.0,
                request_timeout=90.0,
                max_retries=2,
            )
            result = _run_intervention(
                interventions[condition],
                root,
                model=model,
                max_steps=max_steps,
            )
            results.append(result)

    successful = sum(
        result["submitted"]
        and result["verification_status"] == "passed"
        and result["correct_file"]
        for result in results
    )
    return {
        "suite": REAL_SUITE_NAME,
        "model": model_name,
        "provider": provider,
        "conditions": list(conditions),
        "model_calls": sum(result["model_calls"] for result in results),
        "elapsed_seconds": round(time.monotonic() - started, 4),
        "results": results,
        "aggregate": {
            "runs": len(results),
            "verified_successes": successful,
            "verified_success_rate": round(successful / len(results), 4),
        },
        "scope": {
            "real_model": True,
            "production_agent_loop": True,
            "real_fixture_tests": True,
            "single_task": True,
            "single_run_per_condition": True,
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
    print(f"model: {report['model']} ({report['provider']})")
    for result in report["results"]:
        print(
            f"{result['system']}: submitted={result['submitted']} "
            f"verified={result['verification_status']} steps={result['steps']} "
            f"edits={result['edit_attempts']} "
            f"failed_after_edit={result['failed_tests_after_edit']}"
        )
    aggregate = report["aggregate"]
    print(
        "verified_successes: "
        f"{aggregate['verified_successes']}/{aggregate['runs']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--provider", choices=("auto", "openai", "deepseek"), default="auto"
    )
    parser.add_argument("--base-url")
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Private env file; defaults to the file created by `mca init`.",
    )
    parser.add_argument("--condition", action="append", choices=CONDITIONS)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    _load_runtime_env(args.env_file)
    report = run_real_intervention(
        model_name=args.model,
        provider=args.provider,
        base_url=args.base_url,
        conditions=tuple(args.condition or CONDITIONS),
        max_steps=args.max_steps,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    if args.json:
        print(payload)
    else:
        _print_report(report)
    return 0 if report["aggregate"]["verified_successes"] == len(report["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
