from __future__ import annotations

import json

from evals.run_memory_comparison import main, run_comparison


def test_comparison_uses_shared_cross_domain_corpus_and_beats_baselines():
    report = run_comparison()
    systems = {result["system"]: result for result in report["systems"]}
    proposed = systems["evidence_temporal_hybrid"]["metrics"]

    assert report["suite"] == "memory-architecture-comparison-v1"
    assert report["scope"]["offline"] is True
    assert report["scope"]["shared_corpus"] is True
    assert report["scope"]["model_calls"] == 0
    assert set(report["scope"]["domains"]) == {
        "coding",
        "customer_service",
        "generic",
        "personal_assistant",
        "research",
    }
    assert set(systems) == {
        "no_memory",
        "pure_recall",
        "traditional_three_layer",
        "evidence_temporal_hybrid",
    }
    assert report["acceptance"]["passed"] is True
    assert report["acceptance"]["decision_accuracy_gain_vs_best_baseline"] >= 0.15
    assert proposed["harmful_injection_rate"] <= 0.05
    assert proposed["expected_abstention_recall"] >= 0.8


def test_comparison_json_is_machine_readable(capsys):
    assert main(["--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["acceptance"]["passed"] is True
