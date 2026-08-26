from evals.run_sillytavern_memory_eval import run_eval


def test_sillytavern_portable_pipeline_scripted_end_to_end():
    report = run_eval(model_name="scripted")

    assert report["passed"] is True
    assert report["scope"] == {
        "raw_messages": 22,
        "imported_checkpoints": 3,
        "formation_batches": 4,
        "protected_recent_messages": 2,
        "embedding": False,
        "shadow_only": True,
    }
    assert all(report["outcomes"]["expected_active"].values())
    assert report["outcomes"]["hotel_forgotten"] is True
    assert report["outcomes"]["assistant_passport_rejected"] is True
    assert report["outcomes"]["hallucinated_summary_rejected"] is True
    assert report["outcomes"]["store_integrity"] is True
