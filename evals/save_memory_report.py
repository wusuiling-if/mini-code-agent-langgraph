"""生成或校验不含密钥和原始记忆正文的版本化记忆评测记录。"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.run_memory_comparison import run_comparison
from evals.run_memory_control import run_control_eval
from evals.run_memory_evals import run_suite
from evals.run_memory_formation import run_formation
from evals.run_memory_intervention import run_intervention_eval
from evals.run_memory_longitudinal import run_longitudinal
from evals.run_memory_long_conversation import run_diagnostic as run_long_conversation
from evals.run_memory_portability import run_portability

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 9
SOURCE_PATHS = (
    "evals/run_evals.py",
    "evals/fixtures/memory_complex/invoice.py",
    "evals/fixtures/memory_complex/pricing.py",
    "evals/fixtures/memory_complex/shipping.py",
    "evals/fixtures/memory_complex/test_checkout.py",
    "evals/fixtures/memory_natural_holdout/delivery.py",
    "evals/fixtures/memory_natural_holdout/discounts.py",
    "evals/fixtures/memory_natural_holdout/statement.py",
    "evals/fixtures/memory_natural_holdout/test_statement.py",
    "evals/run_memory_evals.py",
    "evals/run_memory_comparison.py",
    "evals/run_memory_longitudinal.py",
    "evals/run_memory_long_conversation.py",
    "evals/run_memory_formation.py",
    "evals/run_memory_model_comparison.py",
    "evals/run_memory_control.py",
    "evals/run_memory_intervention.py",
    "evals/run_memory_intervention_model.py",
    "evals/run_memory_complex_intervention_model.py",
    "evals/run_memory_natural_intervention_model.py",
    "evals/run_memory_portability.py",
    "evals/save_memory_report.py",
    "src/memory_core/__init__.py",
    "src/memory_core/contracts.py",
    "src/memory_core/experience.py",
    "src/memory_core/lifecycle.py",
    "src/memory_core/rendering.py",
    "src/memory_core/runtime.py",
    "src/memory_core/security.py",
    "src/memory_core/semantic.py",
    "src/mini_code_agent/checks.py",
    "src/mini_code_agent/agent.py",
    "src/mini_code_agent/receipt.py",
    "src/mini_code_agent/transaction.py",
    "src/mini_code_agent/transaction_cli.py",
    "src/mini_code_agent/memory_admission.py",
    "src/mini_code_agent/memory_control.py",
    "src/mini_code_agent/memory_models.py",
    "src/mini_code_agent/memory_store.py",
    "src/mini_code_agent/memory_retrieval.py",
    "src/mini_code_agent/memory_adapters/__init__.py",
    "src/mini_code_agent/memory_adapters/agent.py",
    "src/mini_code_agent/memory_adapters/project.py",
    "src/mini_code_agent/memory_adapters/semantic.py",
    "src/mini_code_agent/memory_adapters/transaction.py",
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_payload(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _source_fingerprints() -> dict[str, str]:
    return {
        relative: _sha256_bytes((REPOSITORY_ROOT / relative).read_bytes())
        for relative in SOURCE_PATHS
    }


def _core_summary(report: dict[str, Any]) -> dict[str, Any]:
    aggregate = dict(report["aggregate"])
    aggregate.pop("duration_ms", None)
    return {
        "suite": report["suite"],
        "aggregate": aggregate,
        "metrics": report["metrics"],
        "cases": [
            {
                "name": case["name"],
                "category": case["category"],
                "passed": case["passed"],
            }
            for case in report["cases"]
        ],
    }


def _portability_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "suite": report["suite"],
        "scope": report["scope"],
        "aggregate": report["aggregate"],
        "cases": report["cases"],
        "acceptance": report["acceptance"],
    }


def _long_conversation_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "suite": report["suite"],
        "scope": report["scope"],
        "retrieval": report["retrieval"],
        "store_integrity": report["store_integrity"],
        "claims_boundary": report["claims_boundary"],
    }


def _comparison_summary(report: dict[str, Any]) -> dict[str, Any]:
    systems = []
    for result in report["systems"]:
        systems.append(
            {
                "system": result["system"],
                "metrics": result["metrics"],
                "counts": result["counts"],
                "failed_cases": [
                    case["case"] for case in result["cases"] if not case["correct"]
                ],
                "harmful_cases": [
                    case["case"] for case in result["cases"] if case["harmful"]
                ],
            }
        )
    return {
        "suite": report["suite"],
        "scope": report["scope"],
        "acceptance": report["acceptance"],
        "systems": systems,
    }


def _longitudinal_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "suite": report["suite"],
        "scope": report["scope"],
        "store": report["store"],
        "acceptance": report["acceptance"],
        "systems": [
            {
                "system": result["system"],
                "metrics": result["metrics"],
                "counts": result["counts"],
                "failures": result["failures"],
            }
            for result in report["systems"]
        ],
    }


def _formation_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "suite": report["suite"],
        "scope": report["scope"],
        "aggregate": report["aggregate"],
        "metrics": report["metrics"],
        "cases": report["cases"],
        "acceptance": report["acceptance"],
    }


def _control_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "suite": report["suite"],
        "scope": report["scope"],
        "aggregate": report["aggregate"],
        "metrics": report["metrics"],
        "cases": report["cases"],
        "acceptance": report["acceptance"],
    }


def _intervention_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "suite": report["suite"],
        "scope": report["scope"],
        "results": report["results"],
        "checks": report["checks"],
        "acceptance": report["acceptance"],
    }


def _online_summary(path: Path) -> dict[str, Any]:
    raw_bytes = path.read_bytes()
    report = json.loads(raw_bytes)
    required = {"suite", "model", "provider", "cases", "model_calls", "results"}
    if not required.issubset(report):
        raise ValueError("online report is missing required fields")
    return {
        "source_report_sha256": _sha256_bytes(raw_bytes),
        "suite": report["suite"],
        "model": report["model"],
        "resolved_model": (
            "deepseek-v4-flash"
            if report["model"] == "deepseek-flash"
            else report["model"]
        ),
        "provider": report["provider"],
        "cases": report["cases"],
        "model_calls": report["model_calls"],
        "proposed_gain_vs_best_baseline": report["proposed_gain_vs_best_baseline"],
        "results": [
            {
                "system": result["system"],
                "answer_accuracy": result["answer_accuracy"],
                "correct": result["correct"],
                "failed_cases": [
                    case["case"] for case in result["cases"] if not case["correct"]
                ],
            }
            for result in report["results"]
        ],
        "sanitization": {
            "responses_omitted": True,
            "memory_values_omitted": True,
            "credentials_omitted": True,
        },
    }


def _online_intervention_summary(path: Path) -> dict[str, Any]:
    raw_bytes = path.read_bytes()
    report = json.loads(raw_bytes)
    required = {
        "suite",
        "model",
        "provider",
        "model_calls",
        "results",
        "aggregate",
        "scope",
        "sanitization",
    }
    if not required.issubset(report):
        raise ValueError("intervention report is missing required fields")
    sanitization = report["sanitization"]
    required_omissions = {
        "responses_omitted",
        "memory_values_omitted",
        "tool_outputs_omitted",
        "local_paths_omitted",
        "credentials_omitted",
    }
    if not isinstance(sanitization, dict) or not all(
        sanitization.get(name) is True for name in required_omissions
    ):
        raise ValueError("intervention report is not fully sanitized")
    allowed_result_fields = {
        "system",
        "operation",
        "injected_items",
        "harmful_items",
        "context_chars",
        "submitted",
        "verification_status",
        "correct_file",
        "steps",
        "model_calls",
        "tool_calls",
        "edit_attempts",
        "test_runs",
        "failed_tests_after_edit",
        "repeat",
        "read_calls",
        "changed_files",
        "expected_files_only",
        "verified_success",
    }
    summary = {
        "source_report_sha256": _sha256_bytes(raw_bytes),
        "suite": report["suite"],
        "model": report["model"],
        "provider": report["provider"],
        "conditions": report.get("conditions")
        or list(dict.fromkeys(result["system"] for result in report["results"])),
        "model_calls": report["model_calls"],
        "aggregate": report["aggregate"],
        "scope": report["scope"],
        "results": [
            {
                key: value
                for key, value in result.items()
                if key in allowed_result_fields
            }
            for result in report["results"]
        ],
        "sanitization": dict(sanitization),
    }
    for optional in (
        "repeats",
        "formation",
        "controller_feedback",
        "store_integrity",
    ):
        if optional in report:
            summary[optional] = report[optional]
    return summary


def _online_conversation_summary(path: Path) -> dict[str, Any]:
    raw_bytes = path.read_bytes()
    report = json.loads(raw_bytes)
    required = {
        "suite",
        "model",
        "provider",
        "model_calls",
        "scope",
        "retrieval",
        "reader_results",
        "store_integrity",
        "claims_boundary",
    }
    if not required.issubset(report):
        raise ValueError("long-conversation report is missing required fields")
    return {
        "source_report_sha256": _sha256_bytes(raw_bytes),
        "suite": report["suite"],
        "model": report["model"],
        "provider": report["provider"],
        "model_calls": report["model_calls"],
        "scope": report["scope"],
        "retrieval": report["retrieval"],
        "reader_results": [
            {
                "system": result["system"],
                "correct": result["correct"],
                "accuracy": result["accuracy"],
                "cases": [
                    {
                        "case": case["case"],
                        "ability": case["ability"],
                        "correct": case["correct"],
                        "context_chars": case["context_chars"],
                    }
                    for case in result["cases"]
                ],
            }
            for result in report["reader_results"]
        ],
        "store_integrity": report["store_integrity"],
        "claims_boundary": report["claims_boundary"],
        "sanitization": {
            "responses_omitted": True,
            "memory_values_omitted": True,
            "conversation_sessions_omitted": True,
            "local_paths_omitted": True,
            "credentials_omitted": True,
        },
    }


def build_record(
    *,
    recorded_at: str,
    online_report: Path | None,
    intervention_report: Path | None = None,
    regression_summary: str | None,
    conversation_report: Path | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": recorded_at,
        "environment": {
            "platform": platform.system(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        },
        "source_sha256": _source_fingerprints(),
        "regression_summary": regression_summary,
        "offline_core": _core_summary(run_suite()),
        "offline_comparison": _comparison_summary(run_comparison()),
        "offline_longitudinal": _longitudinal_summary(run_longitudinal()),
        "offline_long_conversation": _long_conversation_summary(
            run_long_conversation()
        ),
        "offline_formation": _formation_summary(run_formation()),
        "offline_portability": _portability_summary(run_portability()),
        "offline_control": _control_summary(run_control_eval()),
        "offline_intervention": _intervention_summary(run_intervention_eval()),
        "online_model": _online_summary(online_report) if online_report else None,
        "online_intervention": (
            _online_intervention_summary(intervention_report)
            if intervention_report
            else None
        ),
        "online_long_conversation": (
            _online_conversation_summary(conversation_report)
            if conversation_report
            else None
        ),
        "claims_boundary": {
            "controlled_repository_benchmark": True,
            "public_benchmark": False,
            "independent_holdout": False,
            "automatic_memory_formation_measured": True,
            "adaptive_memory_control_measured": True,
            "agent_loop_memory_intervention_measured": True,
            "free_text_memory_extraction_measured": False,
            "learned_policy_measured": False,
            "real_model_intervention_measured": intervention_report is not None,
            "long_conversation_reading_measured": True,
            "real_model_long_conversation_measured": conversation_report is not None,
            "host_neutral_core_measured": True,
        },
    }
    record["record_sha256"] = _sha256_bytes(_canonical_payload(record))
    return record


def verify_record(path: Path) -> tuple[bool, tuple[str, ...]]:
    record = json.loads(path.read_text(encoding="utf-8"))
    errors = []
    supplied = record.pop("record_sha256", None)
    expected = _sha256_bytes(_canonical_payload(record))
    if supplied != expected:
        errors.append("record SHA-256 mismatch")
    if record.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported record schema version")
    current_sources = _source_fingerprints()
    if record.get("source_sha256") != current_sources:
        errors.append("evaluation source fingerprints changed")
    return not errors, tuple(errors)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--online-report", type=Path)
    parser.add_argument("--intervention-report", type=Path)
    parser.add_argument("--conversation-report", type=Path)
    parser.add_argument("--regression-summary")
    parser.add_argument(
        "--recorded-at",
        default=datetime.now(timezone.utc).isoformat(),
        help="记录时间；保存可复现快照时建议显式传入。",
    )
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args(argv)
    if args.verify:
        ok, errors = verify_record(args.verify)
        print(f"ok: {str(ok).lower()}")
        for error in errors:
            print(f"error: {error}")
        return 0 if ok else 1
    if args.output is None:
        parser.error("--output is required unless --verify is used")
    record = build_record(
        recorded_at=args.recorded_at,
        online_report=args.online_report,
        intervention_report=args.intervention_report,
        regression_summary=args.regression_summary,
        conversation_report=args.conversation_report,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"saved: {args.output}")
    print(f"record_sha256: {record['record_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
