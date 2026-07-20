from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from mini_code_agent.security import SafeWorkspace, SecurityError


SKIP_DIRS = {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
FingerprintSignature = tuple[int, int, int, int, int, int]
FingerprintCache = dict[str, tuple[FingerprintSignature, str]]
CachedRegularCandidate = tuple[str, str, FingerprintSignature]
_MAX_GIT_REFERENCE_BYTES = 65536


@dataclass(frozen=True)
class _GitControlDirectories:
    worktree: Path
    common: Path


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _file_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _read_git_reference(path: Path, label: str) -> str:
    try:
        original_metadata = path.lstat()
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(original_metadata.st_mode) or not stat.S_ISREG(
        original_metadata.st_mode
    ):
        raise SecurityError(f"{label} must be a regular file")
    file_fd = os.open(path, _file_flags())
    try:
        opened_metadata = os.fstat(file_fd)
        if not stat.S_ISREG(opened_metadata.st_mode):
            raise SecurityError(f"{label} changed while fingerprinting")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(file_fd, min(4096, _MAX_GIT_REFERENCE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_GIT_REFERENCE_BYTES:
                raise SecurityError(f"{label} is too large")
        if _metadata_signature(os.fstat(file_fd)) != _metadata_signature(
            opened_metadata
        ):
            raise OSError(f"{label} changed while fingerprinting")
    finally:
        os.close(file_fd)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as error:
        raise SecurityError(f"{label} is not valid UTF-8") from error


def _single_git_reference(value: str, label: str) -> str:
    lines = value.splitlines()
    if len(lines) != 1 or not lines[0].strip() or "\x00" in lines[0]:
        raise SecurityError(f"{label} is malformed")
    return lines[0].strip()


def _validated_git_directory(base: Path, value: str, label: str) -> Path:
    reference = Path(_single_git_reference(value, label))
    candidate = reference if reference.is_absolute() else base / reference
    candidate = Path(os.path.abspath(candidate))
    current = Path(candidate.anchor)
    start = 1 if candidate.anchor else 0
    if not candidate.parts[start:]:
        raise SecurityError(f"{label} must not reference a filesystem anchor")
    try:
        for part in candidate.parts[start:]:
            current /= part
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise SecurityError(f"{label} must not traverse symbolic links")
    except FileNotFoundError as error:
        raise SecurityError(f"{label} does not exist") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise SecurityError(f"{label} must reference a directory")
    return candidate


def _git_control_directories(root: Path) -> _GitControlDirectories | None:
    git_entry = root / ".git"
    try:
        metadata = git_entry.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISDIR(metadata.st_mode):
        return _GitControlDirectories(worktree=git_entry, common=git_entry)
    if not stat.S_ISREG(metadata.st_mode):
        return None

    pointer = _single_git_reference(_read_git_reference(git_entry, ".git"), ".git")
    prefix = "gitdir: "
    if not pointer.startswith(prefix) or not pointer[len(prefix) :].strip():
        raise SecurityError(".git is not a valid linked-worktree pointer")
    worktree_git_dir = _validated_git_directory(
        root, pointer[len(prefix) :], ".git gitdir"
    )
    commondir_path = worktree_git_dir / "commondir"
    try:
        common_reference = _read_git_reference(
            commondir_path, ".git gitdir/commondir"
        )
    except FileNotFoundError:
        return _GitControlDirectories(
            worktree=worktree_git_dir, common=worktree_git_dir
        )

    common_git_dir = _validated_git_directory(
        worktree_git_dir, common_reference, ".git common directory"
    )
    backlink = _single_git_reference(
        _read_git_reference(worktree_git_dir / "gitdir", ".git gitdir/gitdir"),
        ".git gitdir/gitdir",
    )
    backlink_path = Path(backlink)
    if not backlink_path.is_absolute():
        backlink_path = worktree_git_dir / backlink_path
    if Path(os.path.abspath(backlink_path)) != git_entry:
        raise SecurityError(".git linked-worktree back-reference does not match")
    return _GitControlDirectories(worktree=worktree_git_dir, common=common_git_dir)


def _descriptor_relative_scanning_available() -> bool:
    """Return whether the runtime can scan without rebuilding absolute paths."""

    supports_dir_fd = getattr(os, "supports_dir_fd", set())
    supports_fd = getattr(os, "supports_fd", set())
    supports_follow_symlinks = getattr(os, "supports_follow_symlinks", set())
    return (
        os.name == "posix"
        and hasattr(os, "O_NOFOLLOW")
        and os.open in supports_dir_fd
        and os.stat in supports_dir_fd
        and os.stat in supports_follow_symlinks
        and os.readlink in supports_dir_fd
        and os.scandir in supports_fd
    )


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
        snapshot = WorkspaceFingerprinter(root, cache=cache).capture(
            ignore_paths=ignore_paths
        )
        return cls(root=snapshot.root, files=snapshot.files)

    @cached_property
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.files,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def diff(self, other: "WorkspaceSnapshot") -> dict[str, list[str]]:
        before = self.files
        after = other.files
        return {
            "created": sorted(set(after) - set(before)),
            "deleted": sorted(set(before) - set(after)),
            "modified": sorted(
                path
                for path in set(before) & set(after)
                if before[path] != after[path]
            ),
        }


class WorkspaceFingerprinter:
    """Capture secure workspace snapshots while owning reusable hash state."""

    def __init__(
        self,
        root: Path,
        *,
        cache: FingerprintCache | None = None,
    ) -> None:
        self.root = root.resolve()
        self.workspace = SafeWorkspace(self.root)
        self.cache = cache if cache is not None else {}
        self._use_descriptor_relative_scanner = (
            _descriptor_relative_scanning_available()
        )

    def capture(
        self, *, ignore_paths: set[Path] | None = None
    ) -> WorkspaceSnapshot:
        ignored, ignored_ancestors = self._ignored_relatives(ignore_paths)
        files: dict[str, str] = {}
        live_cache_keys: set[str] = set()
        if self._use_descriptor_relative_scanner:
            self._capture_descriptor_relative(
                files, live_cache_keys, ignored, ignored_ancestors
            )
        else:
            self._capture_fallback(
                files, live_cache_keys, ignored, ignored_ancestors
            )
        for stale in set(self.cache) - live_cache_keys:
            self.cache.pop(stale, None)
        return WorkspaceSnapshot(root=self.root, files=files)

    def _ignored_relatives(
        self, ignore_paths: set[Path] | None
    ) -> tuple[set[str], set[str]]:
        ignored: set[str] = set()
        ignored_ancestors: set[str] = set()
        for path in sorted(ignore_paths or set(), key=str):
            try:
                resolved_ignored = self.workspace.resolve(path)
                ignored.add(str(resolved_ignored.relative_to(self.root)))
                parent = resolved_ignored.parent
                while parent != self.root and parent.is_relative_to(self.root):
                    ignored_ancestors.add(str(parent.relative_to(self.root)))
                    parent = parent.parent
            except (OSError, ValueError, SecurityError):
                continue
        return ignored, ignored_ancestors

    def _capture_descriptor_relative(
        self,
        files: dict[str, str],
        live_cache_keys: set[str],
        ignored: set[str],
        ignored_ancestors: set[str],
    ) -> None:
        root_fd = os.open(self.root, _directory_flags())
        try:
            root_metadata = os.fstat(root_fd)
            if not stat.S_ISDIR(root_metadata.st_mode):
                raise NotADirectoryError(self.root)
            root_signature = _metadata_signature(root_metadata)
            root_cached_regulars: list[CachedRegularCandidate] = []
            unreadable_entry = self._scan_directory(
                root_fd,
                (),
                files,
                live_cache_keys,
                ignored,
                ignored_ancestors,
                skip_dir_names=SKIP_DIRS,
                cached_regulars=root_cached_regulars,
            )
            self._capture_git_controls(root_fd, files, live_cache_keys)
            final_root_signature = _metadata_signature(os.fstat(root_fd))
            if final_root_signature != root_signature:
                if not unreadable_entry:
                    raise OSError("workspace root changed while fingerprinting")
                self._revalidate_cached_regulars(
                    root_fd, root_cached_regulars
                )
        finally:
            os.close(root_fd)

    def _scan_directory(
        self,
        directory_fd: int,
        relative_parts: tuple[str, ...],
        files: dict[str, str],
        live_cache_keys: set[str],
        ignored: set[str],
        ignored_ancestors: set[str],
        *,
        skip_dir_names: set[str],
        cached_regulars: list[CachedRegularCandidate] | None = None,
    ) -> bool:
        directory_metadata = os.fstat(directory_fd)
        if not stat.S_ISDIR(directory_metadata.st_mode):
            raise NotADirectoryError(_relative_name(relative_parts))
        directory_signature = _metadata_signature(directory_metadata)
        with os.scandir(directory_fd) as entries:
            names = sorted(entry.name for entry in entries)
        cached_regulars = cached_regulars if cached_regulars is not None else []
        unreadable_entry = False
        for name in names:
            unreadable_entry = self._capture_named_entry(
                directory_fd,
                name,
                relative_parts,
                files,
                live_cache_keys,
                ignored,
                ignored_ancestors,
                recurse_directories=True,
                skip_dir_names=skip_dir_names,
                cached_regulars=cached_regulars,
            ) or unreadable_entry
        final_directory_signature = _metadata_signature(os.fstat(directory_fd))
        if final_directory_signature != directory_signature:
            if not unreadable_entry:
                display = _relative_name(relative_parts) or "."
                raise OSError(f"directory changed while fingerprinting: {display}")
            self._revalidate_cached_regulars(directory_fd, cached_regulars)
        return unreadable_entry

    def _capture_named_entry(
        self,
        parent_fd: int,
        name: str,
        parent_parts: tuple[str, ...],
        files: dict[str, str],
        live_cache_keys: set[str],
        ignored: set[str],
        ignored_ancestors: set[str],
        *,
        recurse_directories: bool,
        skip_dir_names: set[str],
        enumerated: bool = True,
        cached_regulars: list[CachedRegularCandidate] | None = None,
    ) -> bool:
        relative_parts = (*parent_parts, name)
        relative = _relative_name(relative_parts)
        excluded = relative in ignored or relative in ignored_ancestors
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            if not enumerated:
                raise
            if not excluded:
                live_cache_keys.add(relative)
                files[relative] = "unreadable"
            return True

        if stat.S_ISLNK(metadata.st_mode):
            if not excluded:
                return self._store_metadata_entry(
                    parent_fd,
                    name,
                    relative,
                    metadata,
                    files,
                    live_cache_keys,
                )
            return False

        if stat.S_ISDIR(metadata.st_mode):
            if name in skip_dir_names:
                return False
            try:
                child_fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
            except OSError:
                self._raise_if_symlink_after_open_failure(parent_fd, name, relative)
                if not excluded:
                    self._store_metadata_entry(
                        parent_fd,
                        name,
                        relative,
                        metadata,
                        files,
                        live_cache_keys,
                    )
                return True
            try:
                child_metadata = os.fstat(child_fd)
                if not stat.S_ISDIR(child_metadata.st_mode):
                    raise SecurityError(
                        f"directory entry changed while fingerprinting: {relative}"
                    )
                if not excluded:
                    self._store_metadata_entry(
                        parent_fd,
                        name,
                        relative,
                        child_metadata,
                        files,
                        live_cache_keys,
                    )
                if recurse_directories:
                    return self._scan_directory(
                        child_fd,
                        relative_parts,
                        files,
                        live_cache_keys,
                        ignored,
                        ignored_ancestors,
                        skip_dir_names=skip_dir_names,
                    )
            finally:
                os.close(child_fd)
            return False

        if stat.S_ISREG(metadata.st_mode):
            if excluded:
                return False
            signature = _metadata_signature(metadata)
            cached = self.cache.get(relative)
            if cached is not None and cached[0] == signature:
                live_cache_keys.add(relative)
                files[relative] = cached[1]
                if cached_regulars is not None:
                    cached_regulars.append((name, relative, signature))
                return False
            try:
                file_fd = os.open(name, _file_flags(), dir_fd=parent_fd)
            except OSError:
                self._raise_if_symlink_after_open_failure(parent_fd, name, relative)
                self._store_unreadable_regular_file(
                    relative, metadata, files, live_cache_keys
                )
                return True
            try:
                file_metadata = os.fstat(file_fd)
                if not stat.S_ISREG(file_metadata.st_mode):
                    raise SecurityError(
                        f"file entry changed while fingerprinting: {relative}"
                    )
                return self._store_regular_file(
                    file_fd,
                    relative,
                    file_metadata,
                    files,
                    live_cache_keys,
                )
            finally:
                os.close(file_fd)

        if not excluded:
            return self._store_metadata_entry(
                parent_fd,
                name,
                relative,
                metadata,
                files,
                live_cache_keys,
            )
        return False

    def _revalidate_cached_regulars(
        self,
        parent_fd: int,
        candidates: list[CachedRegularCandidate],
    ) -> None:
        for name, relative, expected_signature in candidates:
            try:
                file_fd = os.open(name, _file_flags(), dir_fd=parent_fd)
            except OSError as error:
                self._raise_if_symlink_after_open_failure(
                    parent_fd, name, relative
                )
                raise OSError(
                    f"cached file could not be revalidated: {relative}"
                ) from error
            try:
                metadata = os.fstat(file_fd)
                if not stat.S_ISREG(metadata.st_mode):
                    raise SecurityError(
                        f"cached file changed type while fingerprinting: {relative}"
                    )
                if _metadata_signature(metadata) != expected_signature:
                    raise OSError(
                        f"cached file changed while fingerprinting: {relative}"
                    )
            finally:
                os.close(file_fd)

    def _store_metadata_entry(
        self,
        parent_fd: int,
        name: str,
        relative: str,
        metadata: os.stat_result,
        files: dict[str, str],
        live_cache_keys: set[str],
    ) -> bool:
        live_cache_keys.add(relative)
        signature = _metadata_signature(metadata)
        cached = self.cache.get(relative)
        if cached is not None and cached[0] == signature:
            files[relative] = cached[1]
            return False

        mode = stat.S_IMODE(metadata.st_mode)
        unreadable = False
        if stat.S_ISLNK(metadata.st_mode):
            try:
                target = os.readlink(name, dir_fd=parent_fd)
            except OSError:
                target = "unreadable"
                unreadable = True
            value = f"symlink:{mode:o}:{target}"
        elif stat.S_ISDIR(metadata.st_mode):
            value = f"directory:{mode:o}"
        else:
            value = f"special:{stat.S_IFMT(metadata.st_mode):o}:{mode:o}"
        self.cache[relative] = (signature, value)
        files[relative] = value
        return unreadable

    def _store_regular_file(
        self,
        file_fd: int,
        relative: str,
        metadata: os.stat_result,
        files: dict[str, str],
        live_cache_keys: set[str],
    ) -> bool:
        live_cache_keys.add(relative)
        signature = _metadata_signature(metadata)
        cached = self.cache.get(relative)
        if cached is not None and cached[0] == signature:
            files[relative] = cached[1]
            return False

        try:
            digest = _hash_file_descriptor(file_fd)
        except OSError:
            self._store_unreadable_regular_file(
                relative, metadata, files, live_cache_keys
            )
            return True
        if _metadata_signature(os.fstat(file_fd)) != signature:
            raise OSError(f"file changed while fingerprinting: {relative}")
        mode = stat.S_IMODE(metadata.st_mode)
        value = f"file:{mode:o}:{digest}"
        self.cache[relative] = (signature, value)
        files[relative] = value
        return False

    def _store_unreadable_regular_file(
        self,
        relative: str,
        metadata: os.stat_result,
        files: dict[str, str],
        live_cache_keys: set[str],
    ) -> None:
        signature = _metadata_signature(metadata)
        mode = stat.S_IMODE(metadata.st_mode)
        value = f"file:{mode:o}:unreadable"
        live_cache_keys.add(relative)
        self.cache[relative] = (signature, value)
        files[relative] = value

    @staticmethod
    def _raise_if_symlink_after_open_failure(
        parent_fd: int, name: str, relative: str
    ) -> None:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            return
        if stat.S_ISLNK(current.st_mode):
            raise SecurityError(
                f"entry changed to a symbolic link while fingerprinting: {relative}"
            )

    def _capture_git_controls(
        self,
        root_fd: int,
        files: dict[str, str],
        live_cache_keys: set[str],
    ) -> None:
        directories = _git_control_directories(self.root)
        if directories is None:
            return
        normal_git_dir = self.root / ".git"
        if directories.worktree == normal_git_dir:
            worktree_fd = os.open(".git", _directory_flags(), dir_fd=root_fd)
        else:
            worktree_fd = os.open(directories.worktree, _directory_flags())
        try:
            worktree_metadata = os.fstat(worktree_fd)
            if not stat.S_ISDIR(worktree_metadata.st_mode):
                raise SecurityError(".git worktree directory changed")
            if directories.common == directories.worktree:
                common_fd = os.dup(worktree_fd)
            else:
                common_fd = os.open(directories.common, _directory_flags())
            try:
                common_metadata = os.fstat(common_fd)
                if not stat.S_ISDIR(common_metadata.st_mode):
                    raise SecurityError(".git common directory changed")
                self._capture_optional_git_entry(
                    common_fd, "config", (".git",), files, live_cache_keys
                )
                self._capture_optional_git_entry(
                    worktree_fd,
                    "config.worktree",
                    (".git",),
                    files,
                    live_cache_keys,
                )
                self._capture_hooks(common_fd, files, live_cache_keys)
                self._capture_git_attributes(common_fd, files, live_cache_keys)
                if _metadata_signature(os.fstat(common_fd)) != _metadata_signature(
                    common_metadata
                ):
                    raise OSError(".git common directory changed while fingerprinting")
                if _metadata_signature(
                    os.fstat(worktree_fd)
                ) != _metadata_signature(worktree_metadata):
                    raise OSError(
                        ".git worktree directory changed while fingerprinting"
                    )
            finally:
                os.close(common_fd)
        finally:
            os.close(worktree_fd)

    def _capture_optional_git_entry(
        self,
        parent_fd: int,
        name: str,
        parent_parts: tuple[str, ...],
        files: dict[str, str],
        live_cache_keys: set[str],
    ) -> None:
        try:
            self._capture_named_entry(
                parent_fd,
                name,
                parent_parts,
                files,
                live_cache_keys,
                set(),
                set(),
                recurse_directories=False,
                skip_dir_names=set(),
                enumerated=False,
            )
        except FileNotFoundError:
            return

    def _capture_hooks(
        self,
        git_fd: int,
        files: dict[str, str],
        live_cache_keys: set[str],
    ) -> None:
        try:
            metadata = os.stat("hooks", dir_fd=git_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise SecurityError(".git/hooks must not be a symbolic link")
        if not stat.S_ISDIR(metadata.st_mode):
            return
        hooks_fd = os.open("hooks", _directory_flags(), dir_fd=git_fd)
        try:
            if not stat.S_ISDIR(os.fstat(hooks_fd).st_mode):
                raise SecurityError(".git/hooks changed while fingerprinting")
            self._scan_directory(
                hooks_fd,
                (".git", "hooks"),
                files,
                live_cache_keys,
                set(),
                set(),
                skip_dir_names=set(),
            )
        finally:
            os.close(hooks_fd)

    def _capture_git_attributes(
        self,
        git_fd: int,
        files: dict[str, str],
        live_cache_keys: set[str],
    ) -> None:
        try:
            metadata = os.stat("info", dir_fd=git_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise SecurityError(".git/info must not be a symbolic link")
        if not stat.S_ISDIR(metadata.st_mode):
            return
        info_fd = os.open("info", _directory_flags(), dir_fd=git_fd)
        try:
            info_metadata = os.fstat(info_fd)
            if not stat.S_ISDIR(info_metadata.st_mode):
                raise SecurityError(".git/info changed while fingerprinting")
            info_signature = _metadata_signature(info_metadata)
            self._capture_optional_git_entry(
                info_fd,
                "attributes",
                (".git", "info"),
                files,
                live_cache_keys,
            )
            if _metadata_signature(os.fstat(info_fd)) != info_signature:
                raise OSError(".git/info changed while fingerprinting")
        finally:
            os.close(info_fd)

    def _capture_fallback(
        self,
        files: dict[str, str],
        live_cache_keys: set[str],
        ignored: set[str],
        ignored_ancestors: set[str],
    ) -> None:
        for path in self.workspace.iter_entries(
            self.root, skip_dir_names=SKIP_DIRS
        ):
            relative = str(path.relative_to(self.root))
            if relative in ignored or relative in ignored_ancestors:
                continue
            live_cache_keys.add(relative)
            files[relative] = _hash_entry(
                path, self.workspace, relative, self.cache
            )

        # Do not hash the mutable Git object/index database, but do bind local
        # configuration and executable hooks that can change command behavior.
        directories = _git_control_directories(self.root)
        git_controls: list[tuple[Path, SafeWorkspace, str]] = []
        if directories is not None:
            worktree_workspace = SafeWorkspace(directories.worktree)
            common_workspace = (
                worktree_workspace
                if directories.common == directories.worktree
                else SafeWorkspace(directories.common)
            )
            git_controls.extend(
                [
                    (
                        directories.common / "config",
                        common_workspace,
                        ".git/config",
                    ),
                    (
                        directories.worktree / "config.worktree",
                        worktree_workspace,
                        ".git/config.worktree",
                    ),
                ]
            )
            hooks = directories.common / "hooks"
            try:
                hooks_metadata = hooks.lstat()
            except FileNotFoundError:
                hooks_metadata = None
            if hooks_metadata is not None and stat.S_ISLNK(hooks_metadata.st_mode):
                raise SecurityError(".git/hooks must not be a symbolic link")
            if hooks_metadata is not None and stat.S_ISDIR(hooks_metadata.st_mode):
                git_controls.extend(
                    (
                        path,
                        common_workspace,
                        str(Path(".git/hooks") / path.relative_to(hooks)),
                    )
                    for path in common_workspace.iter_entries(hooks)
                )
            info = directories.common / "info"
            try:
                info_metadata = info.lstat()
            except FileNotFoundError:
                info_metadata = None
            if info_metadata is not None and stat.S_ISLNK(info_metadata.st_mode):
                raise SecurityError(".git/info must not be a symbolic link")
            if info_metadata is not None and stat.S_ISDIR(info_metadata.st_mode):
                git_controls.append(
                    (
                        info / "attributes",
                        common_workspace,
                        ".git/info/attributes",
                    )
                )
        for path, workspace, relative in git_controls:
            try:
                path.lstat()
            except OSError:
                continue
            live_cache_keys.add(relative)
            files[relative] = _hash_entry(
                path, workspace, relative, self.cache
            )


def _relative_name(parts: tuple[str, ...]) -> str:
    return str(Path(*parts)) if parts else ""


def _metadata_signature(metadata: os.stat_result) -> FingerprintSignature:
    return (
        metadata.st_mode,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


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
    signature = _metadata_signature(metadata)
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


def _hash_file_descriptor(file_fd: int) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = os.read(file_fd, 65536)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


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
