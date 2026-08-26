from __future__ import annotations

import json

from evals.run_memory_portability import main, run_portability


def test_portability_eval_runs_without_mca_host_contracts():
    report = run_portability()

    assert report["suite"] == "memory-core-portability-v1"
    assert report["aggregate"] == {"cases": 5, "passed": 5, "pass_rate": 1.0}
    assert report["scope"]["mca_transaction_required"] is False
    assert report["scope"]["mca_agent_required"] is False
    assert report["acceptance"]["passed"] is True


def test_portability_eval_json(capsys):
    assert main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["aggregate"]["passed"] == 5
