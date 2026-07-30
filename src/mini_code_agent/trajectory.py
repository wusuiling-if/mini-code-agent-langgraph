from __future__ import annotations

import json
import hashlib
import hmac
import os
import secrets
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from mini_code_agent.security import SafeWorkspace
from mini_code_agent.utils import MAX_STATE_FILE_BYTES, atomic_write_text


JOURNAL_VERSION = 2
STATE_REFERENCE_PREFIX = "state:"


def load_trajectory(path: Path) -> dict[str, Any]:
    source = path.expanduser()
    data = json.loads(_read_private_text(source))
    data["_trajectory_path"] = str(source.resolve())
    return data


def write_undo_journal(trajectory_path: Path, root: Path, records: list[dict[str, Any]]) -> str:
    trajectory_path = trajectory_path.resolve()
    root = root.resolve()
    state_dir = _journal_state_dir(trajectory_path, root)
    key = _load_or_create_key(state_dir)
    normalized_records = _validate_records(root, records)
    payload = {
        "version": JOURNAL_VERSION,
        "trajectory_path": str(trajectory_path),
        "cwd": str(root),
        "created_at_ns": time.time_ns(),
        "records": normalized_records,
    }
    encoded_payload = _canonical_json(payload)
    envelope = {
        "payload": payload,
        "hmac_sha256": hmac.new(key, encoded_payload, hashlib.sha256).hexdigest(),
    }
    # One trajectory owns one journal. Rewriting it atomically avoids leaving a
    # new source-containing file behind after every incremental checkpoint.
    journal_id = hmac.new(
        key, str(trajectory_path).encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]
    journal_name = f"undo-{journal_id}.json"
    atomic_write_text(
        state_dir / journal_name,
        json.dumps(envelope, ensure_ascii=False),
        mode=0o600,
        max_bytes=MAX_STATE_FILE_BYTES,
    )
    return STATE_REFERENCE_PREFIX + journal_name


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


def undo_trajectory(
    data: dict[str, Any],
    *,
    dry_run: bool = False,
    force: bool = False,
    allow_legacy_unsafe: bool = False,
) -> list[str]:
    root = Path(data["cwd"]).resolve()
    records = _load_undo_records(data, root, allow_legacy_unsafe=allow_legacy_unsafe)
    if records is not None:
        return _undo_records(root, records, dry_run=dry_run, force=force)

    legacy_edits = [
        event
        for event in data.get("events", [])
        if event.get("type") == "tool"
        and event.get("tool") in {"apply_patch", "replace_lines", "write_file"}
    ]
    if legacy_edits and not allow_legacy_unsafe:
        raise ValueError(
            "legacy trajectory has no authenticated undo journal; "
            "refusing to write files without allow_legacy_unsafe=True"
        )

    # Explicitly opted-in compatibility for 0.1/0.2 trajectories. The source is
    # unauthenticated, so this path is intentionally noisy and never the default.
    actions = []
    workspace = SafeWorkspace(root)
    for event in reversed(legacy_edits):
        tool = event.get("tool")
        args = event.get("args") or {}
        if tool in {"apply_patch", "replace_lines"}:
            path = _resolve(root, args["path"])
            before = event.get("before_content")
            if before is None:
                actions.append(f"skip {path}: no reversible diff found")
                continue
            if not dry_run:
                workspace.atomic_write_text(path, before, encoding="utf-8")
            actions.append(f"restored {path.relative_to(root)}")
        elif tool == "write_file":
            path = _resolve(root, args["path"])
            before = event.get("before_content")
            if before is None:
                actions.append(f"skip {path}: no reversible diff found")
                continue
            if before:
                if not dry_run:
                    workspace.atomic_write_text(path, before, encoding="utf-8")
                actions.append(f"restored {path.relative_to(root)}")
            else:
                if not dry_run and path.exists():
                    workspace.unlink_file(path)
                actions.append(f"removed {path.relative_to(root)}")
    return actions


def load_authenticated_undo_records(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Load resume/undo records only from an authenticated v2 journal."""

    root = Path(data["cwd"]).resolve()
    records = _load_undo_records(data, root, allow_legacy_unsafe=False)
    return records or []


def _load_undo_records(
    data: dict[str, Any],
    root: Path,
    *,
    allow_legacy_unsafe: bool,
) -> list[dict[str, Any]] | None:
    journal_name = data.get("undo_journal")
    if not journal_name:
        return None
    trajectory_path = data.get("_trajectory_path")
    if not trajectory_path:
        raise ValueError("trajectory source path is required to locate its undo journal")
    trajectory_source = Path(trajectory_path).resolve()
    if str(journal_name).startswith(STATE_REFERENCE_PREFIX):
        name = str(journal_name)[len(STATE_REFERENCE_PREFIX) :]
        if not name or Path(name).name != name:
            raise ValueError("invalid undo journal reference")
        state_dir = _journal_state_dir(trajectory_source, root)
        journal_path = state_dir / name
        envelope = json.loads(_read_private_text(journal_path))
        payload = envelope.get("payload")
        signature = str(envelope.get("hmac_sha256", ""))
        if not isinstance(payload, dict):
            raise ValueError("invalid undo journal payload")
        expected = hmac.new(_load_or_create_key(state_dir), _canonical_json(payload), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("undo journal integrity check failed")
        if payload.get("version") != JOURNAL_VERSION:
            raise ValueError("unsupported undo journal version")
        if Path(payload.get("trajectory_path", "")).resolve() != trajectory_source:
            raise ValueError("undo journal trajectory binding does not match")
        if Path(payload.get("cwd", "")).resolve() != root:
            raise ValueError("undo journal workspace does not match trajectory workspace")
        return _validate_records(root, list(payload.get("records", [])))

    if not allow_legacy_unsafe:
        raise ValueError(
            "legacy undo journal is unsigned; refusing it without "
            "allow_legacy_unsafe=True"
        )

    # Explicit compatibility for journals generated by version 0.2.
    trajectory_dir = trajectory_source.parent
    journal_path = (trajectory_dir / str(journal_name)).resolve()
    journal_path.relative_to(trajectory_dir)
    payload = json.loads(_read_private_text(journal_path))
    if Path(payload.get("cwd", "")).resolve() != root:
        raise ValueError("undo journal workspace does not match trajectory workspace")
    return _validate_records(root, list(payload.get("records", [])))


def _undo_records(
    root: Path,
    records: list[dict[str, Any]],
    *,
    dry_run: bool,
    force: bool,
) -> list[str]:
    actions: list[str] = []
    workspace = SafeWorkspace(root)
    simulated_hashes: dict[Path, str | None] = {}
    for record in reversed(records):
        path = _resolve(root, record["path"])
        current_hash = simulated_hashes.get(path, _path_hash(path, workspace))
        expected_hash = record.get("after_hash") or None
        if not force and current_hash != expected_hash:
            actions.append(f"skip {path.relative_to(root)}: file changed since agent edit")
            continue

        existed_before = bool(record.get("existed_before"))
        before = str(record.get("before_content", ""))
        if existed_before:
            if not dry_run:
                workspace.atomic_write_text(path, before, encoding="utf-8")
            simulated_hashes[path] = _text_hash(before)
            actions.append(f"restored {path.relative_to(root)}")
        else:
            if not dry_run and path.exists():
                workspace.unlink_file(path)
            simulated_hashes[path] = None
            actions.append(f"removed {path.relative_to(root)}")
    return actions


def _resolve(root: Path, raw_path: str) -> Path:
    return SafeWorkspace(root).resolve(raw_path)


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _path_hash(path: Path, workspace: SafeWorkspace | None = None) -> str | None:
    workspace = workspace or SafeWorkspace(path.parent)
    try:
        content = workspace.read_bytes(path)
    except (FileNotFoundError, IsADirectoryError):
        return None
    return hashlib.sha256(content).hexdigest()


def _journal_state_dir(trajectory_path: Path, root: Path) -> Path:
    del trajectory_path  # The signed payload itself remains path-bound.
    configured = os.getenv("MCA_STATE_DIR")
    if configured:
        state_root = Path(configured).expanduser()
    elif os.name == "nt":
        state_root = Path(
            os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        ) / "mini-code-agent"
    elif sys.platform == "darwin":
        state_root = (
            Path.home()
            / "Library"
            / "Application Support"
            / "mini-code-agent"
            / "state"
        )
    else:
        state_root = Path(
            os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state")
        ) / "mini-code-agent"
    state_dir = state_root / "undo"
    try:
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = state_dir.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("unsafe mini-code-agent state directory")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError(
                "mini-code-agent state directory is not owned by the current user"
            )
        os.chmod(state_dir, 0o700)
    except PermissionError:
        uid = os.getuid() if hasattr(os, "getuid") else 0
        root_id = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:20]
        state_dir = Path(tempfile.gettempdir()) / f"mini-code-agent-state-{uid}" / root_id
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = state_dir.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("unsafe mini-code-agent state directory")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise PermissionError("mini-code-agent state directory is not owned by the current user")
    os.chmod(state_dir, 0o700)
    return state_dir.resolve()


def _load_or_create_key(state_dir: Path) -> bytes:
    key_path = state_dir / "journal.key"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(key_path, flags, 0o600)
    except FileExistsError:
        fd = -1
    else:
        key = secrets.token_bytes(32)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            os.write(fd, key)
            os.fsync(fd)
        finally:
            os.close(fd)
        return key

    read_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        read_flags |= os.O_NOFOLLOW
    # A concurrent creator may have installed the directory entry but not yet
    # finished its 32-byte write. Retry briefly; never generate a second key.
    for _ in range(100):
        fd = os.open(key_path, read_flags)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("invalid undo journal key")
            key = os.read(fd, 33)
        finally:
            os.close(fd)
        if len(key) == 32:
            return key
        if key:
            break
        time.sleep(0.01)
    raise ValueError("invalid undo journal key")


def _read_private_text(path: Path) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("undo journal is not a regular file")
        if metadata.st_size > MAX_STATE_FILE_BYTES:
            raise ValueError(
                f"state file exceeds the {MAX_STATE_FILE_BYTES}-byte safety limit"
            )
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            payload = handle.read(MAX_STATE_FILE_BYTES + 1)
        if len(payload) > MAX_STATE_FILE_BYTES:
            raise ValueError(
                f"state file exceeds the {MAX_STATE_FILE_BYTES}-byte safety limit"
            )
        return payload.decode("utf-8")
    finally:
        if fd >= 0:
            os.close(fd)


def _validate_records(root: Path, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(records) > 10_000:
        raise ValueError("too many undo records")
    workspace = SafeWorkspace(root)
    normalized: list[dict[str, Any]] = []
    for raw in records:
        if not isinstance(raw, dict) or "path" not in raw:
            raise ValueError("invalid undo record")
        path = workspace.resolve(str(raw["path"]))
        relative = str(path.relative_to(root))
        existed_before = bool(raw.get("existed_before"))
        before = str(raw.get("before_content", ""))
        before_hash = str(raw.get("before_hash", ""))
        after_hash = str(raw.get("after_hash", ""))
        if existed_before and before_hash != _text_hash(before):
            raise ValueError(f"undo record before hash does not match: {relative}")
        if not re_full_hash(after_hash):
            raise ValueError(f"undo record after hash is invalid: {relative}")
        expected_binding = hashlib.sha256(
            f"{root.resolve()}\0{relative}".encode()
        ).hexdigest()
        supplied_binding = raw.get("path_binding")
        if supplied_binding is not None and supplied_binding != expected_binding:
            raise ValueError(f"undo record path binding does not match: {relative}")
        normalized.append({
            "path": relative,
            "path_binding": expected_binding,
            "existed_before": existed_before,
            "before_content": before,
            "before_hash": before_hash,
            "after_hash": after_hash,
        })
    return normalized


def re_full_hash(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value.lower())


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


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
