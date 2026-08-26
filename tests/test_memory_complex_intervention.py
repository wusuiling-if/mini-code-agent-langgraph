from evals import run_memory_complex_intervention_model as complex_eval


def test_complex_intervention_aggregates_balanced_runs(monkeypatch):
    counter = {"value": 0}

    def fake_run_condition(**kwargs):
        counter["value"] += 1
        intervention = kwargs["intervention"]
        return {
            "repeat": kwargs["repeat"],
            "system": intervention.name,
            "operation": intervention.operation,
            "injected_items": intervention.injected_items,
            "harmful_items": intervention.harmful_items,
            "context_chars": len(intervention.context),
            "submitted": True,
            "verification_status": "passed",
            "expected_files_only": True,
            "changed_files": sorted(complex_eval.EXPECTED_FILES),
            "steps": 8,
            "model_calls": 8,
            "tool_calls": 10,
            "read_calls": 3,
            "edit_attempts": 3,
            "test_runs": 2,
            "failed_tests_after_edit": 0,
            "verified_success": True,
        }

    monkeypatch.setattr(complex_eval, "create_model", lambda *args, **kwargs: object())
    monkeypatch.setattr(complex_eval, "_run_condition", fake_run_condition)

    report = complex_eval.run_complex_intervention(
        model_name="fixture-model",
        provider="openai",
        repeats=3,
    )

    assert counter["value"] == 9
    assert report["aggregate"]["runs"] == 9
    assert report["aggregate"]["verified_successes"] == 9
    assert [item["verified_successes"] for item in report["aggregate"]["systems"]] == [
        3,
        3,
        3,
    ]
    assert report["scope"]["balanced_condition_order"] is True
    assert report["scope"]["claims_generalization"] is False


def test_complex_intervention_supports_single_condition(monkeypatch):
    monkeypatch.setattr(complex_eval, "create_model", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        complex_eval,
        "_run_condition",
        lambda **kwargs: {
            "system": kwargs["intervention"].name,
            "steps": 5,
            "model_calls": 5,
            "tool_calls": 6,
            "read_calls": 2,
            "edit_attempts": 3,
            "failed_tests_after_edit": 0,
            "verified_success": True,
        },
    )

    report = complex_eval.run_complex_intervention(
        model_name="fixture-model",
        conditions=("controlled_memory",),
    )

    assert report["aggregate"]["runs"] == 1
    assert report["aggregate"]["systems"] == [
        {
            "system": "controlled_memory",
            "runs": 1,
            "verified_successes": 1,
            "mean_steps": 5.0,
            "mean_tool_calls": 6.0,
            "mean_read_calls": 2.0,
            "mean_edit_attempts": 3.0,
            "failed_tests_after_edit": 0,
        }
    ]
