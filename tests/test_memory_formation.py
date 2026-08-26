from __future__ import annotations

import json

from evals.run_memory_formation import main, run_formation


def test_formation_suite_measures_lifecycle_idempotency_and_scope():
    report = run_formation()

    assert report["suite"] == "memory-formation-v2"
    assert report["aggregate"] == {
        "cases": 10,
        "passed": 10,
        "pass_rate": 1.0,
    }
    assert report["scope"] == {
        "offline": True,
        "deterministic": True,
        "model_calls": 0,
        "free_text_extraction": False,
    }
    assert report["metrics"]["duplicate_card_rate_same_workflow"] == 0.0
    assert report["metrics"]["evidence_sources_on_stable_workflow"] == 2
    assert report["metrics"]["store_integrity"] is True
    assert report["acceptance"]["passed"] is True


def test_formation_json_is_machine_readable(capsys):
    assert main(["--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["aggregate"]["pass_rate"] == 1.0
