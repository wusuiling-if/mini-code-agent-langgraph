from __future__ import annotations

import argparse
import math
import os
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mini_code_agent import __version__
from mini_code_agent.checks import (
    VerificationCheck,
    normalize_verification_checks,
)
from mini_code_agent.utils import command_from_argv


# These nullable seams keep CLI imports lightweight while preserving the
# existing monkeypatch surface used by embedding applications and tests.
MiniCodeAgent = None
ConversationalCodeAgent = None
BashExecutor = None
ToolResult = None
create_model = None


READ_ONLY_CHAT_TOOLS = {"list_files", "search_files", "read_file", "git_diff"}


def _load_mini_code_agent():
    global MiniCodeAgent
    if MiniCodeAgent is None:
        from mini_code_agent.agent import MiniCodeAgent as implementation

        MiniCodeAgent = implementation
    return MiniCodeAgent


def _load_conversational_code_agent():
    global ConversationalCodeAgent
    if ConversationalCodeAgent is None:
        from mini_code_agent.chat import ConversationalCodeAgent as implementation

        ConversationalCodeAgent = implementation
    return ConversationalCodeAgent


def _load_bash_executor():
    global BashExecutor
    if BashExecutor is None:
        from mini_code_agent.executor import BashExecutor as implementation

        BashExecutor = implementation
    return BashExecutor


def _load_tool_result():
    global ToolResult
    if ToolResult is None:
        from mini_code_agent.executor import ToolResult as implementation

        ToolResult = implementation
    return ToolResult


def _load_create_model():
    global create_model
    if create_model is None:
        from mini_code_agent.model import create_model as implementation

        create_model = implementation
    return create_model


class ChatAccessController:
    """Enforce read-only /ask mode without changing the agent/tool runtime API."""

    def __init__(self, executor: Any, *, coding_enabled: bool = True):
        self._executor = executor
        self._coding_enabled = coding_enabled
        self.mode = "ask"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._executor, name)

    def execute_tool(self, name: str, args: dict[str, Any]) -> Any:
        if not self._coding_enabled and name not in READ_ONLY_CHAT_TOOLS:
            return _load_tool_result()(
                tool=name,
                output=(
                    f"{name} requires an authoritative verification check. Restart with "
                    "--test-command '<command>' or --check NAME '<command>'."
                ),
                returncode=-1,
                duration_ms=0,
                exception_info="TestCommandRequired",
                blocked=True,
            )
        if self.mode == "ask" and name not in READ_ONLY_CHAT_TOOLS:
            return _load_tool_result()(
                tool=name,
                output=f"{name} is blocked in read-only /ask mode. The user must enter /code first.",
                returncode=-1,
                duration_ms=0,
                exception_info="ReadOnlyChatMode",
                blocked=True,
            )
        return self._executor.execute_tool(name, args)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _non_empty_command(value: str) -> str:
    command = value.strip()
    if not command:
        raise argparse.ArgumentTypeError("must not be blank")
    return command


def _configured_verification_checks(
    args: argparse.Namespace, *, required: bool
) -> tuple[tuple[VerificationCheck, ...], tuple[VerificationCheck, ...]]:
    explicit = tuple(
        VerificationCheck(name, command)
        for name, command in (getattr(args, "checks", None) or ())
    )
    combined = normalize_verification_checks(
        getattr(args, "test_command", None), explicit
    )
    if required and not combined:
        raise RuntimeError(
            "configure --test-command '<command>' or at least one "
            "--check NAME '<command>'"
        )
    return combined, explicit


def _state_root() -> Path:
    override = os.getenv("MCA_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "mini-code-agent"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "mini-code-agent" / "state"
    return Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "mini-code-agent"


def _config_root() -> Path:
    override = os.getenv("MCA_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming")) / "mini-code-agent"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "mini-code-agent"
    return Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config")) / "mini-code-agent"


def _ensure_private_directory(path: Path) -> Path:
    path = path.expanduser()
    if path.is_symlink():
        raise RuntimeError(f"private state path must not be a symlink: {path}")
    path = path.resolve()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"private state path is not a real directory: {path}")
    if os.name != "nt":
        metadata = path.stat()
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError(f"private state path is not owned by this user: {path}")
        path.chmod(0o700)
    return path


def _reserve_output_path(explicit: Path | None, kind: str) -> Path:
    if explicit is None:
        state_root = _ensure_private_directory(_state_root())
        directory = _ensure_private_directory(state_root / "runs")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        path = directory / f"{timestamp}-{secrets.token_hex(4)}.{kind}.json"
    else:
        path = explicit.expanduser().resolve()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"output file already exists; refusing to overwrite: {path}") from exc
    os.close(descriptor)
    if os.name != "nt":
        path.chmod(0o600)
    return path


def _resume_output_path(
    resume: Path | None, explicit: Path | None, kind: str
) -> Path:
    if resume is None:
        return _reserve_output_path(explicit, kind)
    source = resume.expanduser()
    metadata = source.lstat()
    if source.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"resume trajectory must be a regular non-symlink file: {source}")
    source = source.resolve()
    if explicit is not None and explicit.expanduser().resolve() != source:
        return _reserve_output_path(explicit, kind)
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise PermissionError("resume trajectory is not owned by the current user")
    if os.name != "nt":
        source.chmod(0o600)
    return source


def _load_secure_env_file(path: Path) -> None:
    from mini_code_agent.security import load_env_file

    path = path.expanduser()
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"env file does not exist: {path}") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"env file must be a regular, non-symlink file: {path}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise PermissionError(f"env file is not owned by this user: {path}")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError(f"env file permissions are too broad: {path}; run chmod 600 {path}")
    load_env_file(path)


def _load_runtime_env(explicit: Path | None) -> Path | None:
    """Load an explicit env file or the private file created by ``mca init``."""

    candidate = explicit.expanduser() if explicit is not None else _config_root() / "env"
    if explicit is None and not candidate.exists():
        return None
    _load_secure_env_file(candidate)
    return candidate


def _model_from_args(args: argparse.Namespace):
    return _load_create_model()(
        args.model,
        provider=args.provider,
        base_url=args.base_url,
        request_timeout=args.request_timeout,
        max_retries=args.max_retries,
        deepseek_thinking=args.deepseek_thinking,
    )


def _add_transaction_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", required=True, help="Model name.")
    parser.add_argument("--provider", choices=["auto", "deepseek", "openai"], default="auto")
    parser.add_argument("--base-url", default=None, help="Provider API base URL.")
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--max-steps", type=_positive_int, default=50)
    parser.add_argument("--context-chars", type=_positive_int, default=60_000)
    parser.add_argument("--timeout", type=_positive_int, default=30)
    parser.add_argument("--request-timeout", type=_positive_float, default=60.0)
    parser.add_argument("--max-retries", type=_non_negative_int, default=2)
    parser.add_argument("--deepseek-thinking", action="store_true")
    parser.add_argument("--test-command", type=_non_empty_command, default=None)
    parser.add_argument(
        "--check",
        dest="checks",
        action="append",
        nargs=2,
        metavar=("NAME", "COMMAND"),
        default=None,
    )
    parser.add_argument("--allow-zero-tests", action="store_true")
    parser.add_argument("--allow-shell", action="store_true")
    parser.add_argument(
        "--sandbox",
        choices=["auto", "sandbox-exec", "bwrap", "docker", "none"],
        default="auto",
    )
    parser.add_argument("--docker-image", default=None)
    parser.add_argument("--yolo", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Alias for --yolo.")
    parser.add_argument("--quiet", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mca", description="Mini LangGraph coding agent.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run the coding agent on a task.")
    run.add_argument("task", nargs="?", help="Task for a new agent run.")
    run.add_argument("--cwd", default=None, help="Project directory. Defaults to current directory.")
    run.add_argument("--resume", type=Path, default=None, help="Resume an unfinished run trajectory.")
    run.add_argument("--model", required=True, help="Model name.")
    run.add_argument("--provider", choices=["auto", "deepseek", "openai"], default="auto")
    run.add_argument("--base-url", default=None, help="Provider API base URL.")
    run.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Load API keys from this file; defaults to the private file created by mca init.",
    )
    run.add_argument("--max-steps", type=_positive_int, default=50, help="Maximum model calls.")
    run.add_argument(
        "--context-chars",
        type=_positive_int,
        default=60_000,
        help="Hard approximate character budget for model context.",
    )
    run.add_argument("--timeout", type=_positive_int, default=30, help="Timeout per command in seconds.")
    run.add_argument("--request-timeout", type=_positive_float, default=60.0, help="LLM request timeout in seconds.")
    run.add_argument("--max-retries", type=_non_negative_int, default=2, help="LLM request retries.")
    run.add_argument(
        "--deepseek-thinking",
        action="store_true",
        help="Enable DeepSeek thinking mode (disabled by default for predictable tool loops).",
    )
    run.add_argument(
        "--test-command",
        type=_non_empty_command,
        default=None,
        help="Default command for run_tests.",
    )
    run.add_argument(
        "--check",
        dest="checks",
        action="append",
        nargs=2,
        metavar=("NAME", "COMMAND"),
        default=None,
        help="Add a named authoritative verification check; repeatable.",
    )
    run.add_argument(
        "--allow-zero-tests",
        action="store_true",
        help="Allow a recognized zero-test result to satisfy verification.",
    )
    run.add_argument("--output", type=Path, default=None, help="Trajectory JSON path.")
    run.add_argument("--allow-shell", action="store_true", help="Allow arbitrary bash tool calls.")
    run.add_argument(
        "--sandbox",
        choices=["auto", "sandbox-exec", "bwrap", "docker", "none"],
        default="auto",
        help="Sandbox shell/test commands; auto fails closed if no backend works.",
    )
    run.add_argument(
        "--docker-image",
        default=None,
        help="Pre-pulled image for the Docker sandbox; defaults to MCA_DOCKER_IMAGE or python:3.11-slim.",
    )
    run.add_argument("--allow-dirty", action="store_true", help="Allow running in a dirty git worktree.")
    run.add_argument("--require-clean", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--yolo", action="store_true", help="Run commands without confirmation.")
    run.add_argument("--yes", action="store_true", help="Alias for --yolo.")
    run.add_argument("--quiet", action="store_true", help="Hide step output.")

    transaction = subparsers.add_parser(
        "tx", help="Run and commit coding work as a recoverable transaction."
    )
    transaction_commands = transaction.add_subparsers(
        dest="transaction_command", required=True
    )
    transaction_run = transaction_commands.add_parser(
        "run", help="Run an agent in a new isolated transaction."
    )
    transaction_run.add_argument("task", help="Task for the transactional run.")
    transaction_run.add_argument(
        "--cwd", default=None, help="Clean Git worktree root. Defaults to current directory."
    )
    _add_transaction_runtime_arguments(transaction_run)
    transaction_resume = transaction_commands.add_parser(
        "resume", help="Resume an interrupted open transaction."
    )
    transaction_resume.add_argument("transaction_id")
    _add_transaction_runtime_arguments(transaction_resume)
    for name, help_text in (
        ("status", "Show durable transaction state."),
        ("receipt", "Verify and show a prepared transaction receipt."),
        ("commit", "Apply a prepared transaction to its source worktree."),
        ("abort", "Discard an isolated transaction worktree."),
    ):
        command = transaction_commands.add_parser(name, help=help_text)
        command.add_argument("transaction_id")
    transaction_commands.add_parser(
        "demo", help="Demonstrate successful commit and conflict refusal without an API key."
    )

    chat = subparsers.add_parser("chat", help="Start a persistent chat-and-code session.")
    chat.add_argument("--cwd", default=None, help="Project directory. Defaults to current directory.")
    chat.add_argument("--resume", type=Path, default=None, help="Resume a saved chat trajectory.")
    chat.add_argument("--model", default="deepseek", help="Chat model name.")
    chat.add_argument("--provider", choices=["auto", "deepseek", "openai"], default="auto")
    chat.add_argument("--base-url", default=None, help="Provider API base URL.")
    chat.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Load API keys from this file; defaults to the private file created by mca init.",
    )
    chat.add_argument("--max-steps", type=_positive_int, default=20, help="Maximum model calls per user turn.")
    chat.add_argument(
        "--context-chars",
        type=_positive_int,
        default=60_000,
        help="Hard approximate character budget for persistent model context.",
    )
    chat.add_argument("--timeout", type=_positive_int, default=30, help="Timeout per command in seconds.")
    chat.add_argument("--request-timeout", type=_positive_float, default=60.0, help="LLM request timeout in seconds.")
    chat.add_argument("--max-retries", type=_non_negative_int, default=2, help="LLM request retries.")
    chat.add_argument(
        "--deepseek-thinking",
        action="store_true",
        help="Enable DeepSeek thinking mode; reasoning_content is preserved across tool calls.",
    )
    chat.add_argument(
        "--test-command",
        type=_non_empty_command,
        default=None,
        help="Configure authoritative test verification and enable /code mode.",
    )
    chat.add_argument(
        "--check",
        dest="checks",
        action="append",
        nargs=2,
        metavar=("NAME", "COMMAND"),
        default=None,
        help="Add a named authoritative verification check; repeatable.",
    )
    chat.add_argument(
        "--allow-zero-tests",
        action="store_true",
        help="Allow a recognized zero-test result to satisfy verification.",
    )
    chat.add_argument("--output", type=Path, default=None, help="Session trajectory JSON path.")
    chat.add_argument("--allow-shell", action="store_true")
    chat.add_argument(
        "--sandbox",
        choices=["auto", "sandbox-exec", "bwrap", "docker", "none"],
        default="auto",
    )
    chat.add_argument(
        "--docker-image",
        default=None,
        help="Pre-pulled image for the Docker sandbox; defaults to MCA_DOCKER_IMAGE or python:3.11-slim.",
    )
    chat.add_argument("--allow-dirty", action="store_true")
    chat.add_argument("--yes", action="store_true", help="Approve writes and commands without prompting.")
    chat.add_argument("--quiet", action="store_true", help="Hide tool output.")

    trace = subparsers.add_parser("trace", help="Summarize a trajectory file.")
    trace.add_argument("trajectory", type=Path)
    trace.add_argument("--diff", action="store_true", help="Print file diffs captured in the trajectory.")

    undo = subparsers.add_parser("undo", help="Undo structured file edits from a trajectory.")
    undo.add_argument("trajectory", type=Path)
    undo.add_argument("--dry-run", action="store_true", help="Show undo actions without writing files.")
    undo.add_argument("--force", action="store_true", help="Undo even if files changed after the agent edit.")
    undo.add_argument(
        "--allow-legacy-unsafe",
        action="store_true",
        help="Allow unsigned 0.1/0.2 undo data (unsafe; inspect the file first).",
    )

    init = subparsers.add_parser("init", help="Create a private env/config starter file.")
    init.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Env file path. Defaults to the per-user config directory.",
    )

    subparsers.add_parser(
        "demo",
        help="Run a deterministic no-key coding demo in a temporary workspace.",
    )

    sandbox = subparsers.add_parser(
        "sandbox", help="Inspect command isolation capabilities."
    )
    sandbox_commands = sandbox.add_subparsers(
        dest="sandbox_command", required=True
    )
    probe = sandbox_commands.add_parser(
        "probe", help="Run disposable isolation checks."
    )
    probe.add_argument(
        "--sandbox",
        choices=["auto", "sandbox-exec", "bwrap", "docker"],
        default="auto",
    )
    probe.add_argument("--docker-image", default=None)
    probe.add_argument("--timeout", type=_positive_int, default=10)

    doctor = subparsers.add_parser(
        "doctor",
        help="Inspect local prerequisites without reading secret values.",
    )
    doctor.add_argument("--cwd", default=".", help="Workspace directory to inspect.")
    doctor.add_argument(
        "--sandbox",
        choices=["auto", "sandbox-exec", "bwrap", "docker", "none"],
        default="auto",
        help="Sandbox backend expected for run/chat.",
    )
    doctor.add_argument(
        "--provider",
        choices=["auto", "deepseek", "openai"],
        default="auto",
        help="Provider whose environment configuration should be checked.",
    )
    doctor.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Inspect this env file's metadata without reading its contents.",
    )
    return parser


def run_agent(args: argparse.Namespace) -> int:
    _combined_checks, explicit_checks = _configured_verification_checks(
        args, required=True
    )
    if args.model == "mock":
        raise RuntimeError(
            "The scripted mock is not a general coding model; use `mca demo` for the no-key demo."
        )
    from mini_code_agent.trajectory import collect_file_diffs, load_trajectory

    auto_approve = args.yolo or args.yes
    if not auto_approve and not sys.stdin.isatty():
        raise RuntimeError("Confirmation mode needs an interactive terminal. Re-run with --yes for unattended use.")

    resume_data = load_trajectory(args.resume) if args.resume else None
    if resume_data is None and not (args.task or "").strip():
        raise RuntimeError("a task is required unless --resume is used")
    cwd_source = args.cwd or (resume_data or {}).get("cwd") or "."
    cwd = Path(cwd_source).resolve()
    if not cwd.is_dir():
        raise FileNotFoundError(f"cwd is not a directory: {cwd}")
    if resume_data is None and not args.allow_dirty and _git_dirty(cwd):
        raise RuntimeError(f"git worktree is dirty: {cwd}. Commit/stash changes or re-run with --allow-dirty.")
    _load_runtime_env(args.env_file)

    executor = _load_bash_executor()(
        cwd,
        timeout_seconds=args.timeout,
        approval_mode="yolo" if auto_approve else "confirm",
        allow_shell=args.allow_shell,
        default_test_command=args.test_command,
        verification_checks=explicit_checks,
        allow_zero_tests=args.allow_zero_tests,
        sandbox_mode=args.sandbox,
        docker_image=args.docker_image
        or os.getenv("MCA_DOCKER_IMAGE", "python:3.11-slim"),
    )
    model = _model_from_args(args)
    _require_working_sandbox(executor)
    output = _resume_output_path(args.resume, args.output, "traj")
    agent = _load_mini_code_agent()(
        model,
        executor,
        max_steps=args.max_steps,
        context_char_budget=args.context_chars,
        trajectory_path=output,
        quiet=args.quiet,
    )
    trajectory = agent.run(args.task or "", resume_data=resume_data)
    print(f"\nexit_status: {trajectory['exit_status']}")
    print(f"trajectory: {output.resolve()}")
    print(f"sandbox: {trajectory.get('sandbox', 'unknown')}")
    _print_workspace_changes(trajectory.get("workspace_changes", {}))
    diff = collect_file_diffs(trajectory)
    if diff:
        print("\nfile_diff:")
        print(diff)
    if trajectory.get("submission"):
        print("\nsubmission:")
        print(trajectory["submission"])
    return 0 if trajectory["exit_status"] == "Submitted" else 2


def chat_command(args: argparse.Namespace) -> int:
    combined_checks, explicit_checks = _configured_verification_checks(
        args, required=False
    )
    coding_enabled = bool(combined_checks)
    from mini_code_agent.trajectory import load_trajectory

    if not sys.stdin.isatty():
        raise RuntimeError("Chat mode needs an interactive terminal.")
    if args.model == "mock":
        raise RuntimeError("The scripted mock model is for run/tests only; choose a real model for chat.")
    resume_data = load_trajectory(args.resume) if args.resume else None
    cwd_source = args.cwd or (resume_data or {}).get("cwd") or "."
    cwd = Path(cwd_source).resolve()
    if not cwd.is_dir():
        raise FileNotFoundError(f"cwd is not a directory: {cwd}")
    if resume_data is None and not args.allow_dirty and _git_dirty(cwd):
        raise RuntimeError(f"git worktree is dirty: {cwd}. Commit/stash changes or re-run with --allow-dirty.")
    _load_runtime_env(args.env_file)

    executor = _load_bash_executor()(
        cwd,
        timeout_seconds=args.timeout,
        approval_mode="yolo" if args.yes else "confirm",
        allow_shell=args.allow_shell,
        default_test_command=args.test_command,
        verification_checks=explicit_checks,
        allow_zero_tests=args.allow_zero_tests,
        sandbox_mode=args.sandbox,
        docker_image=args.docker_image
        or os.getenv("MCA_DOCKER_IMAGE", "python:3.11-slim"),
    )
    model = _model_from_args(args)
    if coding_enabled:
        _require_working_sandbox(executor)
    output = _resume_output_path(args.resume, args.output, "chat")
    access = ChatAccessController(executor, coding_enabled=coding_enabled)
    session = _load_conversational_code_agent()(
        model,
        access,
        max_steps_per_turn=args.max_steps,
        context_char_budget=args.context_chars,
        trajectory_path=output,
        quiet=args.quiet,
        resume_data=resume_data,
    )
    print(f"mini-code-agent chat | model={args.model} | cwd={cwd}")
    if resume_data is not None:
        print(f"resumed={args.resume.expanduser().resolve()}")
    print("mode=/ask (read-only) | use /code before allowing edits or command execution")
    print("Commands: /ask, /code, /help, /clear, /exit")
    try:
        while True:
            user_text = input("\nyou> ").strip()
            if not user_text:
                continue
            if user_text in {"/exit", "/quit"}:
                break
            if user_text == "/help":
                print(
                    "/ask [question] selects enforced read-only chat; /code [task] allows edits/tests "
                    "with the configured confirmations. /clear resets context; /exit closes."
                )
                continue
            if user_text == "/clear":
                session.clear_context()
                print("context cleared")
                continue
            command, separator, remainder = user_text.partition(" ")
            if command in {"/ask", "/code"}:
                if command == "/code" and not coding_enabled:
                    access.mode = "ask"
                    print(
                        "/code is unavailable: restart with --test-command '<command>' "
                        "or --check NAME '<command>' to enable coding. Staying in /ask mode."
                    )
                    continue
                access.mode = command.removeprefix("/")
                print(f"mode={command}")
                if access.mode == "code" and args.yes:
                    print("warning: --yes is active; code-mode writes and commands will not prompt")
                if not separator or not remainder.strip():
                    continue
                user_text = remainder.strip()
            mode_instruction = (
                "Read-only /ask mode is enforced: explain or inspect only; do not request write, shell, or test tools."
                if access.mode == "ask"
                else "The user explicitly selected /code mode; coding tools are available under the configured approvals."
            )
            turn = session.respond_turn(
                f"[{mode_instruction}]\n\n{user_text}",
                coding_mode=access.mode == "code",
            )
            if turn.text:
                print(f"\nagent> {turn.text}")
            print(
                "turn: "
                f"status={turn.status} "
                f"completed={str(turn.completed).lower()} "
                f"verified={str(turn.verified).lower()} "
                f"steps={turn.steps} "
                f"error={turn.error or 'none'}"
            )
            if turn.status == "submitted" and access.mode == "code":
                access.mode = "ask"
                print("mode=/ask (coding turn submitted; use /code for another coding task)")
    except (EOFError, KeyboardInterrupt):
        print()
    finally:
        session.close()
    print(f"session: {output.resolve()}")
    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "run":
            raise SystemExit(run_agent(args))
        if args.command == "tx" and args.transaction_command == "run":
            from mini_code_agent.transaction_cli import agent_command

            raise SystemExit(agent_command(args, resume=False))
        if args.command == "tx" and args.transaction_command == "resume":
            from mini_code_agent.transaction_cli import agent_command

            raise SystemExit(agent_command(args, resume=True))
        if args.command == "tx" and args.transaction_command == "demo":
            from mini_code_agent.transaction_cli import (
                demo_command as transaction_demo_command,
            )

            raise SystemExit(transaction_demo_command(args))
        if args.command == "tx":
            from mini_code_agent.transaction_cli import state_command

            raise SystemExit(state_command(args))
        if args.command == "chat":
            raise SystemExit(chat_command(args))
        if args.command == "trace":
            raise SystemExit(trace_command(args))
        if args.command == "undo":
            raise SystemExit(undo_command(args))
        if args.command == "init":
            raise SystemExit(init_command(args))
        if args.command == "demo":
            raise SystemExit(demo_command(args))
        if args.command == "sandbox" and args.sandbox_command == "probe":
            raise SystemExit(sandbox_probe_command(args))
        if args.command == "doctor":
            raise SystemExit(doctor_command(args))
        parser.print_help()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


def trace_command(args: argparse.Namespace) -> int:
    from mini_code_agent.trajectory import (
        collect_file_diffs,
        load_trajectory,
        summarize_trajectory,
    )

    data = load_trajectory(args.trajectory)
    print(summarize_trajectory(data))
    if args.diff:
        diff = collect_file_diffs(data)
        print("\nfile_diff:")
        print(diff or "(none)")
    return 0


def undo_command(args: argparse.Namespace) -> int:
    from mini_code_agent.trajectory import load_trajectory, undo_trajectory

    data = load_trajectory(args.trajectory)
    actions = undo_trajectory(
        data,
        dry_run=args.dry_run,
        force=args.force,
        allow_legacy_unsafe=args.allow_legacy_unsafe,
    )
    if not actions:
        print("nothing to undo")
        return 0
    for action in actions:
        print(("would " if args.dry_run else "") + action)
    return 0


def init_command(args: argparse.Namespace) -> int:
    if args.path is None:
        path = _ensure_private_directory(_config_root()) / "env"
    else:
        path = args.path.expanduser().resolve()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = (
        "DEEPSEEK_API_KEY=\n"
        "# DEEPSEEK_BASE_URL=https://api.deepseek.com\n"
        "# OPENAI_API_KEY=\n"
        "# OPENAI_BASE_URL=https://api.openai.com/v1\n"
        "# MCA_API_KEY=not-needed\n"
        "# MCA_BASE_URL=http://127.0.0.1:8000/v1\n"
        "# MCA_REDACT=comma,separated,extra,secrets\n"
    )
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"file already exists: {path}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    if os.name != "nt":
        path.chmod(0o600)
    print(f"created {path}")
    return 0


def _write_demo_fixture(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        root.chmod(0o700)
    (root / "calculator.py").write_text(
        "def add(a: int, b: int) -> int:\n"
        "    return a - b\n\n\n"
        "def multiply(a: int, b: int) -> int:\n"
        "    return a * b\n",
        encoding="utf-8",
    )
    (root / "test_calculator.py").write_text(
        "import unittest\n\n"
        "from calculator import add, multiply\n\n\n"
        "class CalculatorTest(unittest.TestCase):\n"
        "    def test_add_positive_numbers(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n\n"
        "    def test_add_negative_numbers(self):\n"
        "        self.assertEqual(add(-2, -3), -5)\n\n"
        "    def test_multiply(self):\n"
        "        self.assertEqual(multiply(4, 5), 20)\n\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )


def _create_demo_workspace() -> Path:
    forbidden_root = _demo_forbidden_root()
    candidates = [Path(tempfile.gettempdir())]
    if os.name != "nt":
        candidates.append(Path("/tmp"))
    candidates.append(_state_root() / "demos")
    seen: set[Path] = set()
    for candidate in candidates:
        parent = candidate.expanduser().resolve()
        if parent in seen:
            continue
        seen.add(parent)
        try:
            parent.relative_to(forbidden_root)
        except ValueError:
            pass
        else:
            continue
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            root = Path(tempfile.mkdtemp(prefix="mca-demo-", dir=parent)).resolve()
        except OSError:
            continue
        try:
            root.relative_to(forbidden_root)
        except ValueError:
            return root
        shutil.rmtree(root, ignore_errors=True)
    raise RuntimeError("could not create a demo workspace outside the current directory")


def _demo_forbidden_root() -> Path:
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        try:
            (candidate / ".git").lstat()
        except OSError:
            continue
        return candidate
    return cwd


def _platform_supports_demo() -> bool:
    return True


def demo_command(_args: argparse.Namespace) -> int:
    if not _platform_supports_demo():
        raise RuntimeError(
            "mca demo is not supported by native Windows command execution; "
            "run it from WSL2 or Linux instead"
        )
    root = _create_demo_workspace()
    _write_demo_fixture(root)
    trajectory_path = root.with_suffix(".traj.json")
    test_command = command_from_argv(
        [sys.executable, "-m", "unittest", "discover", "-v"]
    )
    executor = _load_bash_executor()(
        root,
        approval_mode="yolo",
        sandbox_mode="none",
        default_test_command=test_command,
    )
    agent = _load_mini_code_agent()(
        _load_create_model()("mock"),
        executor,
        trajectory_path=trajectory_path,
        quiet=True,
    )
    trajectory = agent.run("Fix the failing calculator tests")
    exit_status = trajectory.get("exit_status", "unknown")
    verification = trajectory.get("verification_status", "unknown")
    print(f"demo_workspace: {root}")
    print(f"exit_status: {exit_status}")
    print(f"tests: {verification}")
    print(f"trajectory: {trajectory_path}")
    print(f"next: mca trace {shlex.quote(str(trajectory_path))} --diff")
    return 0 if exit_status == "Submitted" else 2


def doctor_command(args: argparse.Namespace) -> int:
    from mini_code_agent.diagnostics import run_diagnostics

    checks = run_diagnostics(
        args.cwd,
        sandbox=args.sandbox,
        provider=args.provider,
        env_file=args.env_file,
    )
    counts = {"pass": 0, "warn": 0, "fail": 0}
    for check in checks:
        counts[check.status] += 1
        print(f"[{check.status.upper()}] {check.name}: {check.detail}")
    print(
        "summary: "
        f"pass={counts['pass']} warn={counts['warn']} fail={counts['fail']}"
    )
    return 1 if counts["fail"] else 0


def sandbox_probe_command(args: argparse.Namespace) -> int:
    from mini_code_agent.sandbox_probe import run_sandbox_probe

    report = run_sandbox_probe(
        sandbox_mode=args.sandbox,
        docker_image=args.docker_image
        or os.getenv("MCA_DOCKER_IMAGE", "python:3.11-slim"),
        timeout_seconds=args.timeout,
    )
    for check in report.checks:
        status = "PASS" if check.passed else "FAIL"
        print(f"[{status}] {check.name}: {check.detail}")
    return 0 if report.ok else 1


def _git_dirty(cwd: Path) -> bool:
    git_path = shutil.which("git")
    if not git_path:
        return False
    git_path = str(Path(git_path).resolve())
    try:
        Path(git_path).relative_to(cwd)
    except ValueError:
        pass
    else:
        raise RuntimeError("refusing to execute git from inside the target workspace")
    git = [
        git_path,
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.pager=cat",
    ]
    git_env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "LANG": "C",
    }
    try:
        inside = subprocess.run(
            [*git, "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            env=git_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if inside.returncode != 0:
            return False
        root = subprocess.run(
            [*git, "rev-parse", "--show-toplevel"],
            cwd=cwd,
            env=git_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if root.returncode != 0:
            return False
        git_root = Path(root.stdout.strip()).resolve()
        rel = cwd.relative_to(git_root)
        rel_arg = "." if str(rel) == "." else str(rel)
        status = subprocess.run(
            [*git, "status", "--porcelain", "--untracked-files=all", "--", rel_arg],
            cwd=git_root,
            env=git_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if status.returncode != 0:
            raise RuntimeError(
                f"git status failed with exit code {status.returncode}"
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"could not safely inspect git worktree: {exc}") from exc
    return bool(status.stdout.strip())


def _print_workspace_changes(changes: dict) -> None:
    created = changes.get("created", [])
    modified = changes.get("modified", [])
    deleted = changes.get("deleted", [])
    if not any([created, modified, deleted]):
        print("workspace_changes: none")
        return
    print("workspace_changes:")
    for label, paths in [("created", created), ("modified", modified), ("deleted", deleted)]:
        if paths:
            print(f"  {label}:")
            for path in paths:
                print(f"    {path}")


def _require_working_sandbox(executor: Any) -> None:
    ok, detail = executor.sandbox_probe()
    if not ok:
        raise RuntimeError(
            f"sandbox is not usable: {detail}. Choose a working backend or explicitly pass --sandbox none."
        )
