from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mini_code_agent.contracts import ToolResult
from mini_code_agent.transaction import TransactionStore


READ_TOOLS = frozenset({"read_file"})
BROAD_READ_TOOLS = frozenset(
    {"bash", "list_files", "search_files", "git_diff", "run_tests"}
)
WRITE_TOOLS = frozenset({"bash", "write_file", "apply_patch", "replace_lines"})
BROAD_WRITE_TOOLS = frozenset({"bash", "run_tests"})


class TransactionExecutor:
    """Adapt a tool executor to a transaction's durable access log."""

    def __init__(self, executor: Any, store: TransactionStore, manifest: dict[str, Any]):
        self._executor = executor
        self._store = store
        self._manifest = manifest
        self.cwd = executor.cwd
        self.redactor = executor.redactor

    def workspace_fingerprint(self, *, ignore_paths: set[Path] | None = None):
        return self._executor.workspace_fingerprint(ignore_paths=ignore_paths)

    def sandbox_status(self) -> str:
        return self._executor.sandbox_status()

    def sandbox_probe(self) -> tuple[bool, str]:
        return self._executor.sandbox_probe()

    def execute_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        event: dict[str, Any] = {
            "sequence": len(self._manifest["access_log"]) + 1,
            "tool": name,
            "phase": "started",
            "at_ns": time.time_ns(),
        }
        if name in BROAD_READ_TOOLS:
            self._manifest["broad_read"] = True
        if name in BROAD_WRITE_TOOLS:
            self._manifest["broad_write"] = True
        if name in READ_TOOLS and args.get("path"):
            self._add_path("read_set", str(args["path"]))
        if name in WRITE_TOOLS and args.get("path"):
            self._add_path("write_set", str(args["path"]))
        self._manifest["access_log"].append(event)
        self._store.save(self._manifest)
        result = self._executor.execute_tool(name, args)
        if result.file_path and name in READ_TOOLS | WRITE_TOOLS:
            target = "write_set" if name in WRITE_TOOLS else "read_set"
            self._add_path(target, result.file_path)
        event["phase"] = "completed"
        event["returncode"] = result.returncode
        event["blocked"] = result.blocked
        self._store.save(self._manifest)
        return result

    def _add_path(self, field: str, raw_path: str) -> None:
        path = Path(raw_path)
        try:
            relative = (
                path.resolve().relative_to(self.cwd) if path.is_absolute() else path
            )
        except ValueError:
            return
        normalized = relative.as_posix()
        values = self._manifest[field]
        if normalized not in values:
            values.append(normalized)
            values.sort()
