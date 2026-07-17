from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from mini_code_agent.contracts import (
    ExecutedToolCall,
    ToolBatchOutcome,
    ToolExecutor,
    ToolResult,
)
from mini_code_agent.workspace import WorkspaceSnapshot


STRUCTURED_EDIT_TOOLS = frozenset({"write_file", "apply_patch", "replace_lines"})
POTENTIALLY_MUTATING_TOOLS = STRUCTURED_EDIT_TOOLS | frozenset({"bash", "run_tests"})


def _fingerprint_files(files: dict[str, str]) -> str:
    payload = json.dumps(files, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def capture_workspace_fingerprint(
    executor: ToolExecutor, *, ignore_paths: set[Path] | None = None
) -> str:
    """Return a deterministic fingerprint for every relevant file in the workspace.

    Keep this adapter local to the agent layer so a future executor-native fingerprint
    can be adopted without weakening the verification gate on older executors.
    """

    native = getattr(executor, "workspace_fingerprint", None)
    if callable(native):
        try:
            value = native(ignore_paths=ignore_paths or set())
        except TypeError:
            value = native()
        if isinstance(value, str):
            return value
        files = getattr(value, "files", None)
        if isinstance(files, dict):
            return _fingerprint_files(files)
    snapshot = WorkspaceSnapshot.capture(executor.cwd, ignore_paths=ignore_paths or set())
    return _fingerprint_files(snapshot.files)


@dataclass
class VerificationGate:
    """Bind a passing authoritative test run to one exact workspace state."""

    baseline_fingerprint: str
    current_fingerprint: str
    verified_fingerprint: str = ""
    status: str = "not_required"
    has_changes: bool = False
    require_verification: bool = False

    @classmethod
    def create(
        cls, fingerprint: str, *, require_verification: bool = False
    ) -> "VerificationGate":
        return cls(
            baseline_fingerprint=fingerprint,
            current_fingerprint=fingerprint,
            status="required" if require_verification else "not_required",
            require_verification=require_verification,
        )

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> "VerificationGate":
        return cls(
            baseline_fingerprint=state["baseline_fingerprint"],
            current_fingerprint=state["current_fingerprint"],
            verified_fingerprint=state.get("verified_fingerprint", ""),
            status=state.get("verification_status", "not_required"),
            has_changes=state.get("has_changes", False),
            require_verification=state.get("require_verification", False),
        )

    def sync(self, fingerprint: str) -> None:
        self.current_fingerprint = fingerprint
        self.has_changes = fingerprint != self.baseline_fingerprint
        if self.status == "failed":
            return
        if self.verified_fingerprint and fingerprint == self.verified_fingerprint:
            self.status = "passed"
        elif self.has_changes or self.require_verification:
            self.status = "required"
        else:
            self.status = "not_required"

    def record_test(self, *, passed: bool, fingerprint: str) -> None:
        self.current_fingerprint = fingerprint
        self.has_changes = fingerprint != self.baseline_fingerprint
        if passed:
            self.verified_fingerprint = fingerprint
            self.status = "passed"
        else:
            self.verified_fingerprint = ""
            self.status = "failed"

    def submission_error(self, fingerprint: str) -> tuple[str, str] | None:
        self.sync(fingerprint)
        if self.status == "failed":
            return (
                "VerificationFailed",
                "Submission blocked: the latest authoritative run_tests call failed.",
            )
        if (
            (self.has_changes or self.require_verification)
            and self.verified_fingerprint != fingerprint
        ):
            return (
                "VerificationRequired",
                "Submission blocked: the current workspace must match a passing run_tests snapshot.",
            )
        return None

    def accept_submission(self) -> None:
        was_verified = (
            self.status == "passed"
            and bool(self.verified_fingerprint)
            and self.verified_fingerprint == self.current_fingerprint
        )
        self.baseline_fingerprint = self.current_fingerprint
        self.verified_fingerprint = self.current_fingerprint if was_verified else ""
        self.has_changes = False
        self.require_verification = False
        self.status = "passed" if was_verified else "not_required"

    def to_state(self) -> dict[str, Any]:
        return {
            "has_changes": self.has_changes,
            "verification_status": self.status,
            "baseline_fingerprint": self.baseline_fingerprint,
            "current_fingerprint": self.current_fingerprint,
            "verified_fingerprint": self.verified_fingerprint,
            "require_verification": self.require_verification,
        }


def _blocked_submission(exception_info: str, output: str) -> ToolResult:
    return ToolResult(
        tool="submit",
        output=output,
        returncode=-1,
        duration_ms=0,
        exception_info=exception_info,
        blocked=True,
    )


def execute_tool_batch(
    executor: ToolExecutor,
    tool_calls: Sequence[dict[str, Any]],
    gate: VerificationGate,
    *,
    ignore_paths: set[Path] | None = None,
    on_structured_edit: Callable[[ToolResult], None] | None = None,
) -> ToolBatchOutcome:
    """Execute a model tool-call batch without leaving unmatched ToolMessages.

    Submit calls are deliberately evaluated after every non-submit call in the same
    assistant message. This prevents a valid early submit from hiding a later edit,
    while callers can still append results in the model's original call order.
    """

    normalized: list[tuple[str, dict[str, Any], str]] = []
    for index, call in enumerate(tool_calls):
        raw_args = call.get("args", {})
        args = raw_args if isinstance(raw_args, dict) else {}
        normalized.append(
            (
                str(call.get("name", "")),
                args,
                str(call.get("id") or f"tool-{index + 1}"),
            )
        )

    results: list[ToolResult | None] = [None] * len(normalized)
    ignored = ignore_paths or set()
    try:
        current_fingerprint = capture_workspace_fingerprint(
            executor, ignore_paths=ignored
        )
        gate.sync(current_fingerprint)
    except Exception as exc:
        current_fingerprint = gate.current_fingerprint
        gate.verified_fingerprint = ""
        gate.status = "failed"
        initial_capture_error = f"{type(exc).__name__}: {exc}"
    else:
        initial_capture_error = ""

    for index, (name, args, _tool_call_id) in enumerate(normalized):
        if name == "submit":
            continue
        if initial_capture_error:
            results[index] = _tool_exception_result(
                name, "WorkspaceFingerprintError", initial_capture_error
            )
            continue
        # Only the command configured by the user/CLI is authoritative. A model-
        # supplied command (including `true`) must never mint a verification token.
        effective_args = {} if name == "run_tests" else args
        try:
            result = executor.execute_tool(name, effective_args)
        except Exception as exc:
            result = _tool_exception_result(
                name, type(exc).__name__, f"{type(exc).__name__}: {exc}"
            )
        if result.submitted:
            # Older executors recognize a shell sentinel. It is intentionally not a
            # completion path: only the structured submit tool can end a task.
            result = ToolResult(
                tool=name,
                command=result.command,
                output="Legacy shell submission is disabled; call the submit tool.",
                returncode=-1,
                duration_ms=result.duration_ms,
                args=result.args,
                exception_info="LegacySubmissionDisabled",
                blocked=True,
            )
        if result.returncode == 0 and name in STRUCTURED_EDIT_TOOLS and on_structured_edit:
            try:
                on_structured_edit(result)
            except Exception as exc:
                result = _tool_exception_result(
                    name,
                    "UndoRecordError",
                    f"The edit succeeded but its undo record could not be saved: {exc}",
                )
        if (
            name in POTENTIALLY_MUTATING_TOOLS
            and result.returncode == 0
            and result.approved
            and result.exception_info != "ReadOnlyChatMode"
        ):
            gate.require_verification = True
        if name in POTENTIALLY_MUTATING_TOOLS:
            try:
                current_fingerprint = capture_workspace_fingerprint(
                    executor, ignore_paths=ignored
                )
            except Exception as exc:
                result = _tool_exception_result(
                    name,
                    "WorkspaceFingerprintError",
                    f"Workspace fingerprint failed after tool execution: {exc}",
                )
                gate.verified_fingerprint = ""
                gate.status = "failed"
                results[index] = result
                continue
        if name == "run_tests":
            gate.record_test(
                passed=result.returncode == 0, fingerprint=current_fingerprint
            )
        else:
            gate.sync(current_fingerprint)
        results[index] = result

    # Defer all submit decisions until the rest of the assistant's batch is settled.
    submit_indices = [
        index for index, (name, _args, _call_id) in enumerate(normalized) if name == "submit"
    ]
    submission_error: tuple[str, str] | None = None
    if submit_indices:
        try:
            current_fingerprint = capture_workspace_fingerprint(
                executor, ignore_paths=ignored
            )
            submission_error = gate.submission_error(current_fingerprint)
        except Exception as exc:
            submission_error = (
                "WorkspaceFingerprintError",
                f"Submission blocked because workspace fingerprinting failed: {exc}",
            )
    for index, (name, args, _tool_call_id) in enumerate(normalized):
        if name != "submit":
            continue
        try:
            results[index] = (
                _blocked_submission(*submission_error)
                if submission_error
                else executor.execute_tool("submit", args)
            )
        except Exception as exc:
            results[index] = _blocked_submission(
                "WorkspaceFingerprintError",
                f"Submission blocked because workspace fingerprinting failed: {exc}",
            )

    executed = [
        ExecutedToolCall(name=name, args=args, tool_call_id=tool_call_id, result=result)
        for (name, args, tool_call_id), result in zip(normalized, results)
        if result is not None
    ]
    accepted = [call.result for call in executed if call.result.submitted]
    return ToolBatchOutcome(
        calls=executed,
        submitted=bool(accepted),
        submission=accepted[-1].submission if accepted else "",
    )


def _tool_exception_result(name: str, exception_info: str, output: str) -> ToolResult:
    return ToolResult(
        tool=name,
        output=output,
        returncode=-1,
        duration_ms=0,
        exception_info=exception_info,
        blocked=True,
    )
