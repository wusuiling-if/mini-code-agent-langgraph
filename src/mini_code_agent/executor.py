from __future__ import annotations

import os
import re
import signal
import shutil
import subprocess
import sys
import time
from difflib import unified_diff
from dataclasses import dataclass
from pathlib import Path
from shlex import quote as shlex_quote
from typing import Any, Literal

from mini_code_agent.security import SafeWorkspace, SecretRedactor, is_probably_text_file
from mini_code_agent.utils import DEFAULT_OUTPUT_LIMIT, truncate_text

SUBMIT_SENTINEL = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
DEFAULT_TEST_COMMAND = "python3 -m unittest discover -v"

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


@dataclass
class ToolResult:
    tool: str
    output: str
    returncode: int
    duration_ms: int
    command: str = ""
    args: dict[str, Any] | None = None
    before_content: str | None = None
    after_content: str | None = None
    exception_info: str = ""
    submitted: bool = False
    submission: str = ""
    approved: bool = True
    blocked: bool = False

    def to_observation(self) -> dict:
        data = {
            "returncode": self.returncode,
            "output": truncate_text(self.output, DEFAULT_OUTPUT_LIMIT),
        }
        if self.command:
            data["command"] = self.command
        if self.exception_info:
            data["exception_info"] = self.exception_info
        if self.submitted:
            data["submitted"] = True
            data["submission"] = self.submission
        if not self.approved:
            data["approved"] = False
        if self.blocked:
            data["blocked"] = True
        return data


class BashExecutor:
    def __init__(
        self,
        cwd: Path,
        *,
        timeout_seconds: int = 30,
        approval_mode: Literal["confirm", "yolo"] = "confirm",
        allow_shell: bool = False,
        default_test_command: str = DEFAULT_TEST_COMMAND,
        sandbox_mode: Literal["auto", "sandbox-exec", "bwrap", "docker", "none"] = "auto",
        env: dict[str, str] | None = None,
        redactor: SecretRedactor | None = None,
    ):
        self.workspace = SafeWorkspace(cwd)
        self.cwd = self.workspace.cwd
        self.timeout_seconds = timeout_seconds
        self.approval_mode = approval_mode
        self.allow_shell = allow_shell
        self.default_test_command = default_test_command
        self.sandbox_mode = sandbox_mode
        self.env = env or {}
        self.redactor = redactor or SecretRedactor()

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
                        replace_all=bool(args.get("replace_all", False)),
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
                    return self.run_tests(str(args.get("command") or self.default_test_command))
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
        if command.strip() == f"echo {SUBMIT_SENTINEL}":
            return self.submit("")
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
        return self._run_command("bash", command, args={"command": command}, mark_submission=True)

    def list_files(self, path: str = ".", *, max_files: int = 200) -> ToolResult:
        root = self.workspace.resolve(path)
        if not root.exists():
            raise FileNotFoundError(f"path does not exist: {path}")
        files = []
        if root.is_file():
            files = [root]
        else:
            for candidate in sorted(root.rglob("*")):
                if len(files) >= max_files:
                    break
                if self._should_skip_path(candidate):
                    continue
                if candidate.is_file():
                    files.append(candidate)
        lines = [str(file.relative_to(self.cwd)) for file in files]
        if len(files) >= max_files:
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
        root = self.workspace.resolve(path)
        regex = re.compile(pattern)
        results = []
        candidates = [root] if root.is_file() else sorted(root.rglob("*"))
        for candidate in candidates:
            if len(results) >= max_results:
                break
            if self._should_skip_path(candidate) or not candidate.is_file() or not is_probably_text_file(candidate):
                continue
            for line_number, line in enumerate(candidate.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    results.append(f"{candidate.relative_to(self.cwd)}:{line_number}: {line}")
                    if len(results) >= max_results:
                        break
        if len(results) >= max_results:
            results.append(f"... truncated at {max_results} matches")
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
        if not file_path.is_file():
            raise FileNotFoundError(f"file does not exist: {path}")
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        end = end_line or len(lines)
        selected = "\n".join(f"{number}: {line}" for number, line in enumerate(lines[start - 1 : end], start=start))
        return ToolResult(
            tool="read_file",
            output=truncate_text(self.redactor.redact_text(selected), max_chars),
            returncode=0,
            duration_ms=0,
            args={"path": path, "start_line": start_line, "end_line": end_line, "max_chars": max_chars},
        )

    def write_file(self, path: str, content: str) -> ToolResult:
        file_path = self.workspace.resolve(path)
        before = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
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
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        diff = self._unified_file_diff(file_path, before, content)
        return ToolResult(
            tool="write_file",
            output=self.redactor.redact_text(
                f"Wrote {len(content)} chars to {file_path.relative_to(self.cwd)}.\n\n{diff}"
            ),
            returncode=0,
            duration_ms=0,
            args={"path": path},
            before_content=self.redactor.redact_text(before),
            after_content=self.redactor.redact_text(content),
        )

    def apply_patch(self, path: str, old: str, new: str, *, replace_all: bool = False) -> ToolResult:
        file_path = self.workspace.resolve(path)
        if not file_path.is_file():
            raise FileNotFoundError(f"file does not exist: {path}")
        if not old:
            raise ValueError("old text must not be empty")
        text = file_path.read_text(encoding="utf-8")
        count = text.count(old)
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
        updated = text.replace(old, new, -1 if replace_all else 1)
        file_path.write_text(updated, encoding="utf-8")
        diff = self._unified_file_diff(file_path, text, updated)
        return ToolResult(
            tool="apply_patch",
            output=self.redactor.redact_text(
                f"Patched {file_path.relative_to(self.cwd)}; replacements={count if replace_all else 1}.\n\n{diff}"
            ),
            returncode=0,
            duration_ms=0,
            args={"path": path, "replace_all": replace_all},
            before_content=self.redactor.redact_text(text),
            after_content=self.redactor.redact_text(updated),
        )

    def replace_lines(self, path: str, start_line: int, end_line: int, new_text: str) -> ToolResult:
        file_path = self.workspace.resolve(path)
        if not file_path.is_file():
            raise FileNotFoundError(f"file does not exist: {path}")
        before = file_path.read_text(encoding="utf-8")
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
        replacement = new_text if new_text.endswith("\n") or not new_text else f"{new_text}\n"
        updated = "".join(lines[: start_line - 1] + [replacement] + lines[end_line:])
        file_path.write_text(updated, encoding="utf-8")
        diff = self._unified_file_diff(file_path, before, updated)
        return ToolResult(
            tool="replace_lines",
            output=self.redactor.redact_text(
                f"Replaced {file_path.relative_to(self.cwd)}:{start_line}-{end_line}.\n\n{diff}"
            ),
            returncode=0,
            duration_ms=0,
            args={"path": path, "start_line": start_line, "end_line": end_line},
            before_content=self.redactor.redact_text(before),
            after_content=self.redactor.redact_text(updated),
        )

    def git_diff(self, path: str = "") -> ToolResult:
        if self._run("git rev-parse --is-inside-work-tree").returncode != 0:
            return ToolResult(
                tool="git_diff",
                output="No git repository found for this workspace, so git diff is unavailable.",
                returncode=0,
                duration_ms=0,
                args={"path": path},
            )
        command = "git diff --"
        if path:
            command = f"git diff -- {self.workspace.resolve(path).relative_to(self.cwd)}"
        return self._run_command("git_diff", command, args={"path": path})

    def run_tests(self, command: str = DEFAULT_TEST_COMMAND) -> ToolResult:
        if command != self.default_test_command and not self.allow_shell:
            return ToolResult(
                tool="run_tests",
                command=self.redactor.redact_text(command),
                output=(
                    "Custom test commands are disabled. Use run_tests without a command, "
                    "configure --test-command, or pass --allow-shell."
                ),
                returncode=-1,
                duration_ms=0,
                args={"command": self.redactor.redact_text(command)},
                exception_info="CustomTestCommandDisabled",
                blocked=True,
            )
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
        return self._run_command("run_tests", command, args={"command": command})

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
        mark_submission: bool = False,
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
        if mark_submission:
            self._mark_submission(tool_result)
        return tool_result

    def _confirm(self, tool: str, detail: str) -> bool:
        if not sys.stdin.isatty():
            return False
        print(f"\nAgent wants to use {tool}:\n")
        print(detail)
        answer = input("\nAllow this tool call? [y/N] ").strip().lower()
        return answer in {"y", "yes"}

    def _run(self, command: str) -> subprocess.CompletedProcess[str]:
        wrapped_command = self._sandboxed_command(command)
        process = subprocess.Popen(
            wrapped_command,
            shell=True,
            text=True,
            cwd=self.cwd,
            env=os.environ | self.env,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
        try:
            stdout, _ = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            stdout, _ = process.communicate()
            raise subprocess.TimeoutExpired(command, self.timeout_seconds, output=stdout)
        return subprocess.CompletedProcess(command, process.returncode, stdout)

    def _sandboxed_command(self, command: str) -> str:
        if self.sandbox_mode == "none":
            return command
        if self.sandbox_mode == "sandbox-exec" or (self.sandbox_mode == "auto" and shutil.which("sandbox-exec")):
            if not shutil.which("sandbox-exec"):
                raise RuntimeError("sandbox-exec is not available")
            profile = (
                '(version 1) '
                '(allow default) '
                '(deny file-write*) '
                f'(allow file-write* (subpath "{self.cwd}")) '
                '(allow file-write* (subpath "/private/tmp")) '
                '(allow file-write* (subpath "/tmp"))'
            )
            return f"sandbox-exec -p {shlex_quote(profile)} /bin/sh -lc {shlex_quote(command)}"
        if self.sandbox_mode == "bwrap" or (self.sandbox_mode == "auto" and shutil.which("bwrap")):
            if not shutil.which("bwrap"):
                raise RuntimeError("bwrap is not available")
            return (
                "bwrap --dev-bind / / "
                f"--bind {shlex_quote(str(self.cwd))} {shlex_quote(str(self.cwd))} "
                f"--chdir {shlex_quote(str(self.cwd))} "
                f"/bin/sh -lc {shlex_quote(command)}"
            )
        if self.sandbox_mode == "docker" or (self.sandbox_mode == "auto" and shutil.which("docker")):
            if not shutil.which("docker"):
                raise RuntimeError("docker is not available")
            return (
                "docker run --rm --network none "
                f"-v {shlex_quote(str(self.cwd))}:/workspace "
                "-w /workspace python:3.11-slim "
                f"sh -lc {shlex_quote(command)}"
            )
        return command

    def sandbox_status(self) -> str:
        if self.sandbox_mode == "none":
            return "disabled"
        if self.sandbox_mode == "sandbox-exec":
            return "sandbox-exec" if shutil.which("sandbox-exec") else "sandbox-exec-unavailable"
        if self.sandbox_mode == "bwrap":
            return "bwrap" if shutil.which("bwrap") else "bwrap-unavailable"
        if self.sandbox_mode == "docker":
            return "docker" if shutil.which("docker") else "docker-unavailable"
        if shutil.which("sandbox-exec"):
            return "sandbox-exec"
        if shutil.which("bwrap"):
            return "bwrap"
        if shutil.which("docker"):
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

    @staticmethod
    def _should_skip_path(path: Path) -> bool:
        skip_names = {".git", "__pycache__", ".mypy_cache", ".pytest_cache", "node_modules", ".venv", "venv"}
        return any(part in skip_names for part in path.parts)

    @staticmethod
    def _blocked_command_reason(command: str) -> str:
        for pattern in DANGEROUS_COMMAND_PATTERNS:
            if pattern.search(command):
                return f"Blocked dangerous command pattern: {pattern.pattern}"
        return ""

    @staticmethod
    def _mark_submission(result: ToolResult) -> None:
        lines = result.output.lstrip().splitlines(keepends=True)
        if lines and lines[0].strip() == SUBMIT_SENTINEL and result.returncode == 0:
            result.submitted = True
            result.submission = "".join(lines[1:])
