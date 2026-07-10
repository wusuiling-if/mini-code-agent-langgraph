from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_trajectory(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def summarize_trajectory(data: dict[str, Any]) -> str:
    lines = [
        f"task: {data.get('task', '')}",
        f"cwd: {data.get('cwd', '')}",
        f"exit_status: {data.get('exit_status', '')}",
        f"sandbox: {data.get('sandbox', 'unknown')}",
        f"steps: {data.get('steps', 0)}",
        "workspace_changes:",
    ]
    changes = data.get("workspace_changes", {})
    if not any(changes.get(key) for key in ["created", "modified", "deleted"]):
        lines.append("  none")
    else:
        for key in ["created", "modified", "deleted"]:
            for path in changes.get(key, []):
                lines.append(f"  {key}: {path}")
    lines.append("tools:")
    for event in data.get("events", []):
        if event.get("type") == "tool":
            detail = event.get("command") or event.get("args") or ""
            lines.append(f"  step {event.get('step')}: {event.get('tool')} rc={event.get('returncode')} {detail}")
    if data.get("submission"):
        lines.extend(["submission:", str(data["submission"])])
    return "\n".join(lines)


def collect_file_diffs(data: dict[str, Any]) -> str:
    diffs = []
    for event in data.get("events", []):
        if event.get("type") != "tool" or event.get("tool") not in {"apply_patch", "replace_lines", "write_file"}:
            continue
        output = event.get("output", "")
        marker = "--- a/"
        if marker in output:
            diffs.append(output[output.index(marker) :])
    return "\n\n".join(diffs)


def undo_trajectory(data: dict[str, Any], *, dry_run: bool = False) -> list[str]:
    root = Path(data["cwd"]).resolve()
    actions = []
    for event in reversed(data.get("events", [])):
        if event.get("type") != "tool":
            continue
        tool = event.get("tool")
        args = event.get("args") or {}
        if tool in {"apply_patch", "replace_lines"}:
            path = _resolve(root, args["path"])
            before = event.get("before_content")
            if before is None:
                actions.append(f"skip {path}: no reversible diff found")
                continue
            if not dry_run:
                path.write_text(before, encoding="utf-8")
            actions.append(f"restored {path.relative_to(root)}")
        elif tool == "write_file":
            path = _resolve(root, args["path"])
            before = event.get("before_content")
            if before is None:
                actions.append(f"skip {path}: no reversible diff found")
                continue
            if before:
                if not dry_run:
                    path.write_text(before, encoding="utf-8")
                actions.append(f"restored {path.relative_to(root)}")
            else:
                if not dry_run and path.exists():
                    path.unlink()
                actions.append(f"removed {path.relative_to(root)}")
    return actions


def _resolve(root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    resolved = (path if path.is_absolute() else root / path).resolve()
    resolved.relative_to(root)
    return resolved


def _before_from_unified_diff(text: str) -> str | None:
    if "--- a/" not in text or "+++ b/" not in text:
        return None
    lines = text[text.index("--- a/") :].splitlines()
    before_lines = []
    in_hunk = False
    for line in lines[2:]:
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("-") and not line.startswith("---"):
            before_lines.append(line[1:])
        elif line.startswith(" "):
            before_lines.append(line[1:])
    return "\n".join(before_lines) + ("\n" if before_lines else "")
