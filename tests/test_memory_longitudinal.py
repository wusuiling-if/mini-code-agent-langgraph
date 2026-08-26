from __future__ import annotations

import json

from evals.run_memory_longitudinal import main, run_longitudinal


def test_longitudinal_suite_measures_retention_updates_and_interference():
    report = run_longitudinal()
    systems = {result["system"]: result for result in report["systems"]}
    proposed = systems["evidence_temporal_hybrid"]["metrics"]

    assert report["suite"] == "memory-longitudinal-v1"
    assert report["scope"] == {
        "sessions": 120,
        "probes": 29,
        "offline": True,
        "automatic_memory_formation": False,
    }
    assert report["store"]["cards"] == 132
    assert report["acceptance"]["passed"] is True
    assert report["acceptance"]["gain_vs_best_baseline"] >= 0.1
    assert proposed["overall_accuracy"] == 1.0
    assert proposed["retention_accuracy"] == 1.0
    assert proposed["harmful_injection_rate"] == 0.0
    assert proposed["retention_by_age"] == {
        "short_0_2": 1.0,
        "medium_3_20": 1.0,
        "long_21_60": 1.0,
        "very_long_61_plus": 1.0,
    }


def test_longitudinal_json_is_machine_readable(capsys):
    assert main(["--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["acceptance"]["passed"] is True
