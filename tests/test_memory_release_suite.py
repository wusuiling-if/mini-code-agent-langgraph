from __future__ import annotations

import json

from evals.run_memory_suite import main, run_release_suite


def test_memory_release_suite_runs_all_deterministic_gates() -> None:
    report = run_release_suite()

    assert report["suite"] == "memory-release-v0.6.0"
    assert report["aggregate"]["suites"] == 9
    assert report["aggregate"]["passed"] == 9
    assert report["harness"]["offline"] is True
    assert report["harness"]["model_calls"] == 0
    assert len(report["source_sha256"]) == 64
    assert {result["name"] for result in report["results"]} == {
        "core",
        "conversation_production",
        "architecture_comparison",
        "formation",
        "portability",
        "longitudinal",
        "long_conversation",
        "control_experiment",
        "agent_intervention_experiment",
    }
    assert all(result["passed"] for result in report["results"])


def test_memory_release_suite_json_output_is_machine_readable(capsys) -> None:
    assert main(["--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["aggregate"]["pass_rate"] == 1.0
    assert report["claims_boundary"]["experimental_suites"] == [
        "control_experiment",
        "agent_intervention_experiment",
    ]
