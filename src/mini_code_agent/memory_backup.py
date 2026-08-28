"""Private full-store backup, restore, and explicit destructive purge."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from mini_code_agent.conversation_memory import verify_conversation_memory
from mini_code_agent.memory_store import SQLiteMemoryStore
from mini_code_agent.utils import MAX_STATE_FILE_BYTES

BACKUP_SCHEMA_VERSION = 1
MAX_BACKUP_BYTES = MAX_STATE_FILE_BYTES * 3
EXCLUDED_NAMES = frozenset(
    {
        "memory.lock",
        "conversation-events.lock",
        "embedding-cache.sqlite3",
        "embedding-cache.sqlite3-shm",
        "embedding-cache.sqlite3-wal",
    }
)


def export_memory_backup(memory_directory: Path, destination: Path) -> Path:
    """Write a plaintext private backup after authenticating durable state."""

    memory_directory = Path(os.path.abspath(Path(memory_directory).expanduser()))
    destination = Path(os.path.abspath(Path(destination).expanduser()))
    store = SQLiteMemoryStore(memory_directory, read_only=True)
    verification = store.verify()
    conversation = verify_conversation_memory(memory_directory)
    if not verification.ok or not conversation.ok:
        errors = (*verification.errors, *conversation.errors)
        raise RuntimeError(f"refusing to back up invalid memory: {'; '.join(errors)}")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"backup already exists: {destination}")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    files: list[dict[str, object]] = []
    selected: list[tuple[Path, str, bytes]] = []
    for path in sorted(memory_directory.rglob("*")):
        if path.name in EXCLUDED_NAMES:
            continue
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(f"memory backup refuses symlink: {path}")
        if not stat.S_ISREG(metadata.st_mode):
            continue
        relative = path.relative_to(memory_directory).as_posix()
        content = path.read_bytes()
        selected.append((path, relative, content))
        files.append(
            {
                "path": relative,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    manifest = {
        "schema_version": BACKUP_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "plaintext_sensitive": True,
        "files": files,
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600)
    os.close(descriptor)
    try:
        with zipfile.ZipFile(
            destination, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
            )
            for _path, relative, content in selected:
                archive.writestr(f"memory/{relative}", content)
        if destination.stat().st_size > MAX_BACKUP_BYTES:
            raise ValueError("memory backup exceeds the size limit")
        if os.name != "nt":
            destination.chmod(0o600)
        with tempfile.TemporaryDirectory(
            prefix=".memory-backup-check-", dir=destination.parent
        ) as validation_root:
            restore_memory_backup(
                destination,
                Path(validation_root) / "memory",
            )
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def restore_memory_backup(archive_path: Path, memory_directory: Path) -> Path:
    """Restore only into a new memory directory and verify before installation."""

    archive_path = Path(os.path.abspath(Path(archive_path).expanduser()))
    memory_directory = Path(os.path.abspath(Path(memory_directory).expanduser()))
    if memory_directory.exists() or memory_directory.is_symlink():
        raise FileExistsError(
            "memory state already exists; purge it explicitly before restore"
        )
    metadata = archive_path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("memory backup must be a regular non-symlink file")
    if metadata.st_size > MAX_BACKUP_BYTES:
        raise ValueError("memory backup exceeds the size limit")
    parent = memory_directory.parent.resolve()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".memory-restore-", dir=parent) as raw:
        temporary = Path(raw) / "memory"
        temporary.mkdir(mode=0o700)
        with zipfile.ZipFile(archive_path, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("memory backup contains duplicate entries")
            if sum(item.file_size for item in archive.infolist()) > MAX_BACKUP_BYTES:
                raise ValueError("memory backup expands beyond the size limit")
            try:
                manifest = json.loads(archive.read("manifest.json"))
            except (KeyError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "memory backup manifest is missing or invalid"
                ) from exc
            if manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
                raise ValueError("unsupported memory backup schema")
            raw_files = manifest.get("files")
            if not isinstance(raw_files, list):
                raise ValueError("memory backup file manifest is invalid")
            expected_names = {"manifest.json"}
            for item in raw_files:
                if not isinstance(item, dict):
                    raise ValueError("memory backup file entry is invalid")
                relative = _safe_relative(str(item.get("path", "")))
                archive_name = f"memory/{relative.as_posix()}"
                expected_names.add(archive_name)
                content = archive.read(archive_name)
                if len(content) != item.get("bytes") or hashlib.sha256(
                    content
                ).hexdigest() != item.get("sha256"):
                    raise ValueError(f"memory backup digest mismatch: {relative}")
                target = temporary.joinpath(*relative.parts)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _write_private_file(target, content)
            if set(names) != expected_names:
                raise ValueError("memory backup contains unmanifested entries")
        store_verification = SQLiteMemoryStore(temporary, read_only=True).verify()
        conversation_verification = verify_conversation_memory(temporary)
        if not store_verification.ok or not conversation_verification.ok:
            errors = (*store_verification.errors, *conversation_verification.errors)
            raise ValueError(
                f"restored memory failed verification: {'; '.join(errors)}"
            )
        os.replace(temporary, memory_directory)
    return memory_directory


def purge_memory_store(memory_directory: Path) -> bool:
    """Permanently remove the exact configured memory directory."""

    memory_directory = Path(memory_directory).expanduser()
    try:
        metadata = memory_directory.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("memory state path must be a real directory")
    if memory_directory.name != "memory":
        raise RuntimeError("refusing to purge an unexpected directory name")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise PermissionError("memory state directory is not owned by this user")
    shutil.rmtree(memory_directory)
    return True


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "" in path.parts:
        raise ValueError("memory backup contains an unsafe path")
    return path


def _write_private_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("could not restore memory backup file")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
