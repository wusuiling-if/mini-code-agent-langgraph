from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from mini_code_agent.security import SafeWorkspace, SecurityError


SKIP_DIRS = {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
FingerprintCache = dict[str, tuple[tuple[int, ...], str]]


@dataclass
class WorkspaceSnapshot:
    root: Path
    files: dict[str, str]

    @classmethod
    def capture(
        cls,
        root: Path,
        *,
        ignore_paths: set[Path] | None = None,
        cache: FingerprintCache | None = None,
    ) -> "WorkspaceSnapshot":
        root = root.resolve()
        workspace = SafeWorkspace(root)
        ignored: set[Path] = set()
        ignored_ancestors: set[Path] = set()
        for path in ignore_paths or set():
            try:
                resolved_ignored = workspace.resolve(path)
                ignored.add(resolved_ignored)
                parent = resolved_ignored.parent
                while parent != root and parent.is_relative_to(root):
                    ignored_ancestors.add(parent)
                    parent = parent.parent
            except (OSError, ValueError, SecurityError):
                continue
        files: dict[str, str] = {}
        live_cache_keys: set[str] = set()
        for path in workspace.iter_entries(root, skip_dir_names=SKIP_DIRS):
            if path in ignored or path in ignored_ancestors:
                continue
            relative = str(path.relative_to(root))
            live_cache_keys.add(relative)
            files[relative] = _hash_entry(path, workspace, relative, cache)
        # Do not hash the mutable Git object/index database, but do bind local
        # configuration and executable hooks that can change command behavior.
        git_dir = root / ".git"
        git_controls: list[Path] = []
        hooks = git_dir / "hooks"
        if git_dir.is_dir() and not git_dir.is_symlink():
            git_controls.extend([git_dir / "config", git_dir / "config.worktree"])
            if hooks.is_dir():
                git_controls.extend(workspace.iter_entries(hooks))
            git_controls.append(git_dir / "info" / "attributes")
        for path in git_controls:
            try:
                path.lstat()
            except OSError:
                continue
            relative = str(path.relative_to(root))
            live_cache_keys.add(relative)
            files[relative] = _hash_entry(path, workspace, relative, cache)
        if cache is not None:
            for stale in set(cache) - live_cache_keys:
                cache.pop(stale, None)
        return cls(root=root, files=files)

    def diff(self, other: "WorkspaceSnapshot") -> dict[str, list[str]]:
        before = self.files
        after = other.files
        return {
            "created": sorted(set(after) - set(before)),
            "deleted": sorted(set(before) - set(after)),
            "modified": sorted(path for path in set(before) & set(after) if before[path] != after[path]),
        }


def _hash_entry(
    path: Path,
    workspace: SafeWorkspace,
    relative: str,
    cache: FingerprintCache | None,
) -> str:
    try:
        metadata = path.lstat()
    except OSError:
        return "unreadable"
    signature = (
        metadata.st_mode,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )
    cached = cache.get(relative) if cache is not None else None
    if cached is not None and cached[0] == signature:
        return cached[1]

    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISLNK(metadata.st_mode):
        try:
            value = f"symlink:{mode:o}:{os.readlink(path)}"
        except OSError:
            value = f"symlink:{mode:o}:unreadable"
    elif stat.S_ISDIR(metadata.st_mode):
        value = f"directory:{mode:o}"
    elif stat.S_ISREG(metadata.st_mode):
        value = f"file:{mode:o}:{_hash_file(path, workspace)}"
    else:
        value = f"special:{stat.S_IFMT(metadata.st_mode):o}:{mode:o}"
    if cache is not None:
        cache[relative] = (signature, value)
    return value


def _hash_file(path: Path, workspace: SafeWorkspace | None = None) -> str:
    digest = hashlib.sha256()
    try:
        workspace = workspace or SafeWorkspace(path.parent)
        with workspace.open_binary(path) as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return "unreadable"
    return digest.hexdigest()
