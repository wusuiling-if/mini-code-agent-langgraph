from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import mini_code_agent.executor as executor_module
import mini_code_agent.receipt as receipt_module
from mini_code_agent.executor import BashExecutor
from mini_code_agent.security import SafeWorkspace
from mini_code_agent.utils import command_from_argv


def test_windows_command_serialization_uses_cmd_quoting():
    command = command_from_argv(
        [r"C:\Program Files\Python\python.exe", "-c", "print('ok')"],
        platform_name="nt",
    )

    assert command.startswith('"C:\\Program Files\\Python\\python.exe"')
    assert command.endswith("-c print('ok')")


def test_windows_local_commands_use_cmd_exe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executor = BashExecutor(tmp_path, sandbox_mode="none")
    monkeypatch.setattr(executor_module, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(
        executor,
        "_trusted_executable",
        lambda name: r"C:\Windows\System32\cmd.exe"
        if name in {"cmd.exe", "cmd"}
        else "",
    )

    assert executor._command_argv("py -m pytest -q") == [
        r"C:\Windows\System32\cmd.exe",
        "/d",
        "/s",
        "/c",
        '"py -m pytest -q"',
    ]


def test_windows_cmd_wrapper_preserves_nested_argument_quotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executor = BashExecutor(tmp_path, sandbox_mode="none")
    monkeypatch.setattr(executor_module, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(
        executor,
        "_trusted_executable",
        lambda name: r"C:\Windows\System32\cmd.exe"
        if name in {"cmd.exe", "cmd"}
        else "",
    )
    command = command_from_argv(
        [r"C:\Python\python.exe", "-c", "from value import answer; assert answer == 42"],
        platform_name="nt",
    )

    assert executor._command_argv(command)[-1] == f'"{command}"'


def test_windows_docker_commands_keep_container_posix_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executor = BashExecutor(tmp_path, sandbox_mode="docker")
    monkeypatch.setattr(executor_module, "_is_windows_platform", lambda: True)

    assert executor._command_argv("pytest -q") == [
        "/bin/sh",
        "-c",
        "pytest -q",
    ]


def test_windows_auto_considers_only_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executor = BashExecutor(tmp_path, sandbox_mode="auto")
    attempted: list[list[str]] = []
    monkeypatch.setattr(executor_module, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(
        executor, "_trusted_executable", lambda name: f"C:/tools/{name}"
    )

    def run(argv, **_kwargs):
        attempted.append(argv)
        return subprocess.CompletedProcess(argv, 0, "")

    monkeypatch.setattr(executor, "_run_argv", run)

    assert executor.sandbox_probe() == (True, "docker")
    assert executor._resolved_sandbox_mode == "docker"
    assert attempted == [["/bin/sh", "-c", ":"]]


def test_windows_processes_receive_a_new_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executor = BashExecutor(tmp_path, sandbox_mode="none")
    captured: dict = {}

    class Process:
        pid = 123
        returncode = 0

        def wait(self, timeout=None):
            return 0

    def popen(*_args, **kwargs):
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr(executor_module, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(executor_module.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(executor_module.subprocess, "Popen", popen)
    monkeypatch.setattr(executor, "_terminate_process_group", lambda _process: None)

    result = executor._run_argv(["cmd.exe", "/c", "exit /b 0"], sandbox=False)

    assert result.returncode == 0
    assert captured["creationflags"] == 512
    assert captured["start_new_session"] is False
    assert captured["preexec_fn"] is None


@pytest.mark.parametrize(
    "command",
    ["del /s /q C:\\", "rmdir /s /q C:\\", "format C:"],
)
def test_windows_destructive_commands_are_blocked(tmp_path: Path, command: str):
    result = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        allow_shell=True,
        sandbox_mode="none",
    ).execute_tool("bash", {"command": command})

    assert result.blocked
    assert result.exception_info.startswith("Blocked dangerous command pattern:")


def test_workspace_fallback_keeps_structured_file_tools_operational(tmp_path: Path):
    workspace = SafeWorkspace(tmp_path)
    workspace._use_descriptor_relative_io = False

    written = workspace.atomic_write_text("nested/value.txt", "hello\n")

    assert written == tmp_path / "nested" / "value.txt"
    assert workspace.read_text("nested/value.txt") == "hello\n"
    workspace.unlink_file("nested/value.txt")
    assert not written.exists()


def test_transaction_core_import_does_not_load_agent_contracts():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import mini_code_agent.transaction; "
                "print('\\n'.join(sorted(sys.modules)))"
            ),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    modules = set(result.stdout.splitlines())

    assert "mini_code_agent.contracts" not in modules
    assert "mini_code_agent.agent" not in modules
    assert "langgraph" not in modules


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows binary I/O")
def test_windows_receipt_key_round_trips_control_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    key = b"\x1a" + bytes(range(1, 32))
    monkeypatch.setattr(receipt_module.secrets, "token_bytes", lambda _size: key)

    assert receipt_module._load_or_create_key(tmp_path) == key
    assert receipt_module._load_existing_key(tmp_path) == key
