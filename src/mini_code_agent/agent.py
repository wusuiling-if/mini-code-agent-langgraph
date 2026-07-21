from __future__ import annotations

import json
import operator
import time
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
)
from langgraph.graph import END, START, StateGraph, add_messages

from mini_code_agent.context import (
    AUDIT_OMITTED_FIELDS,
    MAX_TOOL_CALLS_PER_MESSAGE,
    _compact_block,
    _compact_message,
    _compact_value,
    _message_blocks,
    _message_size,
    _summarize_messages,
    audit_tool_args,
    audit_tool_calls,
    compact_messages,
    limit_model_tool_calls,
)
from mini_code_agent.contracts import (
    ExecutedToolCall,
    ToolBatchOutcome,
    ToolExecutor,
    ToolResult,
)
from mini_code_agent.model import ALL_TOOLS
from mini_code_agent.prompts import SYSTEM_PROMPT
from mini_code_agent.trajectory import load_authenticated_undo_records, write_undo_journal
from mini_code_agent.utils import serialize_messages, truncate_text, write_json
from mini_code_agent.verification import (
    POTENTIALLY_MUTATING_TOOLS,
    STRUCTURED_EDIT_TOOLS,
    VerificationGate,
    _blocked_submission,
    _fingerprint_files,
    _tool_exception_result,
    capture_workspace_fingerprint,
    execute_tool_batch,
)


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


class MiniCodeAgent:
    def __init__(
        self,
        model,
        executor: ToolExecutor,
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
            if result.tests_run is not None:
                event["tests_run"] = result.tests_run
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
