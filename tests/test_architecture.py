import subprocess
import sys
from pathlib import Path

import pytest

from mini_code_agent.contracts import ToolResult
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


class _FakeExecutor:
    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.redactor = _IdentityRedactor()

    def workspace_fingerprint(self, *, ignore_paths=None) -> str:
        return "stable-fingerprint"

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

    outcome = execute_tool_batch(
        executor,
        [{"name": "list_files", "args": {}, "id": "list-1"}],
        gate,
    )

    assert [call.tool_call_id for call in outcome.calls] == ["list-1"]
    assert outcome.calls[0].result.output == "fake result"


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
