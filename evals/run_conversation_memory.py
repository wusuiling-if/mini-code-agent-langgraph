"""Deterministic production-bridge gate for long-term conversation memory."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path
from typing import Callable

from mini_code_agent.conversation_ledger import ConversationLedgerError
from mini_code_agent.conversation_memory import (
    LocalConversationMemory,
    verify_conversation_memory,
)
from mini_code_agent.memory_backup import (
    export_memory_backup,
    purge_memory_store,
    restore_memory_backup,
)

SUITE_NAME = "conversation-memory-production-v0.6.0"


def _runtime(root: Path, workspace_name: str = "workspace") -> LocalConversationMemory:
    workspace = root / workspace_name
    workspace.mkdir(exist_ok=True)
    return LocalConversationMemory(
        root / "state",
        workspace,
        root / f"{workspace_name}.chat.json",
    )


def _user_scope_case(root: Path) -> str:
    first = _runtime(root)
    event = first.record_event("user", "我偏好简洁回答")
    first.remember("我偏好简洁回答", event, scope="user")
    second = _runtime(root, "other")
    context = second.recall("我的回答偏好是什么？")
    assert "我偏好简洁回答" in context
    return "cross-workspace user memory recalled"


def _workspace_isolation_case(root: Path) -> str:
    first = _runtime(root)
    event = first.record_event("user", "这个项目使用 pytest")
    first.remember("这个项目使用 pytest", event, scope="workspace")
    second = _runtime(root, "other")
    assert "pytest" not in second.recall("测试框架是什么？")
    return "workspace memory isolated"


def _temporal_controls_case(root: Path) -> str:
    memory = _runtime(root)
    source = memory.record_event("user", "编辑器是 Neovim")
    old = memory.remember("编辑器是 Neovim", source)
    correction = memory.record_event("user", "改成 Helix")
    _old, new = memory.correct(old.id, "编辑器是 Helix", correction)
    forgetting = memory.record_event("user", "忘掉编辑器")
    memory.forget(new.id, forgetting)
    assert memory.store.get_card(old.id).status == "superseded"
    assert memory.store.get_card(new.id).status == "tombstoned"
    return "supersede and tombstone authenticated"


def _candidate_gate_case(root: Path) -> str:
    memory = _runtime(root)
    event = memory.record_event("user", "我偏好使用深色主题")
    candidate = memory.stage_candidate(event)
    assert candidate is not None
    assert memory.store.status().cards == 0
    card = memory.remember_candidate(candidate.candidate_id)
    assert card.scope == "user"
    return "candidate remained pending until approval"


def _secret_refusal_case(root: Path) -> str:
    memory = _runtime(root)
    text = "api_key=abcdefghijklmnopqrstuvwxyz123456"
    event = memory.record_event("user", text)
    try:
        memory.remember(text, event)
    except ValueError:
        return "credential-like durable admission refused"
    raise AssertionError("credential-like value was admitted")


def _hmac_tamper_case(root: Path) -> str:
    memory = _runtime(root)
    memory.record_event("user", "原始事件")
    changed = memory.events_path.read_text(encoding="utf-8").replace(
        "原始事件", "篡改事件"
    )
    memory.events_path.write_text(changed, encoding="utf-8")
    memory.events_path.chmod(0o600)
    try:
        _runtime(root)
    except ConversationLedgerError:
        return "HMAC tampering rejected"
    raise AssertionError("tampered event log was accepted")


def _backup_case(root: Path) -> str:
    memory = _runtime(root)
    event = memory.record_event("user", "用户偏好中文")
    card = memory.remember("用户偏好中文", event, scope="user")
    backup = root / "memory.zip"
    export_memory_backup(memory.store.directory, backup)
    purge_memory_store(memory.store.directory)
    restore_memory_backup(backup, memory.store.directory)
    restored = _runtime(root)
    assert restored.store.get_card(card.id).value == "用户偏好中文"
    return "verified backup, purge, and restore round trip"


def _cross_evidence_case(root: Path) -> str:
    memory = _runtime(root)
    event = memory.record_event("user", "保持来源")
    memory.remember("保持来源", event)
    verification = verify_conversation_memory(memory.store.directory)
    assert verification.ok
    assert verification.checked_sources == 1
    return "card source resolved to authenticated event"


def run_conversation_memory_gate() -> dict[str, object]:
    started = time.monotonic()
    cases: tuple[tuple[str, Callable[[Path], str]], ...] = (
        ("user-scope", _user_scope_case),
        ("workspace-isolation", _workspace_isolation_case),
        ("temporal-controls", _temporal_controls_case),
        ("candidate-approval", _candidate_gate_case),
        ("secret-refusal", _secret_refusal_case),
        ("hmac-tamper", _hmac_tamper_case),
        ("backup-restore", _backup_case),
        ("cross-evidence", _cross_evidence_case),
    )
    results = []
    for name, case in cases:
        with tempfile.TemporaryDirectory(prefix="mca-conversation-eval-") as raw:
            try:
                observed = case(Path(raw))
                passed = True
            except Exception as exc:
                observed = f"{type(exc).__name__}: {exc}"
                passed = False
        results.append({"name": name, "passed": passed, "observed": observed})
    passed = sum(int(item["passed"]) for item in results)
    return {
        "suite": SUITE_NAME,
        "scope": {
            "offline": True,
            "model_calls": 0,
            "automatic_durable_admission": False,
        },
        "acceptance": {
            "cases": len(results),
            "passed_cases": passed,
            "passed": passed == len(results),
        },
        "duration_ms": int((time.monotonic() - started) * 1000),
        "cases": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run_conversation_memory_gate()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        acceptance = report["acceptance"]
        print(
            "conversation memory gate: "
            f"{acceptance['passed_cases']}/{acceptance['cases']} passed"
        )
    return 0 if report["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
