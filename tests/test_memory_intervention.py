from evals import run_memory_intervention_model
from evals.run_memory_intervention import AdvisoryAwareEvalModel, run_intervention_eval


def test_memory_intervention_runs_through_production_agent_loop():
    report = run_intervention_eval()
    results = {item["system"]: item for item in report["results"]}

    assert report["acceptance"]["passed"] is True
    assert report["scope"]["production_agent_loop"] is True
    assert report["scope"]["real_fixture_tests"] is True
    assert report["scope"]["claims_model_quality"] is False
    assert results["no_memory"]["steps"] == 5
    assert results["static_retrieval"]["steps"] == 6
    assert results["static_retrieval"]["failed_tests_after_edit"] == 1
    assert results["controlled_memory"]["steps"] == 4
    assert results["controlled_memory"]["failed_tests_after_edit"] == 0
    assert results["controlled_memory"]["harmful_items"] == 0


def test_real_model_runner_sanitizes_report_and_uses_all_conditions(monkeypatch):
    monkeypatch.setattr(
        run_memory_intervention_model,
        "create_model",
        lambda *args, **kwargs: AdvisoryAwareEvalModel(),
    )

    report = run_memory_intervention_model.run_real_intervention(
        model_name="fixture-model",
        provider="openai",
    )

    assert report["aggregate"]["verified_successes"] == 3
    assert report["conditions"] == [
        "no_memory",
        "static_retrieval",
        "controlled_memory",
    ]
    assert report["scope"]["real_model"] is True
    assert report["scope"]["claims_generalization"] is False
    assert report["sanitization"]["responses_omitted"] is True
    assert "messages" not in str(report)
