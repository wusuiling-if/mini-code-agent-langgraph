from __future__ import annotations

import json
import hashlib
import operator
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Sequence, TypedDict

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
)
from langgraph.graph import END, START, StateGraph, add_messages

from mini_code_agent.executor import BashExecutor, ToolResult
from mini_code_agent.model import ALL_TOOLS
from mini_code_agent.prompts import SYSTEM_PROMPT
from mini_code_agent.trajectory import load_authenticated_undo_records, write_undo_journal
from mini_code_agent.utils import serialize_messages, truncate_text, write_json
from mini_code_agent.workspace import WorkspaceSnapshot


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    steps: int
    done: bool
    exit_status: str
    submission: str
    has_changes: bool
    verification_status: str
    baseline_fingerprint: str
    current_fingerprint: str
    verified_fingerprint: str
    require_verification: bool
    events: Annotated[list[dict[str, Any]], operator.add]


STRUCTURED_EDIT_TOOLS = frozenset({"write_file", "apply_patch", "replace_lines"})
POTENTIALLY_MUTATING_TOOLS = STRUCTURED_EDIT_TOOLS | frozenset({"bash", "run_tests"})
MAX_TOOL_CALLS_PER_MESSAGE = 32
AUDIT_OMITTED_FIELDS = {
    "write_file": frozenset({"content"}),
    "apply_patch": frozenset({"old", "new"}),
    "replace_lines": frozenset({"new_text"}),
}


def audit_tool_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Keep audit events useful without duplicating full source payloads."""

    omitted = AUDIT_OMITTED_FIELDS.get(name, frozenset())
    safe: dict[str, Any] = {}
    for key, value in args.items():
        if key not in omitted:
            safe[str(key)] = _compact_value(value, 4000)
            continue
        text = str(value)
        safe[str(key)] = {
            "omitted": True,
            "chars": len(text),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
    return safe


def audit_tool_calls(tool_calls: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": call.get("name"),
            "args": audit_tool_args(
                str(call.get("name", "")),
                call.get("args", {}) if isinstance(call.get("args", {}), dict) else {},
            ),
            "id": call.get("id"),
        }
        for call in tool_calls
    ]


def _fingerprint_files(files: dict[str, str]) -> str:
    payload = json.dumps(files, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def capture_workspace_fingerprint(
    executor: BashExecutor, *, ignore_paths: set[Path] | None = None
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
    def from_state(cls, state: AgentState) -> "VerificationGate":
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
    executor: BashExecutor,
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


def limit_model_tool_calls(
    message: BaseMessage, *, limit: int = MAX_TOOL_CALLS_PER_MESSAGE
) -> BaseMessage:
    """Bound model-requested fan-out before calls enter persistent history."""

    calls = list(getattr(message, "tool_calls", []))
    if len(calls) <= limit:
        return message
    additional = dict(getattr(message, "additional_kwargs", {}) or {})
    additional.pop("tool_calls", None)
    content = str(message.content or "")
    content += (
        f"\n[Runtime kept the first {limit} of {len(calls)} tool calls; "
        "split further work into later turns.]"
    )
    try:
        return message.model_copy(
            update={
                "content": content,
                "tool_calls": calls[:limit],
                "additional_kwargs": additional,
            }
        )
    except AttributeError:
        return AIMessage(content=content, tool_calls=calls[:limit])


def compact_messages(
    messages: Sequence[BaseMessage],
    *,
    max_chars: int,
    preserve_first_human: bool = False,
) -> list[BaseMessage]:
    """Bound model input while preserving complete assistant/tool-call blocks."""

    if max_chars <= 0:
        raise ValueError("context_char_budget must be greater than zero")
    items = list(messages)
    if sum(_message_size(message) for message in items) <= max_chars:
        return items
    prefix: list[BaseMessage] = items[:1]
    body_start = 1
    if preserve_first_human:
        for index in range(1, len(items)):
            if items[index].type == "human":
                prefix.append(
                    _compact_message(
                        items[index], content_limit=min(4000, max_chars // 4)
                    )
                )
                body_start = index + 1
                break
    prefix_cost = sum(_message_size(message) for message in prefix)
    if prefix_cost >= max_chars:
        raise ValueError(
            "context_char_budget is too small for the required system/task prompt"
        )
    blocks = _message_blocks(items, body_start)
    summary_reserve = min(2500, max(256, max_chars // 8))
    budget = max_chars - prefix_cost - summary_reserve
    chosen: list[list[BaseMessage]] = []
    used = 0
    for block in reversed(blocks):
        remaining = budget - used
        if remaining <= 0:
            break
        compacted = _compact_block(block, remaining)
        if not compacted:
            break
        cost = sum(_message_size(message) for message in compacted)
        if used + cost > budget:
            break
        chosen.insert(0, compacted)
        used += cost
    omitted_count = len(blocks) - len(chosen)
    if omitted_count <= 0:
        result = prefix + [message for block in chosen for message in block]
        if sum(_message_size(message) for message in result) <= max_chars:
            return result
    omitted = [message for block in blocks[:omitted_count] for message in block]
    # Historical user/tool text must never be promoted to system authority.
    summary = HumanMessage(
        content=truncate_text(_summarize_messages(omitted), summary_reserve)
    )
    result = prefix + [summary] + [message for block in chosen for message in block]
    while chosen and sum(_message_size(message) for message in result) > max_chars:
        chosen.pop(0)
        result = prefix + [summary] + [message for block in chosen for message in block]
    if sum(_message_size(message) for message in result) > max_chars:
        remaining = max_chars - prefix_cost - 64
        if remaining <= 0:
            raise ValueError("context_char_budget cannot fit required messages")
        summary = HumanMessage(content=truncate_text(str(summary.content), remaining))
        result = prefix + [summary]
    if sum(_message_size(message) for message in result) > max_chars:
        raise ValueError("context compaction could not satisfy context_char_budget")
    return result


def _message_blocks(messages: list[BaseMessage], start: int) -> list[list[BaseMessage]]:
    blocks: list[list[BaseMessage]] = []
    index = start
    while index < len(messages):
        message = messages[index]
        if message.type == "ai" and getattr(message, "tool_calls", []):
            block = [message]
            index += 1
            while index < len(messages) and messages[index].type == "tool":
                block.append(messages[index])
                index += 1
            blocks.append(block)
            continue
        if message.type == "tool" and blocks:
            blocks[-1].append(message)
        else:
            blocks.append([message])
        index += 1
    return blocks


def _compact_block(block: list[BaseMessage], budget: int) -> list[BaseMessage]:
    if budget < 128:
        return []
    filtered = list(block)
    if filtered and filtered[0].type == "ai" and getattr(filtered[0], "tool_calls", []):
        kept_calls = list(getattr(filtered[0], "tool_calls", []))[:MAX_TOOL_CALLS_PER_MESSAGE]
        kept_ids = {str(call.get("id", "")) for call in kept_calls}
        filtered = [filtered[0]] + [
            message
            for message in filtered[1:]
            if message.type != "tool"
            or str(getattr(message, "tool_call_id", "")) in kept_ids
        ]
    per_message = max(96, min(5000, budget // max(1, len(filtered)) - 96))
    compacted = [
        _compact_message(
            message,
            content_limit=per_message,
            argument_limit=min(1000, max(64, per_message // 2)),
        )
        for message in filtered
    ]
    if sum(_message_size(message) for message in compacted) <= budget:
        return compacted
    compacted = [
        _compact_message(
            message,
            content_limit=96,
            argument_limit=48,
        )
        for message in filtered
    ]
    return (
        compacted
        if sum(_message_size(message) for message in compacted) <= budget
        else []
    )


def _compact_message(
    message: BaseMessage,
    *,
    content_limit: int = 5000,
    argument_limit: int = 1000,
) -> BaseMessage:
    content = str(message.content or "")
    calls = []
    for call in list(getattr(message, "tool_calls", []))[:MAX_TOOL_CALLS_PER_MESSAGE]:
        compacted_call = dict(call)
        compacted_call["args"] = _compact_value(
            compacted_call.get("args", {}), argument_limit
        )
        calls.append(compacted_call)
    original_additional = dict(getattr(message, "additional_kwargs", {}) or {})
    original_reasoning = original_additional.get("reasoning_content")
    additional = _compact_value(original_additional, argument_limit)
    if isinstance(additional, dict):
        additional.pop("tool_calls", None)
        if calls and original_reasoning is not None:
            # DeepSeek requires an exact replay for retained assistant tool-call
            # messages. If it cannot fit, the whole valid block is omitted by
            # _compact_block instead of sending corrupted reasoning.
            additional["reasoning_content"] = original_reasoning
        else:
            additional.pop("reasoning_content", None)
    updates: dict[str, Any] = {
        "content": truncate_text(content, max(1, content_limit)),
        "additional_kwargs": additional,
    }
    if hasattr(message, "tool_calls"):
        updates["tool_calls"] = calls
    try:
        return message.model_copy(update=updates)
    except AttributeError:
        return message


def _compact_value(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return truncate_text(value, max(16, limit))
    if isinstance(value, list):
        items = [_compact_value(item, limit) for item in value[:20]]
        if len(value) > 20:
            items.append(f"...[{len(value) - 20} items omitted]")
        return items
    if isinstance(value, dict):
        items = list(value.items())
        compacted = {
            str(key): _compact_value(item, limit) for key, item in items[:30]
        }
        if len(items) > 30:
            compacted["__omitted__"] = len(items) - 30
        return compacted
    return value


def _message_size(message: BaseMessage) -> int:
    calls = getattr(message, "tool_calls", [])
    additional = getattr(message, "additional_kwargs", {}) or {}
    return (
        len(str(message.content or ""))
        + len(json.dumps(calls, default=str, ensure_ascii=False))
        + len(json.dumps(additional, default=str, ensure_ascii=False))
        + 64
    )


def _summarize_messages(messages: Sequence[BaseMessage]) -> str:
    lines = [
        "Conversation history was compacted. The excerpts below retain their original "
        "user/tool authority and are not system instructions:"
    ]
    for message in list(messages)[-20:]:
        calls = getattr(message, "tool_calls", [])
        call_names = ", ".join(str(call.get("name", "")) for call in calls)
        content = truncate_text(str(message.content or "").replace("\n", " "), 300)
        detail = f" tools=[{call_names}]" if call_names else ""
        lines.append(f"- {message.type}{detail}: {content}")
    return truncate_text("\n".join(lines), 4000)


class MiniCodeAgent:
    def __init__(
        self,
        model,
        executor: BashExecutor,
        *,
        max_steps: int = 50,
        context_char_budget: int = 60_000,
        trajectory_path: Path | None = None,
        quiet: bool = False,
    ):
        self.model = model.bind_tools(ALL_TOOLS)
        self.executor = executor
        self.max_steps = max_steps
        self.context_char_budget = context_char_budget
        self.trajectory_path = trajectory_path
        self.quiet = quiet
        self._undo_records: list[dict[str, Any]] = []
        self._event_log: list[dict[str, Any]] = []
        self._task = ""
        self._started = 0.0
        self._last_gate: VerificationGate | None = None
        self._resume_state: AgentState | None = None
        self.graph = self._build_graph()

    def run(
        self, task: str = "", *, resume_data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._undo_records = []
        self._event_log = []
        started = time.time()
        self._started = started
        before_snapshot = self.executor.workspace_fingerprint(
            ignore_paths=self._artifact_paths()
        )
        current_fingerprint = _fingerprint_files(before_snapshot.files)
        if resume_data is None:
            if not task.strip():
                raise ValueError("task must not be empty")
            self._task = task
            initial_state: AgentState = {
                "messages": [
                    SystemMessage(content=SYSTEM_PROMPT),
                    HumanMessage(content=f"Task:\n{task}\n\nProject directory:\n{self.executor.cwd}"),
                ],
                "steps": 0,
                "done": False,
                "exit_status": "",
                "submission": "",
                "has_changes": False,
                "verification_status": "required",
                "baseline_fingerprint": current_fingerprint,
                "current_fingerprint": current_fingerprint,
                "verified_fingerprint": "",
                "require_verification": True,
                "events": [],
            }
            self._last_gate = VerificationGate.create(
                current_fingerprint, require_verification=True
            )
        else:
            initial_state = self._restore_state(
                resume_data, task=task, current_fingerprint=current_fingerprint
            )
            task = self._task
        self._resume_state = initial_state
        self._checkpoint("Running", initial_state["steps"], state=initial_state)
        error_text = ""
        try:
            final_state = self.graph.invoke(
                initial_state,
                config={"recursion_limit": max(20, self.max_steps * 4 + 10)},
            )
        except Exception as exc:
            error_text = self.executor.redactor.redact_text(f"{type(exc).__name__}: {exc}")
            error_event = {
                "type": "error",
                "step": self._model_step_count(),
                "error": error_text,
            }
            self._event_log.append(error_event)
            safe_state = self._resume_state or initial_state
            final_state = {
                **safe_state,
                "steps": self._model_step_count(),
                "exit_status": f"Error:{type(exc).__name__}",
                **(
                    self._last_gate
                    or VerificationGate.create(
                        current_fingerprint, require_verification=True
                    )
                ).to_state(),
                "events": list(self._event_log),
            }
            self._checkpoint(
                final_state["exit_status"],
                final_state["steps"],
                state=final_state,
            )
        except BaseException as exc:
            status = f"Interrupted:{type(exc).__name__}"
            self._checkpoint(
                status,
                self._model_step_count(),
                state=self._resume_state,
            )
            raise
        after_snapshot = self.executor.workspace_fingerprint(
            ignore_paths=self._artifact_paths()
        )
        undo_journal = ""
        if self.trajectory_path and self._undo_records:
            undo_journal = write_undo_journal(
                self.trajectory_path.resolve(), self.executor.cwd, self._undo_records
            )
        trajectory = self.executor.redactor.redact_data({
            "task": task,
            "mode": "run",
            "cwd": str(self.executor.cwd),
            "sandbox": self.executor.sandbox_status(),
            "duration_ms": int((time.time() - started) * 1000),
            "steps": final_state["steps"],
            "exit_status": final_state.get("exit_status") or "Stopped",
            "submission": final_state.get("submission", ""),
            "verification_status": final_state.get("verification_status", "not_required"),
            "verified_fingerprint": final_state.get("verified_fingerprint", ""),
            "baseline_fingerprint": final_state.get("baseline_fingerprint", ""),
            "current_fingerprint": final_state.get("current_fingerprint", ""),
            "has_changes": final_state.get("has_changes", False),
            "require_verification": final_state.get("require_verification", True),
            "resume_schema": 1,
            "resumable": final_state.get("exit_status") != "Submitted",
            "workspace_changes": before_snapshot.diff(after_snapshot),
            "undo_journal": undo_journal,
            "events": final_state["events"],
            "messages": serialize_messages(
                compact_messages(
                    final_state["messages"],
                    max_chars=self.context_char_budget,
                    preserve_first_human=True,
                )
            ),
        })
        if error_text:
            trajectory["error"] = error_text
        if self.trajectory_path:
            write_json(self.trajectory_path, trajectory)
        return trajectory

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("model", self._call_model)
        graph.add_node("tools", self._run_tools)
        graph.add_node("format_error", self._format_error)
        graph.add_edge(START, "model")
        graph.add_conditional_edges(
            "model",
            self._route_after_model,
            {"tools": "tools", "format_error": "format_error", "end": END},
        )
        graph.add_conditional_edges("tools", self._route_after_tools, {"model": "model", "end": END})
        graph.add_edge("format_error", "model")
        return graph.compile()

    def _call_model(self, state: AgentState) -> dict[str, Any]:
        self._last_gate = VerificationGate.from_state(state)
        if state["steps"] >= self.max_steps:
            return {
                "done": True,
                "exit_status": "StepLimitExceeded",
                "submission": "",
                "events": [{"type": "limit", "reason": "step_limit", "step": state["steps"]}],
            }
        response = self.model.invoke(
            compact_messages(
                state["messages"],
                max_chars=self.context_char_budget,
                preserve_first_human=True,
            )
        )
        response = limit_model_tool_calls(response)
        step = state["steps"] + 1
        self._print(f"\n[model step {step}] {response.content}")
        event = {
            "type": "model",
            "step": step,
            "content": truncate_text(
                self.executor.redactor.redact_text(str(response.content))
            ),
            "tool_calls": self.executor.redactor.redact_data(
                self._serializable_tool_calls(getattr(response, "tool_calls", []))
            ),
        }
        self._event_log.append(event)
        return {
            "messages": [response],
            "steps": step,
            "events": [event],
        }

    def _run_tools(self, state: AgentState) -> dict[str, Any]:
        last_message = state["messages"][-1]
        tool_messages: list[ToolMessage] = []
        events: list[dict[str, Any]] = []
        gate = VerificationGate.from_state(state)
        batch = execute_tool_batch(
            self.executor,
            getattr(last_message, "tool_calls", []),
            gate,
            ignore_paths=self._artifact_paths(),
            on_structured_edit=self._record_undo,
        )
        self._last_gate = gate

        for executed in batch.calls:
            name = executed.name
            args = executed.args
            result = executed.result
            self._print(f"\n[{name}]\n{self.executor.redactor.redact_data(args)}")
            observation = result.to_observation()
            event = {
                "type": "tool",
                "step": state["steps"],
                "tool": name,
                "args": self.executor.redactor.redact_data(
                    audit_tool_args(name, args)
                ),
                "command": truncate_text(result.command),
                "returncode": result.returncode,
                "duration_ms": result.duration_ms,
                "output": truncate_text(result.output),
                "exception_info": truncate_text(result.exception_info),
                "submitted": result.submitted,
                "approved": result.approved,
                "blocked": result.blocked,
            }
            events.append(event)
            self._event_log.append(event)
            self._print(f"[returncode {result.returncode}]\n{truncate_text(result.output, 2000)}")
            tool_messages.append(
                ToolMessage(
                    content=json.dumps(self.executor.redactor.redact_data(observation), ensure_ascii=False),
                    tool_call_id=executed.tool_call_id,
                )
            )

        update: dict[str, Any] = {
            "messages": tool_messages,
            "events": events,
            "done": batch.submitted,
            "exit_status": "Submitted" if batch.submitted else state.get("exit_status", ""),
            "submission": batch.submission if batch.submitted else state.get("submission", ""),
            **gate.to_state(),
        }
        resume_state: AgentState = {
            **state,
            **update,
            "messages": [*state["messages"], *tool_messages],
            "events": [*state["events"], *events],
        }
        self._resume_state = resume_state
        self._checkpoint(
            "Submitted" if batch.submitted else "Running",
            state["steps"],
            state=resume_state,
        )
        return update

    def _record_undo(self, result: ToolResult) -> None:
        if not result.file_path or result.file_existed_before is None:
            return
        self._undo_records.append(
            {
                "path": result.file_path,
                "existed_before": result.file_existed_before,
                "before_content": result.before_content or "",
                "before_hash": result.before_hash,
                "after_hash": result.after_hash,
            }
        )

    def _checkpoint(
        self,
        exit_status: str,
        steps: int,
        *,
        state: AgentState | None,
    ) -> None:
        if not self.trajectory_path:
            return
        state = state or self._resume_state
        if state is None:
            return
        undo_journal = ""
        if self._undo_records:
            undo_journal = write_undo_journal(
                self.trajectory_path.resolve(), self.executor.cwd, self._undo_records
            )
        checkpoint = self.executor.redactor.redact_data(
            {
                "task": self._task,
                "mode": "run",
                "cwd": str(self.executor.cwd),
                "sandbox": self.executor.sandbox_status(),
                "duration_ms": int((time.time() - self._started) * 1000) if self._started else 0,
                "steps": steps,
                "exit_status": exit_status,
                "submission": "",
                "verification_status": state.get("verification_status", "required"),
                "baseline_fingerprint": state.get("baseline_fingerprint", ""),
                "current_fingerprint": state.get("current_fingerprint", ""),
                "verified_fingerprint": state.get("verified_fingerprint", ""),
                "has_changes": state.get("has_changes", False),
                "require_verification": state.get("require_verification", True),
                "resume_schema": 1,
                "resumable": exit_status != "Submitted",
                "workspace_changes": {},
                "undo_journal": undo_journal,
                "events": state.get("events", self._event_log),
                "messages": serialize_messages(
                    compact_messages(
                        state["messages"],
                        max_chars=self.context_char_budget,
                        preserve_first_human=True,
                    )
                ),
            }
        )
        write_json(self.trajectory_path, checkpoint)

    def _artifact_paths(self) -> set[Path]:
        if not self.trajectory_path:
            return set()
        trajectory = self.trajectory_path.resolve()
        return {
            trajectory,
            trajectory.with_suffix(trajectory.suffix + ".tmp"),
            trajectory.with_suffix(trajectory.suffix + ".undo.json"),
            trajectory.with_suffix(trajectory.suffix + ".undo.json.tmp"),
        }

    def _restore_state(
        self,
        data: dict[str, Any],
        *,
        task: str,
        current_fingerprint: str,
    ) -> AgentState:
        if data.get("mode", "run") != "run":
            raise ValueError("resume trajectory is not a run session")
        if int(data.get("resume_schema", 0)) != 1:
            raise ValueError("trajectory does not contain a supported resume checkpoint")
        if data.get("resumable") is False or data.get("exit_status") == "Submitted":
            raise ValueError("trajectory is already submitted and is not resumable")
        if Path(str(data.get("cwd", ""))).resolve() != self.executor.cwd:
            raise ValueError("resume workspace does not match the trajectory cwd")
        saved_task = str(data.get("task", "")).strip()
        if not saved_task:
            raise ValueError("resume trajectory has no task")
        if task.strip() and task.strip() != saved_task:
            raise ValueError("supplied task does not match the resume trajectory")
        raw_messages = data.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise ValueError("resume trajectory has no message checkpoint")
        restored_messages = messages_from_dict(raw_messages)
        if (
            restored_messages
            and restored_messages[-1].type == "ai"
            and getattr(restored_messages[-1], "tool_calls", [])
        ):
            raise ValueError("resume checkpoint ends with unexecuted tool calls")
        restored_messages.append(
            HumanMessage(
                content=(
                    "This run was resumed from a safe checkpoint. Treat the current "
                    "workspace as authoritative and rerun the configured tests before submit."
                )
            )
        )

        self._task = saved_task
        self._event_log = list(data.get("events", []))
        if data.get("undo_journal"):
            self._undo_records = load_authenticated_undo_records(data)
        gate = VerificationGate(
            baseline_fingerprint=str(data.get("baseline_fingerprint", "")),
            current_fingerprint=str(data.get("current_fingerprint", "")),
            # A trajectory is an audit artifact, not a trust anchor. Resuming
            # always requires a fresh authoritative test of the current tree.
            verified_fingerprint="",
            status="required",
            has_changes=bool(data.get("has_changes", False)),
            require_verification=True,
        )
        if not gate.baseline_fingerprint:
            raise ValueError("resume checkpoint has no baseline fingerprint")
        gate.sync(current_fingerprint)
        self._last_gate = gate
        return {
            "messages": restored_messages,
            "steps": max(0, int(data.get("steps", 0))),
            "done": False,
            "exit_status": "",
            "submission": "",
            "events": list(self._event_log),
            **gate.to_state(),
        }

    def _format_error(self, state: AgentState) -> dict[str, Any]:
        event = {"type": "format_error", "step": state["steps"], "reason": "missing_tool_call"}
        self._event_log.append(event)
        correction = HumanMessage(
                    content=(
                        "Your previous response did not call any tool. "
                        "Every response must include at least one tool call. "
                        "Prefer read_file, apply_patch, run_tests, and git_diff for coding work. "
                        "To finish, call submit with a concise summary."
                    )
                )
        update = {
            "messages": [correction],
            "events": [event],
        }
        resume_state: AgentState = {
            **state,
            "messages": [*state["messages"], correction],
            "events": [*state["events"], event],
        }
        self._resume_state = resume_state
        self._checkpoint("Running", state["steps"], state=resume_state)
        return update

    @staticmethod
    def _route_after_model(state: AgentState) -> Literal["tools", "format_error", "end"]:
        if state.get("done"):
            return "end"
        if getattr(state["messages"][-1], "tool_calls", []):
            return "tools"
        return "format_error"

    @staticmethod
    def _route_after_tools(state: AgentState) -> Literal["model", "end"]:
        return "end" if state.get("done") else "model"

    @staticmethod
    def _serializable_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return audit_tool_calls(tool_calls)

    def _model_step_count(self) -> int:
        return sum(1 for event in self._event_log if event.get("type") == "model")

    def _print(self, text: str) -> None:
        if not self.quiet:
            print(text)
