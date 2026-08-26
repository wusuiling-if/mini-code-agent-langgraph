from __future__ import annotations

import json
from pathlib import Path

from evals.save_memory_report import build_record, verify_record


def _online_report(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "suite": "memory-model-comparison-v1",
                "model": "deepseek-flash",
                "provider": "deepseek",
                "cases": 1,
                "model_calls": 4,
                "proposed_gain_vs_best_baseline": 0.25,
                "results": [
                    {
                        "system": "evidence_temporal_hybrid",
                        "answer_accuracy": 1.0,
                        "correct": 1,
                        "cases": [
                            {
                                "case": "private-case",
                                "correct": True,
                                "response": "SECRET RESPONSE MUST NOT PERSIST",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_saved_report_is_sanitized_hashed_and_bound_to_sources(tmp_path: Path):
    online = tmp_path / "online.json"
    _online_report(online)
    record = build_record(
        recorded_at="2026-08-17T00:00:00+08:00",
        online_report=online,
        regression_summary="357 passed, 2 skipped",
    )
    saved = tmp_path / "record.json"
    saved.write_text(json.dumps(record), encoding="utf-8")

    serialized = json.dumps(record)
    assert "SECRET RESPONSE" not in serialized
    assert record["online_model"]["sanitization"]["credentials_omitted"] is True
    assert record["claims_boundary"]["independent_holdout"] is False
    assert record["claims_boundary"]["automatic_memory_formation_measured"] is True
    assert record["claims_boundary"]["adaptive_memory_control_measured"] is True
    assert record["claims_boundary"]["agent_loop_memory_intervention_measured"] is True
    assert record["claims_boundary"]["free_text_memory_extraction_measured"] is False
    assert record["claims_boundary"]["learned_policy_measured"] is False
    assert record["claims_boundary"]["real_model_intervention_measured"] is False
    assert record["offline_formation"]["acceptance"]["passed"] is True
    assert record["offline_portability"]["acceptance"]["passed"] is True
    assert record["offline_long_conversation"]["retrieval"]["accuracy"] == 1.0
    assert record["claims_boundary"]["long_conversation_reading_measured"] is True
    assert record["claims_boundary"]["real_model_long_conversation_measured"] is False
    assert record["claims_boundary"]["host_neutral_core_measured"] is True
    assert record["offline_control"]["acceptance"]["passed"] is True
    assert record["offline_control"]["metrics"]["static_harmful_candidates"] == 1
    assert record["offline_control"]["metrics"]["controlled_harmful_candidates"] == 0
    assert record["offline_intervention"]["acceptance"]["passed"] is True
    intervention = {
        item["system"]: item for item in record["offline_intervention"]["results"]
    }
    assert intervention["no_memory"]["steps"] == 5
    assert intervention["static_retrieval"]["steps"] == 6
    assert intervention["controlled_memory"]["steps"] == 4
    assert len(record["record_sha256"]) == 64
    assert verify_record(saved) == (True, ())

    record["regression_summary"] = "tampered"
    saved.write_text(json.dumps(record), encoding="utf-8")
    ok, errors = verify_record(saved)
    assert ok is False
    assert "record SHA-256 mismatch" in errors


def test_real_intervention_report_is_sanitized_and_bound(tmp_path: Path):
    intervention = tmp_path / "intervention.json"
    intervention.write_text(
        json.dumps(
            {
                "suite": "memory-agent-intervention-v0-real-model",
                "model": "fixture-model",
                "provider": "deepseek",
                "conditions": ["controlled_memory"],
                "model_calls": 4,
                "results": [
                    {
                        "system": "controlled_memory",
                        "submitted": True,
                        "verification_status": "passed",
                        "correct_file": True,
                        "steps": 4,
                        "model_calls": 4,
                        "secret_extra_field": "MUST NOT PERSIST",
                    }
                ],
                "aggregate": {"runs": 1, "verified_successes": 1},
                "scope": {"real_model": True, "claims_generalization": False},
                "sanitization": {
                    "responses_omitted": True,
                    "memory_values_omitted": True,
                    "tool_outputs_omitted": True,
                    "local_paths_omitted": True,
                    "credentials_omitted": True,
                },
            }
        ),
        encoding="utf-8",
    )

    record = build_record(
        recorded_at="2026-08-18T00:00:00+08:00",
        online_report=None,
        intervention_report=intervention,
        regression_summary="fixture",
    )

    serialized = json.dumps(record)
    assert "MUST NOT PERSIST" not in serialized
    assert record["claims_boundary"]["real_model_intervention_measured"] is True
    assert record["online_intervention"]["aggregate"]["verified_successes"] == 1
    assert len(record["online_intervention"]["source_report_sha256"]) == 64
