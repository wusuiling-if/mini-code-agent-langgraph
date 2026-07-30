from __future__ import annotations

import json
import os
import secrets
import shlex
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langchain_core.messages import BaseMessage


DEFAULT_OUTPUT_LIMIT = 12000
MAX_STATE_FILE_BYTES = 256 * 1024 * 1024


def command_from_argv(argv: list[str], *, platform_name: str | None = None) -> str:
    """Serialize trusted argv for the platform's command interpreter."""

    if not argv or any(not isinstance(item, str) for item in argv):
        raise ValueError("argv must be a non-empty list of strings")
    platform_name = os.name if platform_name is None else platform_name
    if platform_name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def truncate_text(text: str, limit: int = DEFAULT_OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return f"{text[:head]}\n\n...[elided {len(text) - limit} chars]...\n\n{text[-tail:]}"


def atomic_write_text(
    path: Path,
    text: str,
    *,
    mode: int | None = None,
    encoding: str = "utf-8",
    max_bytes: int | None = None,
) -> None:
    """Atomically replace *path* without following a target symlink.

    A random, exclusively-created file in the destination directory prevents a
    predictable ``.tmp`` symlink from redirecting state or source writes.  The
    replacement itself changes the directory entry, so an attacker cannot make
    us follow a symlink at the final path between the check and ``os.replace``.
    """

    if max_bytes is not None and len(text.encode(encoding)) > max_bytes:
        raise ValueError(f"state file exceeds the {max_bytes}-byte safety limit")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = path.lstat()
    except FileNotFoundError:
        current = None
    if current is not None and stat.S_ISLNK(current.st_mode):
        raise OSError(f"refusing to replace symlink: {path}")
    if current is not None and not stat.S_ISREG(current.st_mode):
        raise OSError(f"refusing to replace non-regular file: {path}")

    if mode is None:
        mode = stat.S_IMODE(current.st_mode) if current is not None else 0o644
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    fd = -1
    temporary: Path | None = None
    for _ in range(128):
        temporary = path.parent / f".{path.name}.{secrets.token_hex(12)}.tmp"
        try:
            fd = os.open(temporary, flags, mode)
            break
        except FileExistsError:
            continue
    if fd < 0 or temporary is None:
        raise FileExistsError(f"could not allocate a safe temporary file for {path}")

    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary.exists():
            temporary.unlink()


def write_json(path: Path, data: dict[str, Any]) -> None:
    # Trajectories may contain prompts, paths, and redacted operational logs.
    # Treat them as private state even when the caller chooses a shared umask.
    atomic_write_text(
        path,
        json.dumps(data, indent=2, ensure_ascii=False),
        mode=0o600,
        max_bytes=MAX_STATE_FILE_BYTES,
    )


def serialize_messages(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    from langchain_core.messages import messages_to_dict

    return messages_to_dict(messages)
