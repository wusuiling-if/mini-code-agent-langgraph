from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
)

from mini_code_agent.context import (
    audit_tool_args,
    audit_tool_calls,
    compact_messages,
    limit_model_tool_calls,
)
from mini_code_agent.contracts import ToolExecutor, ToolResult
from mini_code_agent.model import ALL_TOOLS
from mini_code_agent.prompts import CHAT_SYSTEM_PROMPT
from mini_code_agent.trajectory import load_authenticated_undo_records, write_undo_journal
from mini_code_agent.utils import serialize_messages, truncate_text, write_json
from mini_code_agent.verification import (
    VerificationGate,
    capture_workspace_fingerprint,
    execute_tool_batch,
)


MAX_PERSISTED_EVENTS = 2000


@dataclass(frozen=True)
class TurnResult:
    """Structured result for callers that need more than the legacy text response."""

    text: str
    status: str
    completed: bool
    verified: bool
    steps: int
    error: str = ""


class ConversationalCodeAgent:
    """A persistent chat session that may answer directly or use coding tools."""

    def __init__(
        self,
        model,
        executor: ToolExecutor,
        *,
        max_steps_per_turn: int = 20,
        context_char_budget: int = 60_000,
        trajectory_path: Path | None = None,
        quiet: bool = False,
        resume_data: dict[str, Any] | None = None,
    ):
        self.model = model.bind_tools(ALL_TOOLS)
        self.executor = executor
        self.max_steps_per_turn = max_steps_per_turn
        self.context_char_budget = context_char_budget
        self.trajectory_path = trajectory_path
        self.quiet = quiet
        self.started = time.time()
        if resume_data is None:
            self.messages = [
                SystemMessage(
                    content=f"{CHAT_SYSTEM_PROMPT}\n\nProject directory:\n{self.executor.cwd}"
                )
            ]
            self.events: list[dict[str, Any]] = []
            self.events_omitted = 0
            self.undo_records: list[dict[str, Any]] = []
            baseline = capture_workspace_fingerprint(
                self.executor, ignore_paths=self._artifact_paths()
            )
            self._gate = VerificationGate.create(baseline)
            self.exit_status = "chatting"
            self.last_turn: TurnResult | None = None
        else:
            self._restore(resume_data)

    @property
    def has_changes(self) -> bool:
        return self._gate.has_changes

    @property
    def verification_status(self) -> str:
        return self._gate.status

    def respond(self, user_text: str, *, coding_mode: bool = False) -> str:
        """Compatibility API: return text while retaining a structured last_turn."""

        return self.respond_turn(user_text, coding_mode=coding_mode).text

    def respond_turn(
        self, user_text: str, *, coding_mode: bool = False
    ) -> TurnResult:
        current = capture_workspace_fingerprint(
            self.executor, ignore_paths=self._artifact_paths()
        )
        self._gate.sync(current)
        self.messages.append(HumanMessage(content=user_text))
        final_text = ""
        step = 0
        try:
            for step in range(1, self.max_steps_per_turn + 1):
                response = self.model.invoke(
                    compact_messages(
                        self.messages,
                        max_chars=self.context_char_budget,
                    )
                )
                response = limit_model_tool_calls(response)
                self.messages.append(response)
                content = str(response.content or "")
                if content:
                    final_text = content
                tool_calls = list(getattr(response, "tool_calls", []))
                self.events.append(
                    self.executor.redactor.redact_data(
                        {
                            "type": "model",
                            "step": step,
                            "content": truncate_text(content),
                            "tool_calls": audit_tool_calls(tool_calls),
                        }
                    )
                )
                if not tool_calls:
                    current = capture_workspace_fingerprint(
                        self.executor, ignore_paths=self._artifact_paths()
                    )
                    self._gate.sync(current)
                    pending_code = self._gate.has_changes or self._gate.status in {
                        "required",
                        "failed",
                    }
                    if pending_code and coding_mode:
                        # A prose-only "done" can never complete a coding turn. Keep
                        # the API history valid and give the model another chance to
                        # run tests and issue a structured submit call.
                        correction = (
                            "The workspace contains unsubmitted changes. Do not claim completion in "
                            "plain text. Run the configured tests if needed, then call submit."
                        )
                        self.messages.append(HumanMessage(content=correction))
                        self.events.append(
                            {
                                "type": "format_error",
                                "step": step,
                                "reason": "unsubmitted_workspace_changes",
                            }
                        )
                        self._save("verification_required")
                        continue
                    result = TurnResult(
                        text=final_text,
                        status=(
                            "answered_with_pending_changes"
                            if pending_code
                            else "answered"
                        ),
                        completed=True,
                        verified=self._gate.status == "passed",
                        steps=step,
                    )
                    self.last_turn = result
                    self._save("chatting")
                    return result

                batch = execute_tool_batch(
                    self.executor,
                    tool_calls,
                    self._gate,
                    ignore_paths=self._artifact_paths(),
                    on_structured_edit=self._record_undo,
                )
                for executed in batch.calls:
                    result = executed.result
                    observation = result.to_observation()
                    self.events.append(
                        self._tool_event(step, executed.name, executed.args, result)
                    )
                    self.messages.append(
                        ToolMessage(
                            content=json.dumps(
                                self.executor.redactor.redact_data(observation),
                                ensure_ascii=False,
                            ),
                            tool_call_id=executed.tool_call_id,
                        )
                    )
                    if not self.quiet:
                        print(
                            f"\n[{executed.name} rc={result.returncode}]\n"
                            f"{truncate_text(result.output, 2000)}"
                        )
                self.messages = compact_messages(
                    self.messages, max_chars=self.context_char_budget
                )
                self._save("chatting")
                if batch.submitted:
                    final_text = batch.submission or final_text
                    was_verified = self._gate.status == "passed"
                    self._gate.accept_submission()
                    result = TurnResult(
                        text=final_text,
                        status="submitted",
                        completed=True,
                        verified=was_verified,
                        steps=step,
                    )
                    self.last_turn = result
                    self._save("submitted")
                    return result

            unverified = self._gate.has_changes or self._gate.status in {"required", "failed"}
            status = "turn_step_limit_unverified" if unverified else "turn_step_limit"
            text = (
                "I could not complete this turn within the model-call limit. "
                "The session is still open; you can ask me to continue."
            )
            result = TurnResult(
                text=text,
                status=status,
                completed=False,
                verified=False,
                steps=self.max_steps_per_turn,
                error="StepLimitExceeded",
            )
            self.last_turn = result
            self._save(status)
            return result
        except KeyboardInterrupt:
            status = "interrupted"
            self.events.append(
                {"type": "error", "step": 0, "error": "KeyboardInterrupt"}
            )
            self._save(status)
            raise
        except Exception as exc:
            error = self.executor.redactor.redact_text(f"{type(exc).__name__}: {exc}")
            status = f"error:{type(exc).__name__}"
            self.events.append({"type": "error", "step": 0, "error": error})
            text = (
                f"This turn stopped because of {type(exc).__name__}. "
                "The chat session is still open; retry or rephrase the request."
            )
            result = TurnResult(
                text=text,
                status=status,
                completed=False,
                verified=False,
                steps=step,
                error=error,
            )
            self.last_turn = result
            self._save(status)
            return result

    def clear_context(self) -> None:
        # Clearing model context must not clear pending edits or their verification
        # state; otherwise /clear would become a submit-gate bypass.
        self.messages = self.messages[:1]
        self._save("chatting")

    def close(self) -> None:
        # Preserve why the last turn stopped. In particular, finally: close() must
        # not turn an interrupt, API error, or step-limit trajectory into "closed".
        if self.exit_status.startswith(("error:", "interrupted", "turn_step_limit")):
            self._save(self.exit_status)
        else:
            self._save("closed")

    def _record_undo(self, result: ToolResult) -> None:
        if not result.file_path or result.file_existed_before is None:
            return
        self.undo_records.append(
            {
                "path": result.file_path,
                "existed_before": result.file_existed_before,
                "before_content": result.before_content or "",
                "before_hash": result.before_hash,
                "after_hash": result.after_hash,
            }
        )

    def _tool_event(
        self, step: int, name: str, args: dict[str, Any], result: ToolResult
    ) -> dict[str, Any]:
        event = {
            "type": "tool",
            "step": step,
            "tool": name,
            "args": audit_tool_args(name, args),
            "command": truncate_text(result.command),
            "returncode": result.returncode,
            "duration_ms": result.duration_ms,
            "output": truncate_text(result.output),
            "exception_info": truncate_text(result.exception_info),
            "submitted": result.submitted,
            "blocked": result.blocked,
        }
        if result.tests_run is not None:
            event["tests_run"] = result.tests_run
        return self.executor.redactor.redact_data(event)

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

    def _save(self, exit_status: str) -> None:
        self.exit_status = exit_status
        self.messages = compact_messages(
            self.messages, max_chars=self.context_char_budget
        )
        if len(self.events) > MAX_PERSISTED_EVENTS:
            excess = len(self.events) - MAX_PERSISTED_EVENTS
            self.events = self.events[-MAX_PERSISTED_EVENTS:]
            self.events_omitted += excess
        if not self.trajectory_path:
            return
        undo_journal = ""
        if self.undo_records:
            undo_journal = write_undo_journal(
                self.trajectory_path.resolve(), self.executor.cwd, self.undo_records
            )
        trajectory = self.executor.redactor.redact_data(
            {
                "mode": "chat",
                "resume_schema": 1,
                "resumable": True,
                "cwd": str(self.executor.cwd),
                "sandbox": self.executor.sandbox_status(),
                "duration_ms": int((time.time() - self.started) * 1000),
                "exit_status": exit_status,
                "verification_status": self._gate.status,
                "has_changes": self._gate.has_changes,
                "baseline_fingerprint": self._gate.baseline_fingerprint,
                "current_fingerprint": self._gate.current_fingerprint,
                "verified_fingerprint": self._gate.verified_fingerprint,
                "require_verification": self._gate.require_verification,
                "undo_journal": undo_journal,
                "last_turn": self.last_turn.__dict__ if self.last_turn else None,
                "events_omitted": self.events_omitted,
                "events": self.events,
                "messages": serialize_messages(self.messages),
            }
        )
        write_json(self.trajectory_path, trajectory)

    def _restore(self, data: dict[str, Any]) -> None:
        if data.get("mode") != "chat":
            raise ValueError("resume trajectory is not a chat session")
        if int(data.get("resume_schema", 0)) != 1:
            raise ValueError("chat trajectory does not contain a supported checkpoint")
        if Path(str(data.get("cwd", ""))).resolve() != self.executor.cwd:
            raise ValueError("resume workspace does not match the chat trajectory cwd")
        raw_messages = data.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise ValueError("chat trajectory has no message checkpoint")
        self.messages = messages_from_dict(raw_messages)
        if (
            self.messages[-1].type == "ai"
            and getattr(self.messages[-1], "tool_calls", [])
        ):
            raise ValueError("chat checkpoint ends with unpaired tool calls")
        self.events = list(data.get("events", []))[-MAX_PERSISTED_EVENTS:]
        self.events_omitted = max(0, int(data.get("events_omitted", 0))) + max(
            0, len(list(data.get("events", []))) - MAX_PERSISTED_EVENTS
        )
        self.undo_records = (
            load_authenticated_undo_records(data)
            if data.get("undo_journal")
            else []
        )
        pending_code = bool(data.get("has_changes", False)) or bool(
            data.get("require_verification", False)
        ) or str(data.get("verification_status", "")) in {"required", "failed"}
        self._gate = VerificationGate(
            baseline_fingerprint=str(data.get("baseline_fingerprint", "")),
            current_fingerprint=str(data.get("current_fingerprint", "")),
            verified_fingerprint="",
            status="required" if pending_code else "not_required",
            has_changes=bool(data.get("has_changes", False)),
            require_verification=pending_code,
        )
        if not self._gate.baseline_fingerprint:
            raise ValueError("chat checkpoint has no baseline fingerprint")
        current = capture_workspace_fingerprint(
            self.executor, ignore_paths=self._artifact_paths()
        )
        self._gate.sync(current)
        self.exit_status = "chatting"
        raw_last_turn = data.get("last_turn")
        self.last_turn = (
            TurnResult(**raw_last_turn) if isinstance(raw_last_turn, dict) else None
        )
