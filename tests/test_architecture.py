import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import get_type_hints

import pytest

from mini_code_agent.contracts import SnapshotLike, ToolExecutor, ToolResult
from mini_code_agent.model import run_tests
from mini_code_agent.prompts import CHAT_SYSTEM_PROMPT, SYSTEM_PROMPT
from mini_code_agent.verification import VerificationGate, execute_tool_batch


def test_chat_import_does_not_load_agent_module():
    command = [
        sys.executable,
        "-c",
        "import sys; import mini_code_agent.chat; "
        "print('mini_code_agent.agent' in sys.modules)",
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=True)
    assert result.stdout.strip() == "False"


@pytest.mark.parametrize(
    "module_name", ["mini_code_agent.agent", "mini_code_agent.chat"]
)
def test_orchestrator_import_does_not_load_concrete_executor(module_name: str):
    command = [
        sys.executable,
        "-c",
        f"import sys; import {module_name}; "
        "print('mini_code_agent.executor' in sys.modules)",
    ]

    result = subprocess.run(command, text=True, capture_output=True, check=True)

    assert result.stdout.strip() == "False"


class _IdentityRedactor:
    def redact_text(self, text: str) -> str:
        return text

    def redact_data(self, value):
        return value


@dataclass
class _FakeSnapshot:
    files: dict[str, str]
    fingerprint: str

    def diff(self, other: SnapshotLike) -> dict[str, list[str]]:
        return {
            "created": sorted(set(other.files) - set(self.files)),
            "deleted": sorted(set(self.files) - set(other.files)),
            "modified": sorted(
                path
                for path in set(self.files) & set(other.files)
                if self.files[path] != other.files[path]
            ),
        }


class _FakeExecutor:
    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.redactor = _IdentityRedactor()

    def workspace_fingerprint(self, *, ignore_paths=None) -> _FakeSnapshot:
        return _FakeSnapshot(
            files={"example.py": "file:stable"}, fingerprint="stable-fingerprint"
        )

    def execute_tool(self, name: str, args: dict) -> ToolResult:
        return ToolResult(
            tool=name,
            output="fake result",
            returncode=0,
            duration_ms=0,
            args=args,
        )

    def sandbox_status(self) -> str:
        return "fake"


def test_execute_tool_batch_accepts_a_protocol_fake(tmp_path: Path):
    executor = _FakeExecutor(tmp_path)
    gate = VerificationGate.create("stable-fingerprint")

    assert isinstance(executor, ToolExecutor)

    outcome = execute_tool_batch(
        executor,
        [{"name": "list_files", "args": {}, "id": "list-1"}],
        gate,
    )

    assert [call.tool_call_id for call in outcome.calls] == ["list-1"]
    assert outcome.calls[0].result.output == "fake result"


def test_full_agent_executor_protocol_requires_a_snapshot():
    annotation = get_type_hints(ToolExecutor.workspace_fingerprint)["return"]

    assert annotation is SnapshotLike


def test_legacy_runtime_exports_are_preserved():
    from mini_code_agent.agent import VerificationGate as LegacyGate
    from mini_code_agent.agent import compact_messages as legacy_compact
    from mini_code_agent.executor import ToolResult as LegacyToolResult
    from mini_code_agent.context import compact_messages
    from mini_code_agent.contracts import ToolResult
    from mini_code_agent.verification import VerificationGate

    assert LegacyGate is VerificationGate
    assert legacy_compact is compact_messages
    assert LegacyToolResult is ToolResult


def test_run_tests_contract_is_argument_free_and_prompts_require_full_matrix():
    schema = run_tests.args_schema.model_json_schema()

    assert schema["properties"] == {}
    assert schema.get("required", []) == []
    for prompt in (SYSTEM_PROMPT, CHAT_SYSTEM_PROMPT):
        assert "run_tests always executes the complete matrix configured by the user" in prompt
        assert "Never invent, select, skip, reorder, or override" in prompt
        assert "Any later file change requires another complete" in prompt
