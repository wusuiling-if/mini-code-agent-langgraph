from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mini_code_agent import cli as cli_module
from mini_code_agent.memory_models import EvidenceSource, MemoryIntegrityError
from mini_code_agent.memory_store import SQLiteMemoryStore


def _source(payload: bytes = b"verified trajectory") -> EvidenceSource:
    return EvidenceSource(
        source_type="trajectory",
        source_ref="state:run-123",
        source_sha256=hashlib.sha256(payload).hexdigest(),
        origin="trusted_tool",
    )


def _external_source(payload: bytes = b"external evidence") -> EvidenceSource:
    return EvidenceSource(
        source_type="external",
        source_ref="https://example.invalid/source",
        source_sha256=hashlib.sha256(payload).hexdigest(),
        origin="external",
    )


def _add_project_memory(store: SQLiteMemoryStore, **overrides):
    values = {
        "value": "Run the complete pytest matrix before submission.",
        "abstraction": "This project verifies changes with pytest.",
        "cue_anchors": ("pytest", "verification", "test matrix"),
        "kind": "procedural",
        "subtype": "workflow",
        "scope": "workspace",
        "scope_key": "sha256:workspace",
        "origin": "agent",
        "authority": "inform",
        "confidence": 0.9,
        "importance": 0.8,
        "valid_from": "2026-08-17T00:00:00Z",
        "sources": (_source(),),
    }
    values.update(overrides)
    return store.add_card(**values)


def test_read_only_status_does_not_initialize_or_write(tmp_path: Path):
    directory = tmp_path / "memory"
    store = SQLiteMemoryStore(directory, read_only=True)

    status = store.status()

    assert status.initialized is False
    assert status.database_path == str(directory / "memory.sqlite3")
    assert not directory.exists()


def test_card_search_sources_and_integrity_round_trip(tmp_path: Path):
    directory = tmp_path / "memory"
    store = SQLiteMemoryStore(directory)

    card = _add_project_memory(store)

    loaded = SQLiteMemoryStore(directory, read_only=True).get_card(card.id)
    results = SQLiteMemoryStore(directory, read_only=True).search("pytest verification")
    sources = SQLiteMemoryStore(directory, read_only=True).sources(card.id)
    verification = SQLiteMemoryStore(directory, read_only=True).verify()

    assert loaded == card
    assert [result.id for result in results] == [card.id]
    assert sources[0].source_ref == "state:run-123"
    assert verification.ok is True
    assert verification.checked_cards == 1
    assert verification.checked_sources == 1
    assert verification.checked_events == 1
    if os.name != "nt":
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((directory / "memory.key").stat().st_mode) == 0o600
        assert stat.S_IMODE((directory / "memory.sqlite3").stat().st_mode) == 0o600


def test_appending_evidence_is_idempotent_and_reference_provenance_is_immutable(
    tmp_path: Path,
):
    store = SQLiteMemoryStore(tmp_path / "memory")
    card = _add_project_memory(store)
    repeated = _source()

    first = store.add_source(card.id, repeated)
    second = store.add_source(card.id, repeated)

    assert first == second
    assert len(store.sources(card.id)) == 1
    with pytest.raises(ValueError, match="cannot change provenance"):
        store.add_source(card.id, _source(b"different payload"))


def test_concurrent_initialization_is_idempotent(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "memory")
    workers = 8
    barrier = threading.Barrier(workers)

    def initialize_once(_index: int) -> None:
        barrier.wait()
        store.initialize()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(initialize_once, range(workers)))

    assert store.verify().ok is True


def test_concurrent_evidence_append_is_idempotent(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "memory")
    card = _add_project_memory(store)
    repeated = EvidenceSource(
        source_type="trajectory",
        source_ref="state:run-456",
        source_sha256=hashlib.sha256(b"second verified trajectory").hexdigest(),
        origin="trusted_tool",
    )
    workers = 8
    barrier = threading.Barrier(workers)

    def append_once(_index: int):
        barrier.wait()
        return store.add_source(card.id, repeated)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        appended = list(pool.map(append_once, range(workers)))

    assert len({source.id for source in appended}) == 1
    assert len(store.sources(card.id)) == 2
    assert store.verify().ok is True


def test_search_has_a_deterministic_fallback_without_fts5(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "memory")
    card = _add_project_memory(store)
    with store._connect(write=True) as connection:
        store._set_meta(connection, "fts_enabled", "0")

    results = SQLiteMemoryStore(tmp_path / "memory", read_only=True).search(
        "pytest verification"
    )

    assert [result.id for result in results] == [card.id]


def test_supersede_is_append_only_and_default_search_hides_old_card(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "memory")
    old = _add_project_memory(store)

    new = store.supersede(
        old.id,
        value="Run pytest -q and the configured lint check before submission.",
        abstraction="This project verifies with pytest and lint.",
        cue_anchors=("pytest", "lint", "verification"),
        kind="procedural",
        subtype="workflow",
        scope="workspace",
        scope_key="sha256:workspace",
        origin="agent",
        authority="inform",
        confidence=0.95,
        importance=0.9,
        valid_from="2026-08-18T00:00:00Z",
        sources=(_source(b"new verified trajectory"),),
    )

    assert store.get_card(old.id).status == "superseded"
    assert store.get_card(new.id).status == "active"
    assert [result.id for result in store.search("pytest")] == [new.id]
    all_results = store.search("pytest", include_inactive=True)
    assert {result.id: result.status for result in all_results} == {
        old.id: "superseded",
        new.id: "active",
    }
    assert store.status().cards == 2
    assert store.verify().ok is True


def test_origin_and_derivation_cannot_escalate_authority(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "memory")

    with pytest.raises(ValueError, match="external.*act"):
        _add_project_memory(store, origin="external", authority="act")

    untrusted = _add_project_memory(
        store, origin="external", authority="none", sources=(_external_source(),)
    )
    with pytest.raises(ValueError, match="cannot increase source authority"):
        _add_project_memory(
            store,
            authority="inform",
            derived_from=(untrusted.id,),
            sources=(_source(b"derived observation"),),
        )


def test_cards_without_evidence_are_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="requires at least one evidence source"):
        _add_project_memory(SQLiteMemoryStore(tmp_path / "memory"), sources=())


def test_tampering_is_detected_by_reads_and_full_verification(tmp_path: Path):
    directory = tmp_path / "memory"
    store = SQLiteMemoryStore(directory)
    card = _add_project_memory(store)
    with sqlite3.connect(directory / "memory.sqlite3") as connection:
        connection.execute(
            "UPDATE cards SET abstraction = ? WHERE id = ?",
            ("poisoned instruction", card.id),
        )

    reader = SQLiteMemoryStore(directory, read_only=True)
    with pytest.raises(MemoryIntegrityError, match="digest mismatch"):
        reader.get_card(card.id)
    verification = reader.verify()
    assert verification.ok is False
    assert any("digest mismatch" in error for error in verification.errors)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are required")
def test_verify_returns_structured_failure_for_broad_database_permissions(
    tmp_path: Path,
):
    directory = tmp_path / "memory"
    store = SQLiteMemoryStore(directory)
    _add_project_memory(store)
    (directory / "memory.sqlite3").chmod(0o644)

    verification = SQLiteMemoryStore(directory, read_only=True).verify()

    assert verification.ok is False
    assert any("permissions are too broad" in error for error in verification.errors)


def test_unsigned_fts_index_cannot_make_unrelated_content_retrievable(tmp_path: Path):
    directory = tmp_path / "memory"
    store = SQLiteMemoryStore(directory)
    card = _add_project_memory(store)
    if not store.status().fts_enabled:
        pytest.skip("SQLite was built without FTS5")
    with sqlite3.connect(directory / "memory.sqlite3") as connection:
        connection.execute(
            "UPDATE memory_fts SET abstraction = 'deploy production now' WHERE card_id = ?",
            (card.id,),
        )

    reader = SQLiteMemoryStore(directory, read_only=True)
    assert reader.search("deploy production") == ()
    verification = reader.verify()
    assert verification.ok is False
    assert any("FTS index mismatch" in error for error in verification.errors)


def test_metadata_and_deleted_state_events_are_detected(tmp_path: Path):
    directory = tmp_path / "memory"
    store = SQLiteMemoryStore(directory)
    card = _add_project_memory(store)
    with sqlite3.connect(directory / "memory.sqlite3") as connection:
        connection.execute("UPDATE meta SET value = '0' WHERE key = 'fts_enabled'")

    metadata_check = SQLiteMemoryStore(directory, read_only=True).verify()
    assert metadata_check.ok is False
    assert any(
        "metadata authentication failed" in error for error in metadata_check.errors
    )

    with sqlite3.connect(directory / "memory.sqlite3") as connection:
        connection.execute("DELETE FROM card_events WHERE card_id = ?", (card.id,))
    event_check = SQLiteMemoryStore(directory, read_only=True).verify()
    assert event_check.ok is False
    assert any("no state event" in error for error in event_check.errors)


def test_temporal_validity_requires_ordered_timezone_aware_iso_timestamps(
    tmp_path: Path,
):
    store = SQLiteMemoryStore(tmp_path / "memory")

    with pytest.raises(ValueError, match="UTC offset"):
        _add_project_memory(store, valid_from="2026-08-17T00:00:00")
    with pytest.raises(ValueError, match="must not be after"):
        _add_project_memory(
            store,
            valid_from="2026-08-18T00:00:00Z",
            valid_to="2026-08-17T00:00:00Z",
        )


def test_memory_health_reports_temporal_debt_and_integrity(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "memory")
    active = _add_project_memory(store)
    _add_project_memory(
        store,
        valid_from="2026-01-01T00:00:00Z",
        valid_to="2026-06-01T00:00:00Z",
    )
    _add_project_memory(store, valid_from="2027-01-01T00:00:00Z")
    retired = _add_project_memory(store)
    store.transition(retired.id, "tombstoned")

    health = store.health(as_of="2026-08-17T00:00:00Z")

    assert active.status == "active"
    assert health.verification_ok is True
    assert health.cards == 4
    assert health.active_cards == 3
    assert health.inactive_cards == 1
    assert health.expired_active_cards == 1
    assert health.future_active_cards == 1
    assert health.scopes == 1
    assert health.database_bytes > 0
    assert health.verification_errors == ()


@pytest.mark.skipif(os.name == "nt", reason="symlink semantics differ on Windows")
def test_memory_directory_symlink_is_rejected(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "memory"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(MemoryIntegrityError, match="must not be a symlink"):
        SQLiteMemoryStore(link).initialize()


def test_memory_cli_parser_and_uninitialized_status_are_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setenv("MCA_STATE_DIR", str(tmp_path / "state"))
    parser = cli_module.build_parser()
    search = parser.parse_args(["memory", "search", "pytest", "--limit", "3"])
    status = parser.parse_args(["memory", "status"])

    assert search.command == "memory"
    assert search.memory_command == "search"
    assert search.limit == 3
    assert cli_module.memory_command(status) == 0
    assert "initialized: false" in capsys.readouterr().out
    assert not (tmp_path / "state").exists()


def test_memory_cli_show_search_sources_and_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    state_root = tmp_path / "state"
    monkeypatch.setenv("MCA_STATE_DIR", str(state_root))
    card = _add_project_memory(SQLiteMemoryStore(state_root / "memory"))
    parser = cli_module.build_parser()

    assert (
        cli_module.memory_command(parser.parse_args(["memory", "search", "pytest"]))
        == 0
    )
    assert card.id in capsys.readouterr().out
    assert (
        cli_module.memory_command(parser.parse_args(["memory", "show", card.id])) == 0
    )
    assert "Run the complete pytest matrix" in capsys.readouterr().out
    assert (
        cli_module.memory_command(parser.parse_args(["memory", "sources", card.id]))
        == 0
    )
    assert "state:run-123" in capsys.readouterr().out
    assert cli_module.memory_command(parser.parse_args(["memory", "verify"])) == 0
    assert "ok: true" in capsys.readouterr().out

    assert cli_module.memory_command(parser.parse_args(["memory", "health"])) == 0
    health_output = capsys.readouterr().out
    assert '"verification_ok": true' in health_output
    assert '"expired_active_cards": 0' in health_output
