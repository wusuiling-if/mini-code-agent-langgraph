from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, messages_to_dict


DEFAULT_OUTPUT_LIMIT = 12000


def truncate_text(text: str, limit: int = DEFAULT_OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return f"{text[:head]}\n\n...[elided {len(text) - limit} chars]...\n\n{text[-tail:]}"


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def serialize_messages(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    return messages_to_dict(messages)
