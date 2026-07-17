from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import mini_code_agent.workspace as workspace_module
from mini_code_agent.security import SafeWorkspace
from mini_code_agent.verification import capture_workspace_fingerprint
from mini_code_agent.workspace import WorkspaceSnapshot


def test_snapshot_fingerprint_matches_legacy_digest(tmp_path: Path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    snapshot = WorkspaceSnapshot.capture(tmp_path, cache={})
    payload = json.dumps(
        snapshot.files, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    assert snapshot.fingerprint == hashlib.sha256(payload.encode()).hexdigest()


def test_lazy_fingerprint_cache_does_not_change_snapshot_equality(tmp_path: Path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    snapshot = WorkspaceSnapshot.capture(tmp_path, cache={})
    equivalent = WorkspaceSnapshot(root=snapshot.root, files=dict(snapshot.files))

    snapshot.fingerprint

    assert snapshot == equivalent


def test_capture_does_not_resolve_every_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    for index in range(40):
        path = tmp_path / "pkg" / f"file-{index}.txt"
        path.parent.mkdir(exist_ok=True)
        path.write_text(str(index), encoding="utf-8")
    calls = 0
    original = SafeWorkspace.resolve

    def counted(self, path):
        nonlocal calls
        calls += 1
        return original(self, path)

    monkeypatch.setattr(SafeWorkspace, "resolve", counted)
    WorkspaceSnapshot.capture(tmp_path, cache={})
    assert calls <= 4


def test_warm_cache_returns_the_same_fingerprint(tmp_path: Path):
    (tmp_path / "source.py").write_text("print('hello')\n", encoding="utf-8")
    fingerprinter = workspace_module.WorkspaceFingerprinter(tmp_path)

    cold = fingerprinter.capture()
    warm = fingerprinter.capture()

    assert warm.fingerprint == cold.fingerprint
    assert warm.files == cold.files


def test_changing_one_file_changes_the_fingerprint(tmp_path: Path):
    source = tmp_path / "source.py"
    source.write_text("before\n", encoding="utf-8")
    fingerprinter = workspace_module.WorkspaceFingerprinter(tmp_path)
    before = fingerprinter.capture().fingerprint

    source.write_text("after with a different size\n", encoding="utf-8")

    assert fingerprinter.capture().fingerprint != before


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are required")
def test_directory_modes_remain_covered(tmp_path: Path):
    package = tmp_path / "pkg"
    package.mkdir()
    fingerprinter = workspace_module.WorkspaceFingerprinter(tmp_path)
    before = fingerprinter.capture().fingerprint
    original_mode = stat.S_IMODE(package.stat().st_mode)

    package.chmod(original_mode ^ stat.S_IXUSR)

    assert fingerprinter.capture().fingerprint != before


def test_symlink_targets_remain_covered(tmp_path: Path):
    (tmp_path / "target-a").write_text("same", encoding="utf-8")
    (tmp_path / "target-b").write_text("same", encoding="utf-8")
    link = tmp_path / "current"
    try:
        link.symlink_to("target-a")
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    fingerprinter = workspace_module.WorkspaceFingerprinter(tmp_path)
    before = fingerprinter.capture().fingerprint

    link.unlink()
    link.symlink_to("target-b")

    assert fingerprinter.capture().fingerprint != before


def test_ignored_artifacts_remain_excluded(tmp_path: Path):
    artifact = tmp_path / "artifacts" / "run.json"
    artifact.parent.mkdir()
    artifact.write_text('{"state":"before"}\n', encoding="utf-8")
    (tmp_path / "source.py").write_text("unchanged\n", encoding="utf-8")
    fingerprinter = workspace_module.WorkspaceFingerprinter(tmp_path)
    before = fingerprinter.capture(ignore_paths={artifact})

    artifact.write_text('{"state":"after and larger"}\n', encoding="utf-8")
    after = fingerprinter.capture(ignore_paths={artifact})

    assert "artifacts" not in after.files
    assert "artifacts/run.json" not in after.files
    assert after.fingerprint == before.fingerprint


def test_dependency_directories_are_not_skipped(tmp_path: Path):
    dependency_files = [
        tmp_path / ".venv" / "site-packages" / "installed.py",
        tmp_path / "node_modules" / "dependency" / "index.js",
    ]
    for path in dependency_files:
        path.parent.mkdir(parents=True)
        path.write_text("dependency content\n", encoding="utf-8")

    snapshot = WorkspaceSnapshot.capture(tmp_path, cache={})

    assert ".venv/site-packages/installed.py" in snapshot.files
    assert "node_modules/dependency/index.js" in snapshot.files


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative scanner is POSIX-only")
def test_scanner_failure_propagates_and_closes_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "nested").mkdir()
    fingerprinter = workspace_module.WorkspaceFingerprinter(tmp_path)
    if not workspace_module._descriptor_relative_scanning_available():
        pytest.skip("descriptor-relative operations are unavailable")

    original_open = os.open
    original_close = os.close
    original_scandir = os.scandir
    opened: set[int] = set()
    scans = 0

    def tracked_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        opened.add(descriptor)
        return descriptor

    def tracked_close(descriptor):
        opened.discard(descriptor)
        return original_close(descriptor)

    def failing_scandir(path):
        nonlocal scans
        scans += 1
        if scans == 2:
            raise OSError("forced scanner failure")
        return original_scandir(path)

    monkeypatch.setattr(workspace_module.os, "open", tracked_open)
    monkeypatch.setattr(workspace_module.os, "close", tracked_close)
    monkeypatch.setattr(workspace_module.os, "scandir", failing_scandir)

    with pytest.raises(OSError, match="forced scanner failure"):
        fingerprinter.capture()

    assert opened == set()


def test_capture_adapter_prefers_snapshot_fingerprint(tmp_path: Path):
    snapshot = SimpleNamespace(fingerprint="native-fingerprint", files={"a": "b"})
    executor = SimpleNamespace(
        cwd=tmp_path, workspace_fingerprint=lambda **_kwargs: snapshot
    )

    assert capture_workspace_fingerprint(executor) == "native-fingerprint"


def test_capture_adapter_keeps_files_fallback(tmp_path: Path):
    files = {"a": "b"}
    executor = SimpleNamespace(
        cwd=tmp_path,
        workspace_fingerprint=lambda **_kwargs: SimpleNamespace(files=files),
    )
    payload = json.dumps(files, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    assert capture_workspace_fingerprint(executor) == hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()
