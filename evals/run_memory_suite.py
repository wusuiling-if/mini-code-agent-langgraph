"""Run the complete deterministic memory release gate without model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any, Callable

from evals.run_memory_comparison import run_comparison
from evals.run_memory_control import run_control_eval
from evals.run_memory_evals import run_suite as run_core
from evals.run_memory_formation import run_formation
from evals.run_memory_intervention import run_intervention_eval
from evals.run_memory_long_conversation import run_diagnostic
from evals.run_memory_longitudinal import run_longitudinal
from evals.run_memory_portability import run_portability


SCHEMA_VERSION = 1
SUITE_NAME = "memory-release-v0.5.0"


def _source_sha256() -> str:
    paths = (
        Path(__file__),
        Path(run_comparison.__code__.co_filename),
        Path(run_control_eval.__code__.co_filename),
        Path(run_core.__code__.co_filename),
        Path(run_formation.__code__.co_filename),
        Path(run_intervention_eval.__code__.co_filename),
        Path(run_diagnostic.__code__.co_filename),
        Path(run_longitudinal.__code__.co_filename),
        Path(run_portability.__code__.co_filename),
    )
    digest = hashlib.sha256()
    for path in sorted({item.resolve() for item in paths}, key=str):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _core_passed(report: dict[str, Any]) -> bool:
    aggregate = report["aggregate"]
    return aggregate["gated_passed"] == aggregate["gated_cases"]


def _long_conversation_passed(report: dict[str, Any]) -> bool:
    scope = report["scope"]
    retrieval = report["retrieval"]
    return (
        report["model_calls"] == 0
        and retrieval["correct"] == scope["cases"]
        and report["store_integrity"] is True
    )


def _acceptance_passed(report: dict[str, Any]) -> bool:
    return report["acceptance"]["passed"] is True


def run_release_suite() -> dict[str, Any]:
    started = time.monotonic()
    runners: tuple[
        tuple[str, Callable[[], dict[str, Any]], Callable[[dict[str, Any]], bool]],
        ...,
    ] = (
        ("core", run_core, _core_passed),
        ("architecture_comparison", run_comparison, _acceptance_passed),
        ("formation", run_formation, _acceptance_passed),
        ("portability", run_portability, _acceptance_passed),
        ("longitudinal", run_longitudinal, _acceptance_passed),
        ("long_conversation", run_diagnostic, _long_conversation_passed),
        ("control_experiment", run_control_eval, _acceptance_passed),
        ("agent_intervention_experiment", run_intervention_eval, _acceptance_passed),
    )
    results: list[dict[str, Any]] = []
    for name, runner, predicate in runners:
        report = runner()
        results.append(
            {
                "name": name,
                "suite": report["suite"],
                "passed": bool(predicate(report)),
                "report": report,
            }
        )
    passed = sum(int(result["passed"]) for result in results)
    return {
        "schema_version": SCHEMA_VERSION,
        "suite": SUITE_NAME,
        "source_sha256": _source_sha256(),
        "aggregate": {
            "suites": len(results),
            "passed": passed,
            "pass_rate": round(passed / len(results), 4),
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
        "harness": {
            "offline": True,
            "model_calls": 0,
            "python": {"major": sys.version_info.major, "minor": sys.version_info.minor},
            "platform": platform.system(),
        },
        "results": results,
        "claims_boundary": {
            "measures": (
                "deterministic storage, retrieval, temporal state, provenance, "
                "formation, portability, and production-loop wiring"
            ),
            "does_not_measure": (
                "open-world model quality, automatic free-conversation extraction, "
                "or public coding-benchmark task success"
            ),
            "experimental_suites": [
                "control_experiment",
                "agent_intervention_experiment",
            ],
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_release_suite()
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    if args.json:
        print(payload)
    else:
        aggregate = report["aggregate"]
        print(
            f"memory release gate: {aggregate['passed']}/{aggregate['suites']} "
            f"suites passed; source={report['source_sha256'][:12]}"
        )
        for result in report["results"]:
            marker = "PASS" if result["passed"] else "FAIL"
            print(f"[{marker}] {result['name']}: {result['suite']}")
    return 0 if report["aggregate"]["passed"] == report["aggregate"]["suites"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
