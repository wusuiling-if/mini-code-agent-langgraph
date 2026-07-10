from __future__ import annotations

import os

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
MODEL_ALIASES = {
    "deepseek": "deepseek-v4-flash",
    "deepseek-flash": "deepseek-v4-flash",
    "deepseek-pro": "deepseek-v4-pro",
}


@tool
def bash(command: str) -> str:
    """Advanced escape hatch for shell commands. Disabled by default except final submission."""
    raise RuntimeError("The LangGraph tools node executes bash calls.")


@tool
def list_files(path: str = ".", max_files: int = 200) -> str:
    """List files inside the project directory."""
    raise RuntimeError("The LangGraph tools node executes list_files calls.")


@tool
def search_files(pattern: str, path: str = ".", max_results: int = 100) -> str:
    """Search UTF-8 text files inside the project directory for a literal or regex pattern."""
    raise RuntimeError("The LangGraph tools node executes search_files calls.")


@tool
def read_file(path: str, start_line: int = 1, end_line: int | None = None, max_chars: int = 12000) -> str:
    """Read a UTF-8 text file inside the project directory, optionally by line range."""
    raise RuntimeError("The LangGraph tools node executes read_file calls.")


@tool
def write_file(path: str, content: str) -> str:
    """Write a UTF-8 text file inside the project directory."""
    raise RuntimeError("The LangGraph tools node executes write_file calls.")


@tool
def apply_patch(path: str, old: str, new: str, replace_all: bool = False) -> str:
    """Patch one file inside the project directory by replacing exact old text with new text."""
    raise RuntimeError("The LangGraph tools node executes apply_patch calls.")


@tool
def replace_lines(path: str, start_line: int, end_line: int, new_text: str) -> str:
    """Replace an inclusive 1-based line range in one file inside the project directory."""
    raise RuntimeError("The LangGraph tools node executes replace_lines calls.")


@tool
def git_diff(path: str = "") -> str:
    """Show git diff for the project or one path inside it."""
    raise RuntimeError("The LangGraph tools node executes git_diff calls.")


@tool
def run_tests(command: str = "python3 -m unittest discover -v") -> str:
    """Run the repository test command in the project directory."""
    raise RuntimeError("The LangGraph tools node executes run_tests calls.")


@tool
def submit(summary: str = "") -> str:
    """Finish the task after the fix has been made and verified."""
    raise RuntimeError("The LangGraph tools node executes submit calls.")


ALL_TOOLS = [
    bash,
    list_files,
    search_files,
    read_file,
    write_file,
    apply_patch,
    replace_lines,
    git_diff,
    run_tests,
    submit,
]


class MockCodingModel:
    """Deterministic local model for testing the graph without an API key."""

    def bind_tools(self, tools: list) -> "MockCodingModel":
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        tool_messages = [m for m in messages if m.type == "tool"]
        if not tool_messages:
            return AIMessage(
                content="I will inspect the project files first.",
                tool_calls=[
                    {
                        "name": "list_files",
                        "args": {},
                        "id": "mock-call-1",
                        "type": "tool_call",
                    }
                ],
            )
        if len(tool_messages) == 1:
            return AIMessage(
                content="I will run the test suite.",
                tool_calls=[
                    {
                        "name": "run_tests",
                        "args": {"command": "python3 -m unittest discover -v"},
                        "id": "mock-call-2",
                        "type": "tool_call",
                    }
                ],
            )
        if len(tool_messages) == 2:
            return AIMessage(
                content="The tests failed, so I will inspect the implementation.",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"path": "calculator.py"},
                        "id": "mock-call-3",
                        "type": "tool_call",
                    }
                ],
            )
        if len(tool_messages) == 3:
            return AIMessage(
                content="The add function subtracts instead of adding. I will patch it.",
                tool_calls=[
                    {
                        "name": "apply_patch",
                        "args": {"path": "calculator.py", "old": "return a - b", "new": "return a + b"},
                        "id": "mock-call-4",
                        "type": "tool_call",
                    }
                ],
            )
        if len(tool_messages) == 4:
            return AIMessage(
                content="I will rerun tests and inspect the diff.",
                tool_calls=[
                    {
                        "name": "run_tests",
                        "args": {"command": "python3 -m unittest discover -v"},
                        "id": "mock-call-5",
                        "type": "tool_call",
                    },
                    {
                        "name": "git_diff",
                        "args": {},
                        "id": "mock-call-6",
                        "type": "tool_call",
                    },
                ],
            )
        return AIMessage(
            content="The tests pass, so I will submit.",
            tool_calls=[
                {
                    "name": "submit",
                    "args": {"summary": "Fixed add() to use addition and verified tests pass."},
                    "id": "mock-call-7",
                    "type": "tool_call",
                }
            ],
        )


def create_model(
    model_name: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    temperature: float = 0.0,
):
    if model_name == "mock":
        return MockCodingModel()
    resolved_model = MODEL_ALIASES.get(model_name, model_name)
    resolved_base_url = base_url or os.getenv("MCA_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    resolved_api_key = api_key or os.getenv("MCA_API_KEY") or os.getenv("OPENAI_API_KEY")

    if resolved_model.startswith("deepseek-"):
        resolved_base_url = resolved_base_url or os.getenv("DEEPSEEK_BASE_URL") or DEEPSEEK_BASE_URL
        resolved_api_key = resolved_api_key or os.getenv("DEEPSEEK_API_KEY")

    return ChatOpenAI(
        model=resolved_model,
        base_url=resolved_base_url,
        api_key=resolved_api_key or "not-needed",
        temperature=temperature,
    )
