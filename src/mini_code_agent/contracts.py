from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from mini_code_agent.utils import DEFAULT_OUTPUT_LIMIT, truncate_text


class Redactor(Protocol):
    def redact_text(self, text: str) -> str: ...

    def redact_data(self, value: Any) -> Any: ...


class SnapshotLike(Protocol):
    files: dict[str, str]
    fingerprint: str

    def diff(self, other: "SnapshotLike") -> dict[str, list[str]]: ...


@dataclass
class ToolResult:
    tool: str
    output: str
    returncode: int
    duration_ms: int
    command: str = ""
    args: dict[str, Any] | None = None
    before_content: str | None = None
    after_content: str | None = None
    file_path: str = ""
    file_existed_before: bool | None = None
    before_hash: str = ""
    after_hash: str = ""
    exception_info: str = ""
    submitted: bool = False
    submission: str = ""
    approved: bool = True
    blocked: bool = False

    def to_observation(self) -> dict:
        data = {
            "returncode": self.returncode,
            "output": truncate_text(self.output, DEFAULT_OUTPUT_LIMIT),
        }
        if self.command:
            data["command"] = self.command
        if self.exception_info:
            data["exception_info"] = self.exception_info
        if self.submitted:
            data["submitted"] = True
            data["submission"] = self.submission
        if not self.approved:
            data["approved"] = False
        if self.blocked:
            data["blocked"] = True
        return data


@runtime_checkable
class ToolExecutor(Protocol):
    cwd: Path
    redactor: Redactor

    def execute_tool(self, name: str, args: dict[str, Any]) -> ToolResult: ...

    def workspace_fingerprint(
        self, *, ignore_paths: set[Path] | None = None
    ) -> SnapshotLike: ...

    def sandbox_status(self) -> str: ...


@dataclass
class ExecutedToolCall:
    name: str
    args: dict[str, Any]
    tool_call_id: str
    result: ToolResult


@dataclass
class ToolBatchOutcome:
    calls: list[ExecutedToolCall]
    submitted: bool
    submission: str
