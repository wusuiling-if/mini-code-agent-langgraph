from __future__ import annotations

import json
import operator
import time
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph, add_messages

from mini_code_agent.executor import BashExecutor
from mini_code_agent.model import ALL_TOOLS
from mini_code_agent.prompts import SYSTEM_PROMPT
from mini_code_agent.utils import serialize_messages, truncate_text, write_json
from mini_code_agent.workspace import WorkspaceSnapshot


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    steps: int
    done: bool
    exit_status: str
    submission: str
    events: Annotated[list[dict[str, Any]], operator.add]


class MiniCodeAgent:
    def __init__(
        self,
        model,
        executor: BashExecutor,
        *,
        max_steps: int = 50,
        trajectory_path: Path | None = None,
        quiet: bool = False,
    ):
        self.model = model.bind_tools(ALL_TOOLS)
        self.executor = executor
        self.max_steps = max_steps
        self.trajectory_path = trajectory_path
        self.quiet = quiet
        self.graph = self._build_graph()

    def run(self, task: str) -> dict[str, Any]:
        initial_state: AgentState = {
            "messages": [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=f"Task:\n{task}\n\nProject directory:\n{self.executor.cwd}"),
            ],
            "steps": 0,
            "done": False,
            "exit_status": "",
            "submission": "",
            "events": [],
        }
        started = time.time()
        before_snapshot = WorkspaceSnapshot.capture(self.executor.cwd)
        final_state = self.graph.invoke(
            initial_state,
            config={"recursion_limit": max(20, self.max_steps * 4 + 10)},
        )
        after_snapshot = WorkspaceSnapshot.capture(self.executor.cwd)
        trajectory = self.executor.redactor.redact_data({
            "task": task,
            "cwd": str(self.executor.cwd),
            "sandbox": self.executor.sandbox_status(),
            "duration_ms": int((time.time() - started) * 1000),
            "steps": final_state["steps"],
            "exit_status": final_state.get("exit_status") or "Stopped",
            "submission": final_state.get("submission", ""),
            "workspace_changes": before_snapshot.diff(after_snapshot),
            "events": final_state["events"],
            "messages": serialize_messages(final_state["messages"]),
        })
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
        if state["steps"] >= self.max_steps:
            return {
                "done": True,
                "exit_status": "StepLimitExceeded",
                "submission": "",
                "events": [{"type": "limit", "reason": "step_limit", "step": state["steps"]}],
            }
        response = self.model.invoke(state["messages"])
        step = state["steps"] + 1
        self._print(f"\n[model step {step}] {response.content}")
        return {
            "messages": [response],
            "steps": step,
            "events": [
                {
                    "type": "model",
                    "step": step,
                    "content": self.executor.redactor.redact_text(str(response.content)),
                    "tool_calls": self.executor.redactor.redact_data(
                        self._serializable_tool_calls(getattr(response, "tool_calls", []))
                    ),
                }
            ],
        }

    def _run_tools(self, state: AgentState) -> dict[str, Any]:
        last_message = state["messages"][-1]
        tool_messages = []
        events = []
        done = False
        exit_status = state.get("exit_status", "")
        submission = state.get("submission", "")

        for tool_call in getattr(last_message, "tool_calls", []):
            name = tool_call.get("name", "")
            args = tool_call.get("args", {})
            tool_call_id = tool_call.get("id") or f"tool-{len(events) + 1}"

            self._print(f"\n[{name}]\n{self.executor.redactor.redact_data(args)}")
            result = self.executor.execute_tool(name, args)
            observation = result.to_observation()
            events.append(
                {
                    "type": "tool",
                    "step": state["steps"],
                    "tool": name,
                    "args": self.executor.redactor.redact_data(args),
                    "command": result.command,
                    "returncode": result.returncode,
                    "duration_ms": result.duration_ms,
                    "output": truncate_text(result.output),
                    "before_content": result.before_content,
                    "after_content": result.after_content,
                    "exception_info": result.exception_info,
                    "submitted": result.submitted,
                    "approved": result.approved,
                    "blocked": result.blocked,
                }
            )
            self._print(f"[returncode {result.returncode}]\n{truncate_text(result.output, 2000)}")
            if result.submitted:
                done = True
                exit_status = "Submitted"
                submission = result.submission

            tool_messages.append(
                ToolMessage(
                    content=json.dumps(self.executor.redactor.redact_data(observation), ensure_ascii=False),
                    tool_call_id=tool_call_id,
                )
            )
            if done:
                break

        return {
            "messages": tool_messages,
            "events": events,
            "done": done,
            "exit_status": exit_status,
            "submission": submission,
        }

    def _format_error(self, state: AgentState) -> dict[str, Any]:
        return {
            "messages": [
                HumanMessage(
                    content=(
                        "Your previous response did not call any tool. "
                        "Every response must include at least one tool call. "
                        "Prefer read_file, apply_patch, run_tests, and git_diff for coding work. "
                        "To finish, call bash with: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
                    )
                )
            ],
            "events": [{"type": "format_error", "step": state["steps"], "reason": "missing_tool_call"}],
        }

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
        return [
            {
                "name": call.get("name"),
                "args": call.get("args"),
                "id": call.get("id"),
            }
            for call in tool_calls
        ]

    def _print(self, text: str) -> None:
        if not self.quiet:
            print(text)
