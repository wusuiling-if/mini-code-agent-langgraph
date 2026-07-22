from __future__ import annotations

import os
import re
import secrets
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import hashlib
import regex as safe_regex
from contextlib import contextmanager
from difflib import unified_diff
from dataclasses import dataclass
from pathlib import Path
from shlex import join as shlex_join
from typing import Any, Literal, Sequence

from mini_code_agent.checks import (
    VerificationCheck,
    VerificationCheckEvidence,
    VerificationCheckExecution,
    normalize_verification_checks,
    run_verification_matrix,
)
from mini_code_agent.contracts import ToolResult
from mini_code_agent.security import SafeWorkspace, SecretRedactor
from mini_code_agent.utils import DEFAULT_OUTPUT_LIMIT, truncate_text
from mini_code_agent.workspace import WorkspaceFingerprinter

UNITTEST_COUNT = re.compile(r"(?m)^Ran\s+(\d+)\s+tests?\s+in\s+")
PYTEST_ZERO = re.compile(
    r"(?im)(?:^collected 0 items\s*$|^no tests ran(?: in .*)?\s*$)"
)
MAX_COMMAND_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_STRUCTURED_EDIT_CHARS = 8 * 1024 * 1024
PROCESS_TERMINATION_GRACE_SECONDS = 0.5
DOCKER_CLEANUP_TIMEOUT_SECONDS = 5
DOCKER_WRAPPER_FAILURE_CODES = frozenset({125, 126, 127})

DANGEROUS_COMMAND_PATTERNS = [
    re.compile(r"(^|[;&|]\s*)rm\s+.*-[^\n]*[rf][^\n]*\s+(/|\$HOME|~)(\s|$)"),
    re.compile(r"(^|[;&|]\s*)git\s+reset\s+--hard(\s|$)"),
    re.compile(r"(^|[;&|]\s*)git\s+checkout\s+--\s+"),
    re.compile(r"(^|[;&|]\s*)git\s+clean\s+-[^\n]*[fdx]"),
    re.compile(r"(^|[;&|]\s*)sudo(\s|$)"),
    re.compile(r"(^|[;&|]\s*)chmod\s+-R\s+777\s+(/|\$HOME|~)"),
    re.compile(r"(^|[;&|]\s*)chown\s+-R\s+"),
    re.compile(r"(^|[;&|]\s*)mkfs(\.|\s|$)"),
    re.compile(r"(^|[;&|]\s*)dd\s+.*\bof=/dev/"),
    re.compile(r"(curl|wget)[^\n|;]*(\|\s*(sh|bash))"),
    re.compile(r">\s*/(etc|bin|sbin|usr|System|Library)/"),
]

SKIP_DIR_NAMES = frozenset({
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
    "venv",
})

SAFE_INHERITED_ENV = frozenset({
    "PATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "COLORTERM",
    "TZ",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "SYSTEMROOT",
    "WINDIR",
})


@dataclass(frozen=True)
class _DockerRunMetadata:
    executable: str
    name: str
    cidfile: Path


class SandboxExecutionError(RuntimeError):
    """A sandbox wrapper failed before ordinary command results were available."""

    def __init__(
        self, backend: str, returncode: int, output: str
    ) -> None:
        super().__init__(
            f"{backend} sandbox lifecycle failed with exit code {returncode}"
        )
        self.returncode = returncode
        self.output = output


class _TerminationSignal(BaseException):
    """Defer SIGTERM propagation until the active child has been reaped."""

    def __init__(self, signum: int, previous_handler: Any, frame: Any):
        super().__init__(signum)
        self.signum = signum
        self.previous_handler = previous_handler
        self.frame = frame

    def propagate(self) -> None:
        if callable(self.previous_handler):
            self.previous_handler(self.signum, self.frame)
        raise SystemExit(128 + self.signum)


@contextmanager
def _defer_sigterm_until_cleanup():
    """Turn the main thread's default SIGTERM into a cleanup-safe exception."""

    if (
        not hasattr(signal, "SIGTERM")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)
    if previous == signal.SIG_IGN:
        yield
        return

    def handle(signum, frame):
        raise _TerminationSignal(signum, previous, frame)

    signal.signal(signal.SIGTERM, handle)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


class BashExecutor:
    def __init__(
        self,
        cwd: Path,
        *,
        timeout_seconds: int = 30,
        approval_mode: Literal["confirm", "yolo"] = "confirm",
        allow_shell: bool = False,
        default_test_command: str | None = None,
        verification_checks: Sequence[VerificationCheck] = (),
        allow_zero_tests: bool = False,
        sandbox_mode: Literal["auto", "sandbox-exec", "bwrap", "docker", "none"] = "auto",
        docker_image: str = "python:3.11-slim",
        env: dict[str, str] | None = None,
        redactor: SecretRedactor | None = None,
    ):
        self.workspace = SafeWorkspace(cwd)
        self.cwd = self.workspace.cwd
        self.timeout_seconds = timeout_seconds
        self.approval_mode = approval_mode
        self.allow_shell = allow_shell
        if default_test_command is not None:
            if not isinstance(default_test_command, str) or not default_test_command.strip():
                raise ValueError("default_test_command must not be blank")
            default_test_command = default_test_command.strip()
        self.default_test_command = default_test_command
        explicit_checks = tuple(verification_checks)
        self.verification_checks = normalize_verification_checks(
            default_test_command, explicit_checks
        )
        self._strict_verification_matrix = bool(explicit_checks)
        self.allow_zero_tests = allow_zero_tests
        self.sandbox_mode = sandbox_mode
        if not isinstance(docker_image, str) or not docker_image.strip():
            raise ValueError("docker_image must not be blank")
        self.docker_image = docker_image.strip()
        self._resolved_sandbox_mode: str | None = None
        self._sandbox_probe_error = ""
        self.env = env or {}
        self.redactor = redactor or SecretRedactor()
        self._git_executable = self._trusted_executable("git")
        self._workspace_fingerprinter = WorkspaceFingerprinter(self.cwd)
        self._runtime_directory = tempfile.TemporaryDirectory(prefix="mini-code-agent-")
        self._runtime_root = Path(self._runtime_directory.name)
        (self._runtime_root / "home").mkdir(mode=0o700)
        (self._runtime_root / "tmp").mkdir(mode=0o700)
        (self._runtime_root / "pycache").mkdir(mode=0o700)

    def workspace_fingerprint(self, *, ignore_paths: set[Path] | None = None):
        """Capture a cached, metadata-aware workspace snapshot."""

        return self._workspace_fingerprinter.capture(
            ignore_paths=ignore_paths or set()
        )

    def execute_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        try:
            match name:
                case "bash":
                    return self.execute_bash(str(args.get("command", "")))
                case "list_files":
                    return self.list_files(str(args.get("path", ".")), max_files=int(args.get("max_files") or 200))
                case "search_files":
                    return self.search_files(
                        str(args.get("pattern", "")),
                        str(args.get("path", ".")),
                        max_results=int(args.get("max_results") or 100),
                    )
                case "read_file":
                    return self.read_file(
                        str(args.get("path", "")),
                        start_line=int(args.get("start_line") or 1),
                        end_line=int(args["end_line"]) if args.get("end_line") is not None else None,
                        max_chars=int(args.get("max_chars") or 12000),
                    )
                case "write_file":
                    return self.write_file(str(args.get("path", "")), str(args.get("content", "")))
                case "apply_patch":
                    return self.apply_patch(
                        str(args.get("path", "")),
                        str(args.get("old", "")),
                        str(args.get("new", "")),
                        replace_all=_strict_bool(
                            args.get("replace_all", False), "replace_all"
                        ),
                    )
                case "replace_lines":
                    return self.replace_lines(
                        str(args.get("path", "")),
                        int(args.get("start_line") or 1),
                        int(args.get("end_line") or 1),
                        str(args.get("new_text", "")),
                    )
                case "git_diff":
                    return self.git_diff(str(args.get("path", "")))
                case "run_tests":
                    raw_ignore_paths = args.get("_verification_ignore_paths")
                    ignore_paths = (
                        raw_ignore_paths
                        if isinstance(raw_ignore_paths, set)
                        and all(
                            isinstance(path, Path)
                            for path in raw_ignore_paths
                        )
                        else None
                    )
                    return self.run_tests(
                        args.get("command"), ignore_paths=ignore_paths
                    )
                case "submit":
                    return self.submit(str(args.get("summary", "")))
        except Exception as exc:
            return ToolResult(
                tool=name,
                output="",
                returncode=-1,
                duration_ms=0,
                args=self.redactor.redact_data(args),
                exception_info=self.redactor.redact_text(f"{type(exc).__name__}: {exc}"),
            )
        return ToolResult(tool=name, output="", returncode=-1, duration_ms=0, exception_info=f"Unknown tool: {name}")

    def execute_bash(self, command: str) -> ToolResult:
        if not self.allow_shell:
            return ToolResult(
                tool="bash",
                command=self.redactor.redact_text(command),
                output="Arbitrary bash is disabled. Use list_files, search_files, read_file, apply_patch, run_tests, git_diff, or submit.",
                returncode=-1,
                duration_ms=0,
                exception_info="ShellDisabled",
                blocked=True,
            )
        blocked_reason = self._blocked_command_reason(command)
        if blocked_reason:
            return ToolResult(
                tool="bash",
                command=self.redactor.redact_text(command),
                output="Command was blocked by the safety policy.",
                returncode=-1,
                duration_ms=0,
                exception_info=blocked_reason,
                blocked=True,
            )
        if self.approval_mode == "confirm" and not self._confirm("bash", command):
            return ToolResult(
                tool="bash",
                command=self.redactor.redact_text(command),
                output="Command was rejected by the user.",
                returncode=-1,
                duration_ms=0,
                exception_info="User rejected command.",
                approved=False,
            )
        return self._run_command("bash", command, args={"command": command})

    def list_files(self, path: str = ".", *, max_files: int = 200) -> ToolResult:
        root = self.workspace.resolve(path)
        if not root.exists():
            raise FileNotFoundError(f"path does not exist: {path}")
        max_files = max(1, min(max_files, 5000))
        files = []
        truncated = False
        for candidate in self.workspace.iter_files(root, skip_dir_names=SKIP_DIR_NAMES):
            if len(files) >= max_files:
                truncated = True
                break
            files.append(candidate)
        lines = [str(file.relative_to(self.cwd)) for file in files]
        if truncated:
            lines.append(f"... truncated at {max_files} files")
        return ToolResult(
            tool="list_files",
            output="\n".join(lines),
            returncode=0,
            duration_ms=0,
            args={"path": path, "max_files": max_files},
        )

    def search_files(self, pattern: str, path: str = ".", *, max_results: int = 100) -> ToolResult:
        if not pattern:
            raise ValueError("pattern must not be empty")
        if len(pattern) > 2000:
            raise ValueError("pattern is too long")
        root = self.workspace.resolve(path)
        try:
            regex = safe_regex.compile(pattern)
        except safe_regex.error as exc:
            raise ValueError(f"invalid search pattern: {exc}") from exc
        max_results = max(1, min(max_results, 5000))
        results = []
        truncated = False
        scanned_bytes = 0
        max_scanned_bytes = 64 * 1024 * 1024
        for candidate in self.workspace.iter_files(root, skip_dir_names=SKIP_DIR_NAMES):
            if len(results) >= max_results:
                truncated = True
                break
            if not self.workspace.is_probably_text_file(candidate):
                continue
            try:
                with self.workspace.open_text(candidate, encoding="utf-8", errors="replace") as handle:
                    for line_number, raw_line in enumerate(handle, 1):
                        scanned_bytes += len(raw_line.encode("utf-8", errors="replace"))
                        if scanned_bytes > max_scanned_bytes:
                            truncated = True
                            break
                        line = raw_line.rstrip("\r\n")
                        try:
                            matched = regex.search(line, timeout=0.05)
                        except TimeoutError as exc:
                            raise ValueError(
                                "search pattern exceeded the per-line time limit"
                            ) from exc
                        if matched:
                            results.append(f"{candidate.relative_to(self.cwd)}:{line_number}: {line}")
                            if len(results) >= max_results:
                                truncated = True
                                break
                    if truncated and (len(results) >= max_results or scanned_bytes > max_scanned_bytes):
                        break
            except OSError:
                continue
        if truncated:
            reason = f"{max_results} matches" if len(results) >= max_results else "64 MiB scanned"
            results.append(f"... truncated at {reason}")
        return ToolResult(
            tool="search_files",
            output=truncate_text(self.redactor.redact_text("\n".join(results))),
            returncode=0,
            duration_ms=0,
            args={"pattern": pattern, "path": path, "max_results": max_results},
        )

    def read_file(
        self,
        path: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        max_chars: int = 12000,
    ) -> ToolResult:
        file_path = self.workspace.resolve(path)
        start = max(1, start_line)
        if end_line is not None and end_line < start:
            raise ValueError("end_line must be greater than or equal to start_line")
        max_chars = max(1, min(max_chars, 200_000))
        selected_parts: list[str] = []
        selected_chars = 0
        truncated = False
        try:
            with self.workspace.open_text(
                file_path, encoding="utf-8", errors="replace"
            ) as handle:
                number = 0
                while True:
                    # TextIO iteration may allocate an entire multi-gigabyte line.
                    # Bound each readline and explicitly drain skipped long lines.
                    raw_line = handle.readline(max_chars + 1)
                    if not raw_line:
                        break
                    number += 1
                    line_complete = raw_line.endswith(("\n", "\r"))
                    if number < start:
                        while not line_complete:
                            remainder = handle.readline(65_536)
                            if not remainder:
                                line_complete = True
                            elif remainder.endswith(("\n", "\r")):
                                line_complete = True
                        continue
                    if end_line is not None and number > end_line:
                        break
                    rendered = f"{number}: {raw_line.rstrip(chr(10) + chr(13))}\n"
                    if selected_chars + len(rendered) > max_chars:
                        remaining = max_chars - selected_chars
                        if remaining > 0:
                            selected_parts.append(rendered[:remaining])
                        truncated = True
                        break
                    selected_parts.append(rendered)
                    selected_chars += len(rendered)
                    if not line_complete:
                        truncated = True
                        break
        except (FileNotFoundError, IsADirectoryError):
            raise FileNotFoundError(f"file does not exist: {path}")
        selected = "".join(selected_parts).rstrip("\n")
        if truncated:
            selected += "\n... truncated at max_chars"
        return ToolResult(
            tool="read_file",
            output=self.redactor.redact_text(selected),
            returncode=0,
            duration_ms=0,
            args={"path": path, "start_line": start_line, "end_line": end_line, "max_chars": max_chars},
        )

    def write_file(self, path: str, content: str) -> ToolResult:
        if len(content) > MAX_STRUCTURED_EDIT_CHARS:
            raise ValueError("structured writes are limited to 8 MiB of text")
        file_path = self.workspace.resolve(path)
        try:
            before = self._read_editable_text(file_path)
            existed_before = True
        except FileNotFoundError:
            before = ""
            existed_before = False
        if existed_before:
            content = _normalize_newlines(content, _preferred_newline(before))
        if self.approval_mode == "confirm" and not self._confirm("write_file", path):
            return ToolResult(
                tool="write_file",
                output="Write was rejected by the user.",
                returncode=-1,
                duration_ms=0,
                args={"path": path},
                exception_info="User rejected write.",
                approved=False,
            )
        self.workspace.atomic_write_text(file_path, content, encoding="utf-8")
        diff = self._unified_file_diff(file_path, before, content)
        return ToolResult(
            tool="write_file",
            output=self.redactor.redact_text(
                f"Wrote {len(content)} chars to {file_path.relative_to(self.cwd)}.\n\n{diff}"
            ),
            returncode=0,
            duration_ms=0,
            args={"path": path},
            before_content=before,
            after_content=content,
            file_path=str(file_path.relative_to(self.cwd)),
            file_existed_before=existed_before,
            before_hash=_content_hash(before) if existed_before else "",
            after_hash=_content_hash(content),
        )

    def apply_patch(self, path: str, old: str, new: str, *, replace_all: bool = False) -> ToolResult:
        file_path = self.workspace.resolve(path)
        try:
            text = self._read_editable_text(file_path)
        except (FileNotFoundError, IsADirectoryError):
            raise FileNotFoundError(f"file does not exist: {path}")
        if not old:
            raise ValueError("old text must not be empty")
        newline = _preferred_newline(text)
        effective_old = _normalize_newlines(old, newline)
        effective_new = _normalize_newlines(new, newline)
        count = text.count(effective_old)
        if count == 0:
            raise ValueError("old text was not found")
        if count > 1 and not replace_all:
            raise ValueError(f"old text matched {count} times; set replace_all=true or provide more specific old text")
        if self.approval_mode == "confirm" and not self._confirm("apply_patch", path):
            return ToolResult(
                tool="apply_patch",
                output="Patch was rejected by the user.",
                returncode=-1,
                duration_ms=0,
                args={"path": path, "replace_all": replace_all},
                exception_info="User rejected patch.",
                approved=False,
            )
        updated = text.replace(effective_old, effective_new, -1 if replace_all else 1)
        self.workspace.atomic_write_text(file_path, updated, encoding="utf-8")
        diff = self._unified_file_diff(file_path, text, updated)
        return ToolResult(
            tool="apply_patch",
            output=self.redactor.redact_text(
                f"Patched {file_path.relative_to(self.cwd)}; replacements={count if replace_all else 1}.\n\n{diff}"
            ),
            returncode=0,
            duration_ms=0,
            args={"path": path, "replace_all": replace_all},
            before_content=text,
            after_content=updated,
            file_path=str(file_path.relative_to(self.cwd)),
            file_existed_before=True,
            before_hash=_content_hash(text),
            after_hash=_content_hash(updated),
        )

    def replace_lines(self, path: str, start_line: int, end_line: int, new_text: str) -> ToolResult:
        file_path = self.workspace.resolve(path)
        try:
            before = self._read_editable_text(file_path)
        except (FileNotFoundError, IsADirectoryError):
            raise FileNotFoundError(f"file does not exist: {path}")
        lines = before.splitlines(keepends=True)
        if start_line < 1 or end_line < start_line or end_line > len(lines):
            raise ValueError(f"invalid line range: {start_line}-{end_line}")
        if self.approval_mode == "confirm" and not self._confirm("replace_lines", f"{path}:{start_line}-{end_line}"):
            return ToolResult(
                tool="replace_lines",
                output="Line replacement was rejected by the user.",
                returncode=-1,
                duration_ms=0,
                args={"path": path, "start_line": start_line, "end_line": end_line},
                exception_info="User rejected line replacement.",
                approved=False,
            )
        newline = _preferred_newline(before)
        replacement = _normalize_newlines(new_text, newline)
        if replacement and not replacement.endswith(("\n", "\r")):
            replacement += newline
        updated = "".join(lines[: start_line - 1] + [replacement] + lines[end_line:])
        self.workspace.atomic_write_text(file_path, updated, encoding="utf-8")
        diff = self._unified_file_diff(file_path, before, updated)
        return ToolResult(
            tool="replace_lines",
            output=self.redactor.redact_text(
                f"Replaced {file_path.relative_to(self.cwd)}:{start_line}-{end_line}.\n\n{diff}"
            ),
            returncode=0,
            duration_ms=0,
            args={"path": path, "start_line": start_line, "end_line": end_line},
            before_content=before,
            after_content=updated,
            file_path=str(file_path.relative_to(self.cwd)),
            file_existed_before=True,
            before_hash=_content_hash(before),
            after_hash=_content_hash(updated),
        )

    def git_diff(self, path: str = "") -> ToolResult:
        started = time.time()
        if not self._git_executable:
            return ToolResult(
                tool="git_diff",
                output="A trusted git executable is not available.",
                returncode=0,
                duration_ms=0,
                args={"path": path},
            )
        git = [self._git_executable, "-c", "core.pager=cat", "-c", "pager.diff=false", "-c", "core.fsmonitor=false"]
        if self._run_argv([*git, "rev-parse", "--is-inside-work-tree"], sandbox=False).returncode != 0:
            return ToolResult(
                tool="git_diff",
                output="No git repository found for this workspace, so git diff is unavailable.",
                returncode=0,
                duration_ms=0,
                args={"path": path},
            )
        pathspec: list[str] = []
        if path:
            pathspec = [str(self.workspace.resolve(path).relative_to(self.cwd))]
        commands = {
            "status": [*git, "status", "--short", "--untracked-files=all", "--", *pathspec],
            "unstaged": [
                *git,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--",
                *pathspec,
            ],
            "staged": [
                *git,
                "diff",
                "--cached",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--",
                *pathspec,
            ],
            "untracked": [*git, "ls-files", "--others", "--exclude-standard", "-z", "--", *pathspec],
        }
        completed = {name: self._run_argv(argv, sandbox=False) for name, argv in commands.items()}
        failed = next((result for result in completed.values() if result.returncode != 0), None)
        if failed is not None:
            return ToolResult(
                tool="git_diff",
                command="; ".join(shlex_join(argv) for argv in commands.values()),
                output=self.redactor.redact_text(failed.stdout),
                returncode=failed.returncode,
                duration_ms=int((time.time() - started) * 1000),
                args={"path": path},
            )

        sections = [
            "## status\n" + (completed["status"].stdout.rstrip() or "(clean)"),
            "## unstaged diff\n" + (completed["unstaged"].stdout.rstrip() or "(none)"),
            "## staged diff\n" + (completed["staged"].stdout.rstrip() or "(none)"),
        ]
        untracked_diffs: list[str] = []
        for raw_name in completed["untracked"].stdout.split("\0"):
            if not raw_name:
                continue
            try:
                candidate = self.workspace.resolve(raw_name)
                if self.workspace.is_probably_text_file(candidate):
                    content = self.workspace.read_text(candidate, encoding="utf-8", errors="replace", max_chars=50000)
                    untracked_diffs.append(self._unified_file_diff(candidate, "", content))
                else:
                    untracked_diffs.append(f"untracked binary: {raw_name}")
            except (OSError, ValueError):
                untracked_diffs.append(f"untracked unreadable: {raw_name}")
            if len(untracked_diffs) >= 50:
                untracked_diffs.append("... additional untracked files omitted")
                break
        sections.append("## untracked files\n" + ("\n\n".join(untracked_diffs) or "(none)"))
        output = truncate_text(self.redactor.redact_text("\n\n".join(sections)), DEFAULT_OUTPUT_LIMIT)
        return ToolResult(
            tool="git_diff",
            command="; ".join(shlex_join(argv) for argv in commands.values()),
            output=output,
            returncode=0,
            duration_ms=int((time.time() - started) * 1000),
            args={"path": path},
        )

    def run_tests(
        self,
        command: str | None = None,
        *,
        ignore_paths: set[Path] | None = None,
    ) -> ToolResult:
        if not self.verification_checks:
            return ToolResult(
                tool="run_tests",
                command="",
                output=(
                    "No authoritative verification check is configured. Restart "
                    "with --test-command '<command>' or --check NAME '<command>'."
                ),
                returncode=-1,
                duration_ms=0,
                exception_info="TestCommandRequired",
                blocked=True,
            )
        if command is not None:
            if not isinstance(command, str) or not command.strip():
                return ToolResult(
                    tool="run_tests",
                    command="",
                    output="The configured test command must not be blank.",
                    returncode=-1,
                    duration_ms=0,
                    exception_info="InvalidTestCommand",
                    blocked=True,
                )
            command = command.strip()
            legacy_match = (
                not self._strict_verification_matrix
                and self.default_test_command is not None
                and command == self.default_test_command
            )
            if not legacy_match:
                return ToolResult(
                    tool="run_tests",
                    command=self.redactor.redact_text(command),
                    output=(
                        "Custom verification commands are disabled. Call run_tests "
                        "without arguments and configure commands before startup."
                    ),
                    returncode=-1,
                    duration_ms=0,
                    exception_info="CustomTestCommandDisabled",
                    blocked=True,
                )
        if not self._strict_verification_matrix:
            return self._run_legacy_test(
                self.default_test_command or "",
                ignore_paths=ignore_paths or set(),
            )
        return self._run_test_matrix(ignore_paths=ignore_paths or set())

    def _run_legacy_test(
        self, command: str, *, ignore_paths: set[Path]
    ) -> ToolResult:
        blocked_reason = self._blocked_command_reason(command)
        if blocked_reason:
            return ToolResult(
                tool="run_tests",
                command=self.redactor.redact_text(command),
                output="Test command was blocked by the safety policy.",
                returncode=-1,
                duration_ms=0,
                args={"command": self.redactor.redact_text(command)},
                exception_info=blocked_reason,
                blocked=True,
            )
        if self.approval_mode == "confirm" and not self._confirm("run_tests", command):
            return ToolResult(
                tool="run_tests",
                command=self.redactor.redact_text(command),
                output="Test command was rejected by the user.",
                returncode=-1,
                duration_ms=0,
                args={"command": self.redactor.redact_text(command)},
                exception_info="User rejected test command.",
                approved=False,
            )
        try:
            baseline = self.workspace_fingerprint(
                ignore_paths=ignore_paths
            ).fingerprint
        except Exception:
            return ToolResult(
                tool="run_tests",
                command=self.redactor.redact_text(command),
                output=(
                    "Workspace fingerprint capture failed before "
                    "verification."
                ),
                returncode=-1,
                duration_ms=0,
                args={"command": self.redactor.redact_text(command)},
                exception_info="WorkspaceFingerprintError",
            )
        result = self._apply_test_count(
            self._run_command(
                "run_tests", command, args={"command": command}
            )
        )
        try:
            after = self.workspace_fingerprint(
                ignore_paths=ignore_paths
            ).fingerprint
        except Exception:
            result.output = truncate_text(
                f"{result.output}\n\n"
                "Workspace fingerprint capture failed after verification.",
                DEFAULT_OUTPUT_LIMIT,
            )
            result.returncode = -1
            result.exception_info = "WorkspaceFingerprintError"
            return result
        result.verification_boundary_checked = True
        if after != baseline:
            result.output = truncate_text(
                f"{result.output}\n\n"
                "Test command changed the fingerprinted workspace; "
                "submission evidence was not minted.",
                DEFAULT_OUTPUT_LIMIT,
            )
            result.returncode = -1
            result.exception_info = (
                "WorkspaceChangedDuringVerification"
            )
            return result
        if result.returncode == 0:
            result.verification_fingerprint = baseline
        return result

    def _apply_test_count(self, result: ToolResult) -> ToolResult:
        unittest_match = UNITTEST_COUNT.search(result.output)
        if unittest_match:
            result.tests_run = int(unittest_match.group(1))
        elif PYTEST_ZERO.search(result.output):
            result.tests_run = 0
        if result.tests_run == 0 and result.returncode in {0, 5}:
            if self.allow_zero_tests:
                result.returncode = 0
            else:
                result.returncode = 1
                result.exception_info = "NoTestsCollected"
        return result

    def _run_test_matrix(self, *, ignore_paths: set[Path]) -> ToolResult:
        for check in self.verification_checks:
            blocked_reason = self._blocked_command_reason(check.command)
            if blocked_reason:
                evidence = VerificationCheckEvidence(
                    name=check.name,
                    returncode=-1,
                    duration_ms=0,
                    exception_info=blocked_reason,
                    blocked=True,
                )
                return ToolResult(
                    tool="run_tests",
                    command="<verification matrix>",
                    output=f"Verification check {check.name} was blocked by policy.",
                    returncode=-1,
                    duration_ms=0,
                    exception_info=blocked_reason,
                    blocked=True,
                    verification_checks=(evidence,),
                )

        detail = "\n".join(
            f"{check.name}: {self.redactor.redact_text(check.command)}"
            for check in self.verification_checks
        )
        if self.approval_mode == "confirm" and not self._confirm(
            "run_tests", detail
        ):
            return ToolResult(
                tool="run_tests",
                command="<verification matrix>",
                output="Verification matrix was rejected by the user.",
                returncode=-1,
                duration_ms=0,
                exception_info="User rejected verification matrix.",
                approved=False,
            )

        def execute(check: VerificationCheck) -> VerificationCheckExecution:
            result = self._apply_test_count(
                self._run_command("run_tests", check.command, args={})
            )
            return VerificationCheckExecution(
                evidence=VerificationCheckEvidence(
                    name=check.name,
                    returncode=result.returncode,
                    duration_ms=result.duration_ms,
                    tests_run=result.tests_run,
                    exception_info=(
                        result.exception_info.partition(":")[0]
                        if result.exception_info
                        else ""
                    ),
                    blocked=result.blocked,
                    approved=result.approved,
                ),
                output=result.output,
            )

        matrix = run_verification_matrix(
            self.verification_checks,
            capture_fingerprint=lambda: self.workspace_fingerprint(
                ignore_paths=ignore_paths
            ).fingerprint,
            execute_check=execute,
        )
        return ToolResult(
            tool="run_tests",
            command="<verification matrix>",
            output=self.redactor.redact_text(matrix.output),
            returncode=matrix.returncode,
            duration_ms=sum(
                item.duration_ms for item in matrix.verification_checks
            ),
            exception_info=matrix.exception_info,
            approved=matrix.approved,
            blocked=matrix.blocked,
            verification_checks=matrix.verification_checks,
            verification_boundary_checked=matrix.returncode == 0,
            verification_fingerprint=matrix.verification_fingerprint,
        )

    def submit(self, summary: str = "") -> ToolResult:
        submission = summary.strip()
        return ToolResult(
            tool="submit",
            output=submission or "Submitted.",
            returncode=0,
            duration_ms=0,
            args={"summary": self.redactor.redact_text(summary)},
            submitted=True,
            submission=submission,
        )

    def _run_command(
        self,
        tool: str,
        command: str,
        *,
        args: dict[str, Any],
    ) -> ToolResult:
        started = time.time()
        try:
            result = self._run(command)
            tool_result = ToolResult(
                tool=tool,
                command=self.redactor.redact_text(command),
                output=self.redactor.redact_text(result.stdout),
                returncode=result.returncode,
                duration_ms=int((time.time() - started) * 1000),
                args=self.redactor.redact_data(args),
            )
        except Exception as exc:
            raw_output = getattr(exc, "output", "") or ""
            if isinstance(raw_output, bytes):
                raw_output = raw_output.decode("utf-8", errors="replace")
            tool_result = ToolResult(
                tool=tool,
                command=self.redactor.redact_text(command),
                output=self.redactor.redact_text(raw_output),
                returncode=-1,
                duration_ms=int((time.time() - started) * 1000),
                args=self.redactor.redact_data(args),
                exception_info=self.redactor.redact_text(f"{type(exc).__name__}: {exc}"),
            )
        return tool_result

    def _confirm(self, tool: str, detail: str) -> bool:
        if not sys.stdin.isatty():
            return False
        print(f"\nAgent wants to use {tool}:\n")
        print(detail)
        answer = input("\nAllow this tool call? [y/N] ").strip().lower()
        return answer in {"y", "yes"}

    def _run(self, command: str) -> subprocess.CompletedProcess[str]:
        return self._run_argv(["/bin/sh", "-c", command])

    def _run_argv(
        self,
        argv: list[str],
        *,
        sandbox: bool = True,
        timeout_seconds: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if not argv or any(not isinstance(item, str) for item in argv):
            raise ValueError("argv must be a non-empty list of strings")
        wrapped_argv = self._sandboxed_argv(argv) if sandbox else list(argv)
        docker_run = self._docker_run_metadata(wrapped_argv) if sandbox else None
        timeout = timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        # A child may print without bound. Spooling to a private temporary file
        # prevents command output from exhausting the agent process's memory.
        with tempfile.TemporaryFile(mode="w+b", dir=self._runtime_root / "tmp") as output:
            process: subprocess.Popen | None = None
            timed_out = False
            deferred_signal: _TerminationSignal | None = None
            try:
                process = subprocess.Popen(
                    wrapped_argv,
                    shell=False,
                    cwd=self.cwd,
                    env=self._subprocess_env(),
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name == "posix",
                    preexec_fn=self._resource_limiter() if os.name == "posix" else None,
                )
                try:
                    with _defer_sigterm_until_cleanup():
                        process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                except _TerminationSignal as exc:
                    deferred_signal = exc
            finally:
                # This runs for timeout, KeyboardInterrupt, SIGTERM translated
                # above, output handling errors, and every other exception path.
                # It also removes background descendants left by a command that
                # returned normally.
                if process is not None:
                    self._terminate_process_group(process)
                if docker_run is not None:
                    self._cleanup_docker_run(docker_run)

            if deferred_signal is not None:
                deferred_signal.propagate()
            stdout = _read_capped_output(output, MAX_COMMAND_OUTPUT_BYTES)
            if timed_out:
                raise subprocess.TimeoutExpired(
                    shlex_join(argv), timeout, output=stdout
                )
            if process is None:
                raise RuntimeError("command process was not started")
            if (
                docker_run is not None
                and process.returncode in DOCKER_WRAPPER_FAILURE_CODES
            ):
                raise SandboxExecutionError(
                    "docker", process.returncode, stdout
                )
        return subprocess.CompletedProcess(argv, process.returncode, stdout)

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen) -> None:
        """Best-effort, bounded cleanup for the command and all its descendants."""

        if os.name != "posix":
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
                    except BaseException:
                        process.kill()
                        process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
            except BaseException:
                # Cleanup must not hide the original command result or exception.
                pass
            return

        process_group = process.pid

        def group_exists() -> bool:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            except OSError:
                return False
            return True

        try:
            if group_exists():
                os.killpg(process_group, signal.SIGTERM)
                deadline = time.monotonic() + PROCESS_TERMINATION_GRACE_SECONDS
                while group_exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
            if group_exists():
                os.killpg(process_group, signal.SIGKILL)
        except (OSError, ValueError):
            try:
                if process.poll() is None:
                    process.kill()
            except BaseException:
                pass
        finally:
            try:
                if process.poll() is None:
                    process.wait(timeout=PROCESS_TERMINATION_GRACE_SECONDS)
            except BaseException:
                pass

    @staticmethod
    def _docker_run_metadata(argv: list[str]) -> _DockerRunMetadata | None:
        try:
            name_index = argv.index("--name")
            cidfile_index = argv.index("--cidfile")
            name = argv[name_index + 1]
            cidfile = Path(argv[cidfile_index + 1])
        except (ValueError, IndexError):
            return None
        if len(argv) < 2 or argv[1] != "run" or not name:
            return None
        return _DockerRunMetadata(
            executable=argv[0], name=name, cidfile=cidfile
        )

    def _cleanup_docker_run(self, run: _DockerRunMetadata) -> None:
        """Remove a Docker container even if the attached client was interrupted."""

        identifiers: list[str] = []
        try:
            raw_cid = run.cidfile.read_text(encoding="ascii").strip().lower()
        except (OSError, UnicodeError):
            raw_cid = ""
        if raw_cid and len(raw_cid) <= 64 and all(
            character in "0123456789abcdef" for character in raw_cid
        ):
            identifiers.append(raw_cid)
        identifiers.append(run.name)

        try:
            for identifier in dict.fromkeys(identifiers):
                try:
                    completed = subprocess.run(
                        [run.executable, "rm", "-f", identifier],
                        shell=False,
                        cwd=self.cwd,
                        env=self._subprocess_env(),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=DOCKER_CLEANUP_TIMEOUT_SECONDS,
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError):
                    continue
                if completed.returncode == 0:
                    break
        finally:
            try:
                run.cidfile.unlink(missing_ok=True)
            except OSError:
                pass

    def _sandboxed_command(self, command: str) -> str:
        # Backward-compatible debug helper; execution itself always uses argv.
        return shlex_join(self._sandboxed_argv(["/bin/sh", "-lc", command]))

    def _sandboxed_argv(self, argv: list[str]) -> list[str]:
        mode = self._selected_sandbox_mode()
        if mode == "none":
            return list(argv)
        if mode == "sandbox-exec":
            executable = self._trusted_executable("sandbox-exec")
            if not executable:
                raise RuntimeError("sandbox-exec is not available")
            return [executable, "-p", self._sandbox_profile(), *argv]
        if mode == "bwrap":
            executable = self._trusted_executable("bwrap")
            if not executable:
                raise RuntimeError("bwrap is not available")
            prefix = [
                executable,
                "--die-with-parent",
                "--new-session",
                "--unshare-all",
                "--ro-bind",
                "/",
                "/",
                "--tmpfs",
                "/run",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--tmpfs",
                "/tmp",
            ]
            home = Path.home().resolve()
            try:
                self.cwd.relative_to(home)
            except ValueError:
                # Hide user credentials while retaining a read-only system root.
                prefix.extend(["--tmpfs", str(home)])
            else:
                prefix.extend(["--tmpfs", str(home)])
                relative_parent = self.cwd.relative_to(home).parent
                cursor = home
                for part in relative_parent.parts:
                    cursor /= part
                    prefix.extend(["--dir", str(cursor)])
            prefix.extend([
                "--bind",
                str(self.cwd),
                str(self.cwd),
                "--bind",
                str(self._runtime_root),
                str(self._runtime_root),
                "--chdir",
                str(self.cwd),
                "--",
            ])
            return [*prefix, *argv]
        if mode == "docker":
            executable = self._trusted_executable("docker")
            if not executable:
                raise RuntimeError("docker is not available")
            container_name = f"mca-{os.getpid()}-{secrets.token_hex(8)}"
            cidfile = self._runtime_root / "tmp" / f"{container_name}.cid"
            return [
                executable,
                "run",
                "--rm",
                "--name",
                container_name,
                "--cidfile",
                str(cidfile),
                "--pull=never",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "128",
                "--memory",
                "2g",
                "--cpus",
                "2",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=256m",
                *(
                    [
                        "--user",
                        f"{os.getuid()}:{os.getgid()}",
                        "--env",
                        "HOME=/tmp",
                        "--env",
                        "TMPDIR=/tmp",
                        "--env",
                        "PYTHONDONTWRITEBYTECODE=1",
                        "--env",
                        "GIT_CONFIG_GLOBAL=/dev/null",
                        "--env",
                        "GIT_CONFIG_NOSYSTEM=1",
                        "--env",
                        "GIT_TERMINAL_PROMPT=0",
                    ]
                    if os.name == "posix"
                    else []
                ),
                "--mount",
                f"type=bind,src={self.cwd},dst=/workspace",
                "-w",
                "/workspace",
                self.docker_image,
                *argv,
            ]
        raise RuntimeError(f"unsupported sandbox mode: {mode}")

    def _sandbox_profile(self) -> str:
        def literal(path: Path) -> str:
            return str(path).replace("\\", "\\\\").replace('"', '\\"')

        home = literal(Path.home().resolve())
        cwd = literal(self.cwd)
        runtime = literal(self._runtime_root.resolve())
        # Default read access is retained for macOS frameworks and package
        # managers, but the real home directory is hidden except for the target
        # workspace.  HOME itself points at the isolated runtime directory.
        return (
            '(version 1) '
            '(allow default) '
            '(deny network*) '
            f'(deny file-read* (subpath "{home}")) '
            f'(allow file-read* (subpath "{cwd}")) '
            f'(allow file-read* (subpath "{runtime}")) '
            '(deny file-write*) '
            f'(allow file-write* (subpath "{cwd}")) '
            f'(allow file-write* (subpath "{runtime}"))'
        )

    def _selected_sandbox_mode(self) -> str:
        if self.sandbox_mode != "auto":
            return self.sandbox_mode
        if self._resolved_sandbox_mode:
            return self._resolved_sandbox_mode
        if self._sandbox_probe_error:
            raise RuntimeError(self._sandbox_probe_error)
        if self._trusted_executable("sandbox-exec"):
            return "sandbox-exec"
        if self._trusted_executable("bwrap"):
            return "bwrap"
        if self._trusted_executable("docker"):
            return "docker"
        raise RuntimeError(
            "no sandbox backend is available; explicitly choose sandbox_mode='none' "
            "only for a disposable, trusted workspace"
        )

    def _subprocess_env(self) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in SAFE_INHERITED_ENV and not _is_secret_env_name(key)
        }
        for key, value in self.env.items():
            if not _is_secret_env_name(key):
                environment[str(key)] = str(value)
        environment.update({
            "HOME": str(self._runtime_root / "home"),
            "TMPDIR": str(self._runtime_root / "tmp"),
            # The isolated cache starts empty and bytecode writes are disabled,
            # so rapid same-size edits cannot reuse a stale timestamp-based pyc.
            "PYTHONPYCACHEPREFIX": str(self._runtime_root / "pycache"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        })
        environment.setdefault("PATH", os.defpath)
        environment.setdefault("LANG", "C.UTF-8")
        return environment

    def _resource_limiter(self):
        timeout = max(1, int(self.timeout_seconds))

        def limit() -> None:
            try:
                import resource

                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                for resource_name, soft_limit in [
                    (resource.RLIMIT_NOFILE, 256),
                    (resource.RLIMIT_FSIZE, 256 * 1024 * 1024),
                    (resource.RLIMIT_CPU, timeout + 2),
                ]:
                    current_soft, current_hard = resource.getrlimit(resource_name)
                    target = soft_limit if current_hard < 0 else min(soft_limit, current_hard)
                    resource.setrlimit(resource_name, (target, current_hard))
            except (ImportError, OSError, ValueError):
                pass

        return limit

    def sandbox_probe(self) -> tuple[bool, str]:
        """Select a backend by actually starting it; auto tries every local option."""

        if self.sandbox_mode == "none":
            return True, "disabled"

        if self.sandbox_mode == "auto":
            # A later retry may succeed after Docker Desktop or another backend
            # becomes available in the same process.
            self._sandbox_probe_error = ""
            candidates = [
                mode
                for mode, executable in [
                    ("sandbox-exec", "sandbox-exec"),
                    ("bwrap", "bwrap"),
                    ("docker", "docker"),
                ]
                if self._trusted_executable(executable)
            ]
            if not candidates:
                self._sandbox_probe_error = (
                    "no sandbox backend is available; explicitly choose "
                    "sandbox_mode='none' only for a disposable, trusted workspace"
                )
                return False, "unavailable"
        else:
            status = self.sandbox_status()
            if status.endswith("-unavailable"):
                return False, status
            candidates = [self.sandbox_mode]

        failures: list[str] = []
        for candidate in candidates:
            self._resolved_sandbox_mode = candidate
            try:
                probe = self._run_argv(
                    ["/bin/sh", "-c", ":"],
                    timeout_seconds=min(self.timeout_seconds, 5),
                )
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                failures.append(f"{candidate}: {type(exc).__name__}: {exc}")
                continue
            if probe.returncode == 0:
                self._sandbox_probe_error = ""
                return True, candidate
            detail = truncate_text((probe.stdout or "").strip(), 500)
            failures.append(
                f"{candidate}: probe failed ({probe.returncode})"
                f"{': ' + detail if detail else ''}"
            )

        self._resolved_sandbox_mode = None
        if self.sandbox_mode == "auto":
            detail = "auto: no usable backend (" + "; ".join(failures) + ")"
            self._sandbox_probe_error = detail
            return False, detail
        return False, failures[-1]

    def sandbox_status(self) -> str:
        if self.sandbox_mode == "none":
            return "disabled"
        if self.sandbox_mode == "sandbox-exec":
            return "sandbox-exec" if self._trusted_executable("sandbox-exec") else "sandbox-exec-unavailable"
        if self.sandbox_mode == "bwrap":
            return "bwrap" if self._trusted_executable("bwrap") else "bwrap-unavailable"
        if self.sandbox_mode == "docker":
            return "docker" if self._trusted_executable("docker") else "docker-unavailable"
        if self._resolved_sandbox_mode:
            return self._resolved_sandbox_mode
        if self._sandbox_probe_error:
            return "unavailable"
        if self._trusted_executable("sandbox-exec"):
            return "sandbox-exec"
        if self._trusted_executable("bwrap"):
            return "bwrap"
        if self._trusted_executable("docker"):
            return "docker"
        return "unavailable"

    def _unified_file_diff(self, file_path: Path, before: str, after: str) -> str:
        rel_path = str(file_path.relative_to(self.cwd))
        return "".join(
            unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
            )
        ).strip() or "(no textual diff)"

    def _trusted_executable(self, name: str) -> str:
        candidate = shutil.which(name)
        if not candidate:
            return ""
        resolved = Path(candidate).resolve()
        try:
            resolved.relative_to(self.cwd)
        except ValueError:
            pass
        else:
            return ""
        return str(resolved) if resolved.is_file() else ""

    def _read_editable_text(self, file_path: Path) -> str:
        text = self.workspace.read_text(
            file_path,
            encoding="utf-8",
            max_chars=MAX_STRUCTURED_EDIT_CHARS + 1,
        )
        if len(text) > MAX_STRUCTURED_EDIT_CHARS:
            raise ValueError("structured edits are limited to 8 MiB text files")
        return text

    @staticmethod
    def _blocked_command_reason(command: str) -> str:
        for pattern in DANGEROUS_COMMAND_PATTERNS:
            if pattern.search(command):
                return f"Blocked dangerous command pattern: {pattern.pattern}"
        return ""

def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _preferred_newline(content: str) -> str:
    """Return the existing file's dominant newline, defaulting to LF."""
    crlf = content.count("\r\n")
    lf = content.count("\n") - crlf
    cr = content.count("\r") - crlf
    if crlf >= lf and crlf >= cr and crlf:
        return "\r\n"
    if cr > lf and cr:
        return "\r"
    return "\n"


def _normalize_newlines(content: str, newline: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return normalized if newline == "\n" else normalized.replace("\n", newline)


def _is_secret_env_name(name: str) -> bool:
    return SecretRedactor.is_secret_env_name(name)


def _strict_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be a JSON boolean")


def _read_capped_output(handle, limit: int) -> str:
    """Decode at most *limit* bytes while retaining both useful ends."""

    handle.flush()
    size = handle.seek(0, os.SEEK_END)
    if size <= limit:
        handle.seek(0)
        data = handle.read()
    else:
        head_size = limit // 2
        tail_size = limit - head_size
        handle.seek(0)
        head = handle.read(head_size)
        handle.seek(-tail_size, os.SEEK_END)
        tail = handle.read(tail_size)
        marker = f"\n\n...[elided {size - limit} output bytes]...\n\n".encode()
        data = head + marker + tail
    return data.decode("utf-8", errors="replace")
