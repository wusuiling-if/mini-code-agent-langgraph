from __future__ import annotations

import math
import os
from typing import Any, Literal

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from langchain_core.tools import tool

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
MODEL_ALIASES = {
    "deepseek": "deepseek-v4-flash",
    "deepseek-flash": "deepseek-v4-flash",
    "deepseek-pro": "deepseek-v4-pro",
}
Provider = Literal["auto", "deepseek", "openai"]


@tool
def bash(command: str) -> str:
    """Advanced escape hatch for shell commands. Disabled by default."""
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
def run_tests() -> str:
    """Run every user-configured authoritative verification check."""
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
                        "args": {},
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
                        "args": {},
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


class _DeepSeekToolChatMixin:
    """DeepSeek adapter that round-trips thinking-mode tool-call context.

    DeepSeek requires ``reasoning_content`` from assistant tool-call messages to
    be sent back on later requests. ``ChatDeepSeek`` extracts the field from
    responses, while this small adapter also restores it in request payloads.
    """

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        source_messages = self._convert_input(input_).to_messages()
        wire_messages = payload.get("messages", [])
        for source, wire in zip(source_messages, wire_messages):
            if not isinstance(source, AIMessage) or wire.get("role") != "assistant":
                continue
            reasoning_content = source.additional_kwargs.get("reasoning_content")
            if reasoning_content is not None:
                wire["reasoning_content"] = reasoning_content
            # DeepSeek requires assistant content to be present during tool loops.
            if wire.get("tool_calls") and wire.get("content") is None:
                wire["content"] = ""
        return payload

    def _create_chat_result(
        self,
        response: dict[str, Any] | Any,
        generation_info: dict[str, Any] | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        # The upstream adapter handles SDK response models. Keep dict responses
        # (used by some compatible transports and tests) lossless as well.
        if isinstance(response, dict):
            choices = response.get("choices") or []
            if choices:
                raw_message = choices[0].get("message") or {}
                if "reasoning_content" in raw_message:
                    result.generations[0].message.additional_kwargs["reasoning_content"] = (
                        raw_message["reasoning_content"]
                    )
        return result


_DEEPSEEK_MODEL_CLASS = None


def _deepseek_model_class():
    """Create the DeepSeek adapter class only when that provider is selected."""

    global _DEEPSEEK_MODEL_CLASS
    if _DEEPSEEK_MODEL_CLASS is None:
        from langchain_deepseek import ChatDeepSeek

        class DeepSeekToolChatModel(_DeepSeekToolChatMixin, ChatDeepSeek):
            pass

        _DEEPSEEK_MODEL_CLASS = DeepSeekToolChatModel
    return _DEEPSEEK_MODEL_CLASS


def _resolve_provider(model_name: str, provider: Provider) -> Literal["deepseek", "openai"]:
    if provider not in {"auto", "deepseek", "openai"}:
        raise ValueError(f"unsupported provider: {provider}")
    if provider != "auto":
        return provider
    resolved_model = MODEL_ALIASES.get(model_name, model_name)
    return "deepseek" if resolved_model.startswith("deepseek-") else "openai"


def create_model(
    model_name: str,
    *,
    provider: Provider = "auto",
    base_url: str | None = None,
    api_key: str | None = None,
    temperature: float = 0.0,
    request_timeout: float = 60.0,
    max_retries: int = 2,
    deepseek_thinking: bool = False,
):
    if model_name == "mock":
        return MockCodingModel()
    if not math.isfinite(request_timeout) or request_timeout <= 0:
        raise ValueError("request_timeout must be greater than zero")
    if max_retries < 0:
        raise ValueError("max_retries must be zero or greater")
    resolved_model = MODEL_ALIASES.get(model_name, model_name)
    resolved_provider = _resolve_provider(model_name, provider)

    if resolved_provider == "deepseek":
        resolved_base_url = (
            base_url
            or os.getenv("DEEPSEEK_BASE_URL")
            or os.getenv("DEEPSEEK_API_BASE")
            or os.getenv("MCA_BASE_URL")
            or DEEPSEEK_BASE_URL
        )
        resolved_api_key = api_key or os.getenv("DEEPSEEK_API_KEY") or os.getenv("MCA_API_KEY")
        if not resolved_api_key:
            raise RuntimeError(
                "DeepSeek API key is missing. Set DEEPSEEK_API_KEY, MCA_API_KEY, or use --env-file."
            )
        model_options: dict[str, Any] = {
            "model": resolved_model,
            "base_url": resolved_base_url,
            "api_key": resolved_api_key,
            "timeout": request_timeout,
            "max_retries": max_retries,
            "extra_body": {
                "thinking": {"type": "enabled" if deepseek_thinking else "disabled"}
            },
        }
        # DeepSeek thinking mode ignores sampling parameters. Omitting them keeps
        # the request unambiguous and avoids provider compatibility warnings.
        if not deepseek_thinking:
            model_options["temperature"] = temperature
        return _deepseek_model_class()(**model_options)

    resolved_base_url = base_url or os.getenv("OPENAI_BASE_URL") or os.getenv("MCA_BASE_URL")
    resolved_api_key = api_key or os.getenv("OPENAI_API_KEY") or os.getenv("MCA_API_KEY")
    if not resolved_api_key:
        raise RuntimeError(
            "OpenAI API key is missing. Set OPENAI_API_KEY, MCA_API_KEY, or use --env-file. "
            "For a keyless local compatible server, set MCA_API_KEY=not-needed explicitly."
        )
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=resolved_model,
        base_url=resolved_base_url,
        api_key=resolved_api_key,
        temperature=temperature,
        timeout=request_timeout,
        max_retries=max_retries,
    )
