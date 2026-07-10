from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


SKIP_DIRS = {".git", "__pycache__", ".mypy_cache", ".pytest_cache", "node_modules", ".venv", "venv"}


@dataclass
class WorkspaceSnapshot:
    root: Path
    files: dict[str, str]

    @classmethod
    def capture(cls, root: Path) -> "WorkspaceSnapshot":
        root = root.resolve()
        files = {}
        for path in sorted(root.rglob("*")):
            if _should_skip(path) or not path.is_file():
                continue
            files[str(path.relative_to(root))] = _hash_file(path)
        return cls(root=root, files=files)

    def diff(self, other: "WorkspaceSnapshot") -> dict[str, list[str]]:
        before = self.files
        after = other.files
        return {
            "created": sorted(set(after) - set(before)),
            "deleted": sorted(set(before) - set(after)),
            "modified": sorted(path for path in set(before) & set(after) if before[path] != after[path]),
        }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return "unreadable"
    return digest.hexdigest()


def _should_skip(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)
