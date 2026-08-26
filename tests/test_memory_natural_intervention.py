from pathlib import Path

from evals import run_memory_natural_intervention_model as natural_eval
from mini_code_agent.memory_store import SQLiteMemoryStore


def test_verified_diff_and_experience_are_formed_without_written_memory(tmp_path: Path):
    original = tmp_path / "original"
    repaired = tmp_path / "repaired"
    original.mkdir()
    repaired.mkdir()
    (original / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repaired / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    diff = natural_eval._verified_diff(
        original,
        repaired,
        frozenset({"module.py"}),
    )
    store = SQLiteMemoryStore(tmp_path / "memory")
    result = {
        "verified_success": True,
        "outcome_sha256": "a" * 64,
        "changed_files": ["module.py"],
        "verification_status": "passed",
    }

    card = natural_eval._form_experience(
        store,
        task="Fix the settlement regression.",
        diff=diff,
        training_result=result,
    )

    assert "-VALUE = 1" in card.value
    assert "+VALUE = 2" in card.value
    assert card.cue_anchors == ("settlement", "regression")
    assert store.sources(card.id)[0].source_type == "verified_agent_trajectory"
    assert store.verify().ok is True


def test_unverified_training_run_cannot_form_experience(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "memory")
    try:
        natural_eval._form_experience(
            store,
            task="failed task",
            diff="--- before\n+++ after\n",
            training_result={"verified_success": False},
        )
    except ValueError as exc:
        assert "unverified" in str(exc)
    else:
        raise AssertionError("unverified experience was accepted")
