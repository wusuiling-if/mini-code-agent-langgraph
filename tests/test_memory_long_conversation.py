from pathlib import Path

from evals.run_memory_long_conversation import run_diagnostic


def test_long_conversation_diagnostic_covers_reading_without_claiming_extraction(
    tmp_path: Path,
):
    report = run_diagnostic()

    assert report["scope"]["sessions"] == 120
    assert report["scope"]["cases"] == 10
    assert report["scope"]["embedding"] is False
    assert report["scope"]["explicit_authenticated_ingestion"] is True
    assert report["scope"]["automatic_conversation_extraction"] is False
    assert report["retrieval"]["correct"] == 10
    assert report["retrieval"]["accuracy"] == 1.0
    assert report["reader_results"] == []
    assert report["store_integrity"] is True
