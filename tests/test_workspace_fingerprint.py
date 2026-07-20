from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import asdict, fields
from pathlib import Path
from types import SimpleNamespace

import pytest

import mini_code_agent.workspace as workspace_module
from mini_code_agent.security import SafeWorkspace, SecurityError
from mini_code_agent.verification import capture_workspace_fingerprint
from mini_code_agent.workspace import WorkspaceSnapshot


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def _create_linked_worktree(tmp_path: Path) -> tuple[Path, Path, Path]:
    repository = tmp_path / "repository"
    worktree = tmp_path / "linked-worktree"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "tests@example.invalid")
    _git(repository, "config", "user.name", "Test User")
    (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", "initial")
    _git(repository, "worktree", "add", "-b", "linked-test", str(worktree))
    git_dir = Path(_git(worktree, "rev-parse", "--absolute-git-dir"))
    return repository, worktree, git_dir


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


def test_fingerprint_cache_is_not_a_dataclass_field(tmp_path: Path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    snapshot = WorkspaceSnapshot.capture(tmp_path, cache={})

    snapshot.fingerprint

    assert [item.name for item in fields(snapshot)] == ["root", "files"]
    assert asdict(snapshot) == {"root": snapshot.root, "files": snapshot.files}


def test_snapshot_subclass_capture_returns_the_subclass(tmp_path: Path):
    class CustomWorkspaceSnapshot(WorkspaceSnapshot):
        pass

    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")

    snapshot = CustomWorkspaceSnapshot.capture(tmp_path, cache={})

    assert type(snapshot) is CustomWorkspaceSnapshot


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


@pytest.mark.parametrize("use_descriptor", [True, False], ids=["descriptor", "fallback"])
def test_warm_cache_skips_unchanged_hashes_and_rehashes_only_changed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_descriptor: bool
):
    source = tmp_path / "source.py"
    unchanged = tmp_path / "unchanged.py"
    source.write_text("print('hello')\n", encoding="utf-8")
    unchanged.write_text("constant\n", encoding="utf-8")
    fingerprinter = workspace_module.WorkspaceFingerprinter(tmp_path)
    if use_descriptor and not workspace_module._descriptor_relative_scanning_available():
        pytest.skip("descriptor-relative operations are unavailable")
    fingerprinter._use_descriptor_relative_scanner = use_descriptor
    hashed_inodes: list[int] = []

    if use_descriptor:
        original_descriptor_hash = workspace_module._hash_file_descriptor

        def counted_descriptor_hash(file_fd: int) -> str:
            hashed_inodes.append(os.fstat(file_fd).st_ino)
            return original_descriptor_hash(file_fd)

        monkeypatch.setattr(
            workspace_module, "_hash_file_descriptor", counted_descriptor_hash
        )
    else:
        original_file_hash = workspace_module._hash_file

        def counted_file_hash(path: Path, workspace=None) -> str:
            hashed_inodes.append(path.stat().st_ino)
            return original_file_hash(path, workspace)

        monkeypatch.setattr(workspace_module, "_hash_file", counted_file_hash)

    cold = fingerprinter.capture()
    cold_hashes = list(hashed_inodes)
    warm = fingerprinter.capture()

    assert warm.fingerprint == cold.fingerprint
    assert warm.files == cold.files
    assert set(cold_hashes) == {source.stat().st_ino, unchanged.stat().st_ino}
    assert hashed_inodes == cold_hashes

    source.write_text("print('changed and larger')\n", encoding="utf-8")
    fingerprinter.capture()

    assert hashed_inodes[len(cold_hashes) :] == [source.stat().st_ino]


@pytest.mark.skipif(
    not workspace_module._descriptor_relative_scanning_available(),
    reason="descriptor-relative operations are unavailable",
)
def test_descriptor_warm_cache_does_not_reopen_unchanged_regular_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source.py"
    source.write_text("print('hello')\n", encoding="utf-8")
    fingerprinter = _descriptor_fingerprinter(tmp_path)
    cold = fingerprinter.capture()
    original_open = os.open
    reopened_regular_files: list[str] = []

    def tracked_open(path, flags, *args, **kwargs):
        if path == source.name and kwargs.get("dir_fd") is not None:
            reopened_regular_files.append(path)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(workspace_module.os, "open", tracked_open)

    warm = fingerprinter.capture()

    assert warm.files == cold.files
    assert reopened_regular_files == []


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative scanner is POSIX-only")
def test_descriptor_warm_cache_revalidates_when_directory_change_is_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    cached = tmp_path / "cached.txt"
    target = tmp_path / "target.txt"
    vanishing = tmp_path / "vanishing.txt"
    cached.write_text("cached\n", encoding="utf-8")
    target.write_text("target\n", encoding="utf-8")
    fingerprinter = _descriptor_fingerprinter(tmp_path)
    fingerprinter.capture()
    vanishing.write_text("vanishing\n", encoding="utf-8")
    original_stat = os.stat
    switched = False

    def racing_stat(path, *args, **kwargs):
        nonlocal switched
        descriptor_relative = (
            kwargs.get("dir_fd") is not None
            and kwargs.get("follow_symlinks") is False
        )
        if path == cached.name and descriptor_relative and not switched:
            metadata = original_stat(path, *args, **kwargs)
            cached.unlink()
            try:
                cached.symlink_to(target.name)
            except OSError:
                pytest.skip("symlinks are unavailable on this platform")
            switched = True
            return metadata
        if path == vanishing.name and descriptor_relative:
            vanishing.unlink(missing_ok=True)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(workspace_module.os, "stat", racing_stat)

    with pytest.raises(SecurityError, match="cached.txt"):
        fingerprinter.capture()


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


def _descriptor_fingerprinter(tmp_path: Path):
    if not workspace_module._descriptor_relative_scanning_available():
        pytest.skip("descriptor-relative operations are unavailable")
    fingerprinter = workspace_module.WorkspaceFingerprinter(tmp_path)
    fingerprinter._use_descriptor_relative_scanner = True
    return fingerprinter


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative scanner is POSIX-only")
def test_descriptor_entry_removed_after_enumeration_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "vanishing.txt").write_text("content\n", encoding="utf-8")
    fingerprinter = _descriptor_fingerprinter(tmp_path)
    original_stat = os.stat

    def missing_stat(path, *args, **kwargs):
        if (
            path == "vanishing.txt"
            and kwargs.get("dir_fd") is not None
            and kwargs.get("follow_symlinks") is False
        ):
            (tmp_path / "vanishing.txt").unlink()
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(workspace_module.os, "stat", missing_stat)

    snapshot = fingerprinter.capture()

    assert snapshot.files["vanishing.txt"] == "unreadable"
    assert "vanishing.txt" not in fingerprinter.cache


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative scanner is POSIX-only")
@pytest.mark.parametrize("failure_point", ["open", "read"])
def test_descriptor_regular_file_io_errors_are_unreadable_and_close_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
):
    blocked = tmp_path / "blocked.txt"
    blocked.write_text("content\n", encoding="utf-8")
    mode = stat.S_IMODE(blocked.stat().st_mode)
    blocked_inode = blocked.stat().st_ino
    fingerprinter = _descriptor_fingerprinter(tmp_path)
    original_open = os.open
    original_close = os.close
    original_read = os.read
    opened: set[int] = set()

    def tracked_open(path, flags, *args, **kwargs):
        if (
            failure_point == "open"
            and path == "blocked.txt"
            and kwargs.get("dir_fd") is not None
        ):
            raise PermissionError("forced file open failure")
        descriptor = original_open(path, flags, *args, **kwargs)
        opened.add(descriptor)
        return descriptor

    def tracked_close(descriptor):
        opened.discard(descriptor)
        return original_close(descriptor)

    def failing_read(descriptor, size):
        if (
            failure_point == "read"
            and descriptor in opened
            and os.fstat(descriptor).st_ino == blocked_inode
        ):
            raise PermissionError("forced file read failure")
        return original_read(descriptor, size)

    monkeypatch.setattr(workspace_module.os, "open", tracked_open)
    monkeypatch.setattr(workspace_module.os, "close", tracked_close)
    monkeypatch.setattr(workspace_module.os, "read", failing_read)

    snapshot = fingerprinter.capture()

    marker = f"file:{mode:o}:unreadable"
    assert snapshot.files["blocked.txt"] == marker
    assert fingerprinter.cache["blocked.txt"][1] == marker
    assert opened == set()


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative scanner is POSIX-only")
def test_descriptor_symlink_readlink_error_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    link = tmp_path / "current"
    try:
        link.symlink_to("missing-target")
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    mode = stat.S_IMODE(link.lstat().st_mode)
    fingerprinter = _descriptor_fingerprinter(tmp_path)
    original_readlink = os.readlink

    def failing_readlink(path, *args, **kwargs):
        if path == "current" and kwargs.get("dir_fd") is not None:
            raise PermissionError("forced readlink failure")
        return original_readlink(path, *args, **kwargs)

    monkeypatch.setattr(workspace_module.os, "readlink", failing_readlink)

    snapshot = fingerprinter.capture()

    marker = f"symlink:{mode:o}:unreadable"
    assert snapshot.files["current"] == marker
    assert fingerprinter.cache["current"][1] == marker


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative scanner is POSIX-only")
def test_descriptor_unreadable_directory_keeps_marker_and_closes_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "nested.txt").write_text("content\n", encoding="utf-8")
    mode = stat.S_IMODE(blocked.stat().st_mode)
    fingerprinter = _descriptor_fingerprinter(tmp_path)
    original_open = os.open
    original_close = os.close
    opened: set[int] = set()

    def tracked_open(path, flags, *args, **kwargs):
        if path == "blocked" and kwargs.get("dir_fd") is not None:
            raise PermissionError("forced directory open failure")
        descriptor = original_open(path, flags, *args, **kwargs)
        opened.add(descriptor)
        return descriptor

    def tracked_close(descriptor):
        opened.discard(descriptor)
        return original_close(descriptor)

    monkeypatch.setattr(workspace_module.os, "open", tracked_open)
    monkeypatch.setattr(workspace_module.os, "close", tracked_close)

    snapshot = fingerprinter.capture()

    marker = f"directory:{mode:o}"
    assert snapshot.files["blocked"] == marker
    assert "blocked/nested.txt" not in snapshot.files
    assert fingerprinter.cache["blocked"][1] == marker
    assert opened == set()


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative scanner is POSIX-only")
def test_descriptor_open_failure_that_became_symlink_fails_closed_and_closes_fds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    race = tmp_path / "race.txt"
    target = tmp_path / "target.txt"
    race.write_text("before\n", encoding="utf-8")
    target.write_text("target\n", encoding="utf-8")
    fingerprinter = _descriptor_fingerprinter(tmp_path)
    original_open = os.open
    original_close = os.close
    opened: set[int] = set()
    switched = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal switched
        if (
            not switched
            and path == "race.txt"
            and kwargs.get("dir_fd") is not None
        ):
            switched = True
            race.unlink()
            try:
                race.symlink_to(target.name)
            except OSError:
                pytest.skip("symlinks are unavailable on this platform")
        descriptor = original_open(path, flags, *args, **kwargs)
        opened.add(descriptor)
        return descriptor

    def tracked_close(descriptor):
        opened.discard(descriptor)
        return original_close(descriptor)

    monkeypatch.setattr(workspace_module.os, "open", racing_open)
    monkeypatch.setattr(workspace_module.os, "close", tracked_close)

    with pytest.raises(SecurityError, match="race.txt"):
        fingerprinter.capture()

    assert opened == set()


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative scanner is POSIX-only")
def test_descriptor_root_metadata_failure_remains_fail_closed_with_unreadable_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "blocked.txt").write_text("content\n", encoding="utf-8")
    fingerprinter = _descriptor_fingerprinter(tmp_path)
    original_stat = os.stat
    original_fstat = os.fstat
    root_fstats = 0

    def failing_stat(path, *args, **kwargs):
        if path == "blocked.txt" and kwargs.get("dir_fd") is not None:
            raise PermissionError("forced entry stat failure")
        return original_stat(path, *args, **kwargs)

    def failing_final_root_fstat(descriptor):
        nonlocal root_fstats
        root_fstats += 1
        if root_fstats == 3:
            raise OSError("forced root metadata failure")
        return original_fstat(descriptor)

    monkeypatch.setattr(workspace_module.os, "stat", failing_stat)
    monkeypatch.setattr(workspace_module.os, "fstat", failing_final_root_fstat)

    with pytest.raises(OSError, match="forced root metadata failure"):
        fingerprinter.capture()


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative scanner is POSIX-only")
def test_non_directory_git_controls_match_fallback(tmp_path: Path):
    if not workspace_module._descriptor_relative_scanning_available():
        pytest.skip("descriptor-relative operations are unavailable")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "hooks").write_text("not a directory\n", encoding="utf-8")
    (git_dir / "info").write_text("not a directory\n", encoding="utf-8")

    descriptor = workspace_module.WorkspaceFingerprinter(tmp_path)
    descriptor._use_descriptor_relative_scanner = True
    fallback = workspace_module.WorkspaceFingerprinter(tmp_path)
    fallback._use_descriptor_relative_scanner = False

    descriptor_snapshot = descriptor.capture()
    fallback_snapshot = fallback.capture()

    assert descriptor_snapshot.files == fallback_snapshot.files
    assert ".git/hooks" not in descriptor_snapshot.files
    assert ".git/info" not in descriptor_snapshot.files


@pytest.mark.skipif(os.name != "posix", reason="POSIX filesystem anchor required")
@pytest.mark.parametrize("use_descriptor", [True, False], ids=["descriptor", "fallback"])
def test_git_pointer_rejects_anchor_only_administrative_directory(
    tmp_path: Path, use_descriptor: bool
):
    if use_descriptor and not workspace_module._descriptor_relative_scanning_available():
        pytest.skip("descriptor-relative operations are unavailable")
    (tmp_path / ".git").write_text("gitdir: /\n", encoding="utf-8")
    fingerprinter = workspace_module.WorkspaceFingerprinter(tmp_path)
    fingerprinter._use_descriptor_relative_scanner = use_descriptor

    with pytest.raises(
        SecurityError, match=r"\.git gitdir.*filesystem anchor"
    ) as caught:
        fingerprinter.capture()

    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize(
    ("pointer", "message"),
    [
        (b"not-a-git-pointer\n", "valid linked-worktree pointer"),
        (b"gitdir: /tmp\nunexpected second line\n", "malformed"),
    ],
    ids=["malformed", "multiline"],
)
def test_git_pointer_rejects_malformed_metadata_without_path_leakage(
    tmp_path: Path, pointer: bytes, message: str
):
    (tmp_path / ".git").write_bytes(pointer)

    with pytest.raises(SecurityError, match=message) as caught:
        workspace_module.WorkspaceFingerprinter(tmp_path).capture()

    assert str(tmp_path) not in str(caught.value)


def test_git_pointer_rejects_oversized_metadata_without_path_leakage(tmp_path: Path):
    (tmp_path / ".git").write_bytes(b"gitdir: " + b"x" * 65536)

    with pytest.raises(SecurityError, match=r"\.git is too large") as caught:
        workspace_module.WorkspaceFingerprinter(tmp_path).capture()

    assert str(tmp_path) not in str(caught.value)


def test_git_pointer_rejects_invalid_utf8_without_path_leakage(tmp_path: Path):
    (tmp_path / ".git").write_bytes(b"gitdir: \xff\n")

    with pytest.raises(SecurityError, match=r"\.git is not valid UTF-8") as caught:
        workspace_module.WorkspaceFingerprinter(tmp_path).capture()

    assert str(tmp_path) not in str(caught.value)


def test_git_pointer_rejects_symlinked_administrative_path_component(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    real_parent = tmp_path / "real-parent"
    administrative_directory = real_parent / "worktrees" / "linked"
    workspace.mkdir()
    administrative_directory.mkdir(parents=True)
    symlinked_parent = tmp_path / "symlinked-parent"
    try:
        symlinked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    (workspace / ".git").write_text(
        f"gitdir: {symlinked_parent / 'worktrees' / 'linked'}\n",
        encoding="utf-8",
    )

    with pytest.raises(SecurityError, match="must not traverse symbolic links") as caught:
        workspace_module.WorkspaceFingerprinter(workspace).capture()

    assert str(tmp_path) not in str(caught.value)


def test_linked_worktree_rejects_commondir_backlink_mismatch_without_path_leakage(
    tmp_path: Path,
):
    _repository, worktree, worktree_git_dir = _create_linked_worktree(tmp_path)
    (worktree_git_dir / "gitdir").write_text(
        str(tmp_path / "different-worktree" / ".git") + "\n", encoding="utf-8"
    )

    with pytest.raises(SecurityError, match="back-reference does not match") as caught:
        workspace_module.WorkspaceFingerprinter(worktree).capture()

    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize("use_descriptor", [True, False], ids=["descriptor", "fallback"])
def test_linked_worktree_git_controls_are_captured_and_change_fingerprint(
    tmp_path: Path, use_descriptor: bool
):
    repository, worktree, worktree_git_dir = _create_linked_worktree(tmp_path)
    if use_descriptor and not workspace_module._descriptor_relative_scanning_available():
        pytest.skip("descriptor-relative operations are unavailable")
    common_git_dir = repository / ".git"
    controls = {
        ".git/config": common_git_dir / "config",
        ".git/config.worktree": worktree_git_dir / "config.worktree",
        ".git/hooks/review-hook": common_git_dir / "hooks" / "review-hook",
        ".git/info/attributes": common_git_dir / "info" / "attributes",
    }
    controls[".git/config.worktree"].write_text(
        "[test]\n\tworktree = one\n", encoding="utf-8"
    )
    controls[".git/hooks/review-hook"].write_text(
        "#!/bin/sh\nexit 0\n", encoding="utf-8"
    )
    controls[".git/info/attributes"].write_text(
        "*.review -diff\n", encoding="utf-8"
    )
    fingerprinter = workspace_module.WorkspaceFingerprinter(worktree)
    fingerprinter._use_descriptor_relative_scanner = use_descriptor

    snapshot = fingerprinter.capture()

    assert set(controls) <= set(snapshot.files)
    assert str(tmp_path) not in "\n".join(snapshot.files)
    for index, (synthetic_key, control_path) in enumerate(controls.items()):
        before = snapshot.fingerprint
        with control_path.open("a", encoding="utf-8") as handle:
            handle.write(f"# fingerprint mutation {index}\n")
        snapshot = fingerprinter.capture()
        assert snapshot.fingerprint != before, synthetic_key


@pytest.mark.skipif(
    not workspace_module._descriptor_relative_scanning_available(),
    reason="descriptor-relative operations are unavailable",
)
def test_linked_worktree_git_controls_match_between_scanners(tmp_path: Path):
    repository, worktree, worktree_git_dir = _create_linked_worktree(tmp_path)
    common_git_dir = repository / ".git"
    (worktree_git_dir / "config.worktree").write_text(
        "[test]\n\tworktree = one\n", encoding="utf-8"
    )
    (common_git_dir / "hooks" / "review-hook").write_text(
        "#!/bin/sh\nexit 0\n", encoding="utf-8"
    )
    (common_git_dir / "info" / "attributes").write_text(
        "*.review -diff\n", encoding="utf-8"
    )
    descriptor = workspace_module.WorkspaceFingerprinter(worktree)
    descriptor._use_descriptor_relative_scanner = True
    fallback = workspace_module.WorkspaceFingerprinter(worktree)
    fallback._use_descriptor_relative_scanner = False

    assert descriptor.capture().files == fallback.capture().files


@pytest.mark.parametrize("use_descriptor", [True, False], ids=["descriptor", "fallback"])
@pytest.mark.parametrize("control_name", ["hooks", "info"])
def test_linked_worktree_symlinked_common_git_control_directories_fail_closed(
    tmp_path: Path, use_descriptor: bool, control_name: str
):
    repository, worktree, _worktree_git_dir = _create_linked_worktree(tmp_path)
    if use_descriptor and not workspace_module._descriptor_relative_scanning_available():
        pytest.skip("descriptor-relative operations are unavailable")
    control = repository / ".git" / control_name
    original = repository / ".git" / f"{control_name}.original"
    control.rename(original)
    try:
        control.symlink_to(original.name)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    fingerprinter = workspace_module.WorkspaceFingerprinter(worktree)
    fingerprinter._use_descriptor_relative_scanner = use_descriptor

    with pytest.raises(SecurityError, match=rf"\.git/{control_name}"):
        fingerprinter.capture()


@pytest.mark.parametrize("use_descriptor", [True, False], ids=["descriptor", "fallback"])
@pytest.mark.parametrize("control_name", ["hooks", "info"])
def test_symlinked_git_control_directories_fail_closed(
    tmp_path: Path, use_descriptor: bool, control_name: str
):
    if use_descriptor and not workspace_module._descriptor_relative_scanning_available():
        pytest.skip("descriptor-relative operations are unavailable")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    try:
        (git_dir / control_name).symlink_to("missing-control-target")
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    fingerprinter = workspace_module.WorkspaceFingerprinter(tmp_path)
    fingerprinter._use_descriptor_relative_scanner = use_descriptor

    with pytest.raises(SecurityError, match=rf"\.git/{control_name}"):
        fingerprinter.capture()


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


def test_capture_adapter_keeps_direct_string_tolerance(tmp_path: Path):
    executor = SimpleNamespace(
        cwd=tmp_path,
        workspace_fingerprint=lambda **_kwargs: "legacy-fingerprint",
    )

    assert capture_workspace_fingerprint(executor) == "legacy-fingerprint"


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
