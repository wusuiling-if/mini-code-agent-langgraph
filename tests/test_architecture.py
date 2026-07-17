import subprocess
import sys


def test_chat_import_does_not_load_agent_module():
    command = [
        sys.executable,
        "-c",
        "import sys; import mini_code_agent.chat; "
        "print('mini_code_agent.agent' in sys.modules)",
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=True)
    assert result.stdout.strip() == "False"


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
