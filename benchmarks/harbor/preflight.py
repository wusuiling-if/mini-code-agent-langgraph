"""Paid-run transport preflight for the fixed-model Harbor protocol."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any


def _configured_key(environ: Mapping[str, str]) -> str:
    key = environ.get("OPENAI_API_KEY") or environ.get("MCA_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY or MCA_API_KEY is required for preflight")
    return key


def run_transport_preflight(
    protocol: Mapping[str, Any],
    environ: Mapping[str, str],
    *,
    client_factory: Callable[..., Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Require a long-context streamed tool call before launching paid tasks."""

    comparison = protocol["comparison"]
    provider, model = comparison["model"].split("/", 1)
    transport = comparison["transport"]
    preflight = transport["preflight"]
    if provider != "openai" or transport.get("api") != "chat_completions":
        raise ValueError("transport preflight supports pinned OpenAI Chat Completions")
    if transport.get("streaming") is not True:
        raise ValueError("transport preflight requires streaming=true")
    reasoning_effort = transport.get("reasoning_effort")
    if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
        raise ValueError("transport preflight requires reasoning_effort")
    context_chars = preflight.get("context_chars")
    request_timeout = preflight.get("request_timeout")
    if not isinstance(context_chars, int) or context_chars < 8_000:
        raise ValueError("preflight context_chars must be at least 8000")
    if not isinstance(request_timeout, int) or request_timeout < 1:
        raise ValueError("preflight request_timeout must be positive")

    if client_factory is None:
        from openai import OpenAI

        client_factory = OpenAI
    client = client_factory(
        api_key=_configured_key(environ),
        base_url=comparison.get("base_url"),
        timeout=request_timeout,
        max_retries=0,
    )
    filler = ("repository context line: inspect files before editing.\n" * 600)[
        :context_chars
    ]
    messages = [
        {
            "role": "system",
            "content": "You are a coding agent. Use the required tool call.",
        },
        {
            "role": "user",
            "content": filler + "\nList the project root now.",
        },
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List project files.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    started = clock()
    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools,
        tool_choice={"type": "function", "function": {"name": "list_files"}},
        parallel_tool_calls=False,
        reasoning_effort=reasoning_effort.strip(),
        stream=True,
        stream_options={"include_usage": True},
    )
    first_chunk_seconds: float | None = None
    chunk_count = 0
    tool_name = ""
    arguments = ""
    input_tokens = 0
    output_tokens = 0
    for chunk in stream:
        chunk_count += 1
        if first_chunk_seconds is None:
            first_chunk_seconds = clock() - started
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = choices[0].delta
        for tool_call in getattr(delta, "tool_calls", None) or []:
            function = getattr(tool_call, "function", None)
            if function is None:
                continue
            tool_name += getattr(function, "name", None) or ""
            arguments += getattr(function, "arguments", None) or ""
    total_seconds = clock() - started
    if chunk_count < 1 or first_chunk_seconds is None:
        raise RuntimeError("transport preflight returned no streaming chunks")
    if tool_name != "list_files":
        raise RuntimeError("transport preflight did not return the required tool call")
    try:
        parsed_arguments = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "transport preflight returned invalid tool arguments"
        ) from exc
    if not isinstance(parsed_arguments, dict):
        raise TypeError("transport preflight tool arguments must be an object")
    return {
        "status": "passed",
        "api": "chat_completions",
        "streaming": True,
        "reasoning_effort": reasoning_effort.strip(),
        "context_chars": len(messages[1]["content"]),
        "chunks": chunk_count,
        "first_chunk_seconds": round(first_chunk_seconds, 3),
        "total_seconds": round(total_seconds, 3),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tool": tool_name,
    }
