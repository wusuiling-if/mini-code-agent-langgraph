from __future__ import annotations

from evals.run_memory_control import run_control_eval


def test_memory_control_eval_exercises_feedback_countermemory_and_shadow():
    report = run_control_eval()

    assert report["suite"] == "memory-control-v0"
    assert report["aggregate"] == {
        "cases": 6,
        "passed": 6,
        "pass_rate": 1.0,
    }
    assert report["metrics"] == {
        "static_harmful_candidates": 1,
        "controlled_harmful_candidates": 0,
        "store_integrity": True,
    }
    assert report["acceptance"]["passed"] is True
