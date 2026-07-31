from __future__ import annotations

import os
import re
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator, TextIO


class SecurityError(Exception):
    """Raised when a tool request violates the workspace safety policy."""


def _descriptor_relative_available() -> bool:
    supports_dir_fd = getattr(os, "supports_dir_fd", set())
    supports_follow_symlinks = getattr(os, "supports_follow_symlinks", set())
    return (
        os.name == "posix"
        and os.open in supports_dir_fd
        and os.stat in supports_dir_fd
        and os.stat in supports_follow_symlinks
        and os.unlink in supports_dir_fd
    )


class SafeWorkspace:
    def __init__(self, cwd: Path):
        self.cwd = cwd.resolve()
        if not self.cwd.is_dir():
            raise NotADirectoryError(f"workspace does not exist: {cwd}")
        self._use_descriptor_relative_io = _descriptor_relative_available()

    def resolve(self, path: str | Path) -> Path:
        raw_path = Path(path or ".")
        candidate = raw_path if raw_path.is_absolute() else self.cwd / raw_path
        # Normalize ``..`` lexically first.  Calling Path.resolve() alone would
        # silently accept an in-workspace symlink and make later checks racy.
        resolved = Path(os.path.abspath(candidate))
        try:
            relative = resolved.relative_to(self.cwd)
        except ValueError as exc:
            raise SecurityError(f"Path escapes workspace: {path}") from exc

        cursor = self.cwd
        for part in relative.parts:
            cursor = cursor / part
            try:
                metadata = cursor.lstat()
            except FileNotFoundError:
                # A not-yet-created leaf is valid for a structured write.
                continue
            if stat.S_ISLNK(metadata.st_mode):
                raise SecurityError(f"Symbolic links are not allowed in workspace paths: {path}")
        return resolved

    def iter_files(
        self,
        path: str | Path = ".",
        *,
        skip_dir_names: set[str] | frozenset[str] | None = None,
    ) -> Iterator[Path]:
        """Yield regular files without following symlinks.

        Entries are sorted one directory at a time (rather than materializing a
        whole-repository rglob), allowing callers to stop as soon as their own
        output limit is reached.
        """

        root = self.resolve(path)
        try:
            root_metadata = root.lstat()
        except FileNotFoundError:
            raise FileNotFoundError(f"path does not exist: {path}") from None
        if stat.S_ISREG(root_metadata.st_mode):
            yield root
            return
        if not stat.S_ISDIR(root_metadata.st_mode):
            return

        skipped = skip_dir_names or set()
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name, reverse=True)
            except OSError:
                continue
            child_directories: list[Path] = []
            files: list[Path] = []
            for entry in entries:
                if entry.is_symlink():
                    continue
                candidate = Path(entry.path)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in skipped:
                            continue
                        child_directories.append(candidate)
                    elif entry.is_file(follow_symlinks=False):
                        files.append(candidate)
                except OSError:
                    continue
            # ``stack`` is LIFO; reverse-sorted children give ascending output.
            stack.extend(child_directories)
            for candidate in reversed(files):
                yield candidate

    def iter_entries(
        self,
        path: str | Path = ".",
        *,
        skip_dir_names: set[str] | frozenset[str] | None = None,
    ) -> Iterator[Path]:
        """Yield directories, regular files, and symlinks without following links."""

        root = self.resolve(path)
        skipped = skip_dir_names or set()
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                entries = sorted(
                    os.scandir(directory), key=lambda item: item.name, reverse=True
                )
            except OSError:
                continue
            child_directories: list[Path] = []
            yielded: list[Path] = []
            for entry in entries:
                candidate = Path(entry.path)
                try:
                    if entry.is_symlink():
                        yielded.append(candidate)
                    elif entry.is_dir(follow_symlinks=False):
                        if entry.name in skipped:
                            continue
                        yielded.append(candidate)
                        child_directories.append(candidate)
                    elif entry.is_file(follow_symlinks=False):
                        yielded.append(candidate)
                except OSError:
                    continue
            stack.extend(child_directories)
            for candidate in reversed(yielded):
                yield candidate

    def read_bytes(self, path: str | Path, *, max_bytes: int | None = None) -> bytes:
        resolved = self.resolve(path)
        fd = self._open_file_fd(resolved, os.O_RDONLY)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise SecurityError(f"Not a regular file: {path}")
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                return handle.read() if max_bytes is None else handle.read(max_bytes)
        finally:
            if fd >= 0:
                os.close(fd)

    def read_text(
        self,
        path: str | Path,
        *,
        encoding: str = "utf-8",
        errors: str = "strict",
        max_chars: int | None = None,
    ) -> str:
        resolved = self.resolve(path)
        fd = self._open_file_fd(resolved, os.O_RDONLY)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise SecurityError(f"Not a regular file: {path}")
            with os.fdopen(fd, "r", encoding=encoding, errors=errors, newline="") as handle:
                fd = -1
                return handle.read() if max_chars is None else handle.read(max_chars)
        finally:
            if fd >= 0:
                os.close(fd)

    @contextmanager
    def open_binary(self, path: str | Path) -> Iterator[BinaryIO]:
        resolved = self.resolve(path)
        fd = self._open_file_fd(resolved, os.O_RDONLY)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise SecurityError(f"Not a regular file: {path}")
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                yield handle
        finally:
            if fd >= 0:
                os.close(fd)

    @contextmanager
    def open_text(
        self,
        path: str | Path,
        *,
        encoding: str = "utf-8",
        errors: str = "strict",
    ) -> Iterator[TextIO]:
        resolved = self.resolve(path)
        fd = self._open_file_fd(resolved, os.O_RDONLY)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise SecurityError(f"Not a regular file: {path}")
            with os.fdopen(fd, "r", encoding=encoding, errors=errors, newline="") as handle:
                fd = -1
                yield handle
        finally:
            if fd >= 0:
                os.close(fd)

    def atomic_write_text(
        self,
        path: str | Path,
        text: str,
        *,
        encoding: str = "utf-8",
    ) -> Path:
        """Safely replace a regular workspace file via directory descriptors."""

        resolved = self.resolve(path)
        if not self._use_descriptor_relative_io:
            return self._atomic_write_fallback(resolved, text, encoding=encoding)
        relative = resolved.relative_to(self.cwd)
        if not relative.parts:
            raise IsADirectoryError("cannot write the workspace directory")
        parent_fd = self._open_parent_fd(relative.parts[:-1], create=True)
        filename = relative.parts[-1]
        temporary = ""
        fd = -1
        try:
            try:
                current = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if current is not None:
                if stat.S_ISLNK(current.st_mode):
                    raise SecurityError(f"Refusing to replace symbolic link: {path}")
                if not stat.S_ISREG(current.st_mode):
                    raise SecurityError(f"Refusing to replace non-regular file: {path}")
            mode = stat.S_IMODE(current.st_mode) if current is not None else 0o644
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            for _ in range(128):
                temporary = f".{filename}.{secrets.token_hex(12)}.tmp"
                try:
                    fd = os.open(temporary, flags, mode, dir_fd=parent_fd)
                    break
                except FileExistsError:
                    continue
            if fd < 0:
                raise FileExistsError(f"could not allocate a safe temporary file for {path}")
            if hasattr(os, "fchmod"):
                os.fchmod(fd, mode)
            with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
                fd = -1
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, filename, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            temporary = ""
            os.fsync(parent_fd)
            return resolved
        finally:
            if fd >= 0:
                os.close(fd)
            if temporary:
                try:
                    os.unlink(temporary, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            os.close(parent_fd)

    def unlink_file(self, path: str | Path) -> None:
        resolved = self.resolve(path)
        if not self._use_descriptor_relative_io:
            metadata = resolved.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise SecurityError(f"Refusing to unlink non-regular file: {path}")
            resolved.unlink()
            return
        relative = resolved.relative_to(self.cwd)
        if not relative.parts:
            raise IsADirectoryError("cannot unlink the workspace directory")
        parent_fd = self._open_parent_fd(relative.parts[:-1], create=False)
        try:
            metadata = os.stat(relative.parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise SecurityError(f"Refusing to unlink non-regular file: {path}")
            os.unlink(relative.parts[-1], dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    def is_probably_text_file(self, path: str | Path, *, sample_size: int = 2048) -> bool:
        try:
            return b"\x00" not in self.read_bytes(path, max_bytes=sample_size)
        except (OSError, SecurityError):
            return False

    def _open_file_fd(self, path: Path, flags: int) -> int:
        if not self._use_descriptor_relative_io:
            descriptor = os.open(path, flags)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise SecurityError(f"Not a regular file: {path}")
            return descriptor
        relative = path.relative_to(self.cwd)
        if not relative.parts:
            raise IsADirectoryError(path)
        parent_fd = self._open_parent_fd(relative.parts[:-1], create=False)
        final_flags = flags
        if hasattr(os, "O_NOFOLLOW"):
            final_flags |= os.O_NOFOLLOW
        try:
            return os.open(relative.parts[-1], final_flags, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)

    def _open_parent_fd(self, parts: tuple[str, ...], *, create: bool) -> int:
        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        fd = os.open(self.cwd, directory_flags)
        try:
            for part in parts:
                try:
                    child_fd = os.open(part, directory_flags, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, 0o755, dir_fd=fd)
                    child_fd = os.open(part, directory_flags, dir_fd=fd)
                os.close(fd)
                fd = child_fd
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _atomic_write_fallback(
        self, resolved: Path, text: str, *, encoding: str
    ) -> Path:
        """Best available atomic replacement where descriptor-relative I/O is absent."""

        resolved.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        # Re-run containment and symlink checks after creating parent directories.
        resolved = self.resolve(resolved)
        try:
            current = resolved.lstat()
        except FileNotFoundError:
            current = None
        if current is not None and (
            stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode)
        ):
            raise SecurityError(f"Refusing to replace non-regular file: {resolved}")
        mode = stat.S_IMODE(current.st_mode) if current is not None else 0o644
        temporary: Path | None = None
        descriptor = -1
        try:
            for _ in range(128):
                temporary = resolved.parent / (
                    f".{resolved.name}.{secrets.token_hex(12)}.tmp"
                )
                try:
                    descriptor = os.open(
                        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode
                    )
                    break
                except FileExistsError:
                    continue
            if descriptor < 0 or temporary is None:
                raise FileExistsError(
                    f"could not allocate a safe temporary file for {resolved}"
                )
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "w", encoding=encoding, newline="") as handle:
                descriptor = -1
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, resolved)
            temporary = None
            return resolved
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class SecretRedactor:
    SECRET_ENV_NAMES = {
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "MCA_API_KEY",
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
    }
    SECRET_PATTERNS = [
        re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_\-]{8,}"),
        re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
        re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
        re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
        re.compile(r"xox[baprs]-[A-Za-z0-9\-]{20,}"),
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"Bearer\s+[A-Za-z0-9._\-]{12,}", re.IGNORECASE),
    ]
    SECRET_NAME_FRAGMENTS = (
        "API_KEY",
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "PASSWD",
        "CREDENTIAL",
        "PRIVATE_KEY",
        "ACCESS_KEY",
        "AUTHORIZATION",
    )

    def __init__(self, extra_secrets: list[str] | None = None):
        env_secrets = [secret for secret in os.getenv("MCA_REDACT", "").split(",") if secret]
        self.secrets = [
            value
            for value in [
                env_value
                for env_name, env_value in os.environ.items()
                if self.is_secret_env_name(env_name)
            ]
            + env_secrets
            + (extra_secrets or [])
            if value and len(value) >= 8
        ]

    @classmethod
    def is_secret_env_name(cls, name: str) -> bool:
        upper = name.upper()
        return upper in cls.SECRET_ENV_NAMES or any(
            fragment in upper for fragment in cls.SECRET_NAME_FRAGMENTS
        )

    def redact_text(self, text: str) -> str:
        redacted = text
        for secret in self.secrets:
            redacted = redacted.replace(secret, "[REDACTED_SECRET]")
        for pattern in self.SECRET_PATTERNS:
            redacted = pattern.sub("[REDACTED_SECRET]", redacted)
        return redacted

    def redact_data(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, list):
            return [self.redact_data(item) for item in value]
        if isinstance(value, dict):
            return {key: self.redact_data(item) for key, item in value.items()}
        return value


def load_env_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"env file does not exist: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise SecurityError(f"env file is not a regular file: {path}")
        if (
            hasattr(os, "getuid")
            and hasattr(os, "fchmod")
            and metadata.st_uid == os.getuid()
        ):
            # API credential files should not inherit a permissive umask.
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            lines = handle.read().splitlines()
    finally:
        if fd >= 0:
            os.close(fd)
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"invalid environment variable name in {path}: {key!r}")
        if key not in os.environ:
            os.environ[key] = value


def is_probably_text_file(path: Path, *, sample_size: int = 2048) -> bool:
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(sample_size)
    except OSError:
        return False
    return b"\x00" not in data
