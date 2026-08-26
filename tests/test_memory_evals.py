from __future__ import annotations

import json

from evals.run_memory_evals import main, run_suite


def test_memory_eval_covers_retrieval_temporal_provenance_and_security():
    report = run_suite()

    assert report["suite"] == "memory-core-v0.1"
    assert report["aggregate"]["gated_passed"] == report["aggregate"]["gated_cases"]
    assert report["metrics"]["lexical_top1"] == {
        "correct": 6,
        "total": 6,
        "rate": 1.0,
    }
    assert report["metrics"]["irrelevant_query_abstention"]["rate"] == 1.0
    assert report["metrics"]["semantic_paraphrase_without_matching_cue"]["rate"] == 0.0
    categories = {case["category"] for case in report["cases"]}
    assert categories == {
        "abstention",
        "authority",
        "fallback",
        "integrity",
        "known_limit",
        "lexical_retrieval",
        "provenance",
        "temporal",
    }


def test_memory_eval_json_output_is_machine_readable(capsys):
    assert main(["--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["aggregate"]["pass_rate"] == 1.0
    assert report["scope"]["offline"] is True
