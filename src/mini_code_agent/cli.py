from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from mini_code_agent.agent import MiniCodeAgent
from mini_code_agent.executor import BashExecutor
from mini_code_agent.model import create_model
from mini_code_agent.security import load_env_file
from mini_code_agent.trajectory import collect_file_diffs, load_trajectory, summarize_trajectory, undo_trajectory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mca", description="Mini LangGraph coding agent.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run the coding agent on a task.")
    run.add_argument("task", help="Task for the agent.")
    run.add_argument("--cwd", default=".", help="Project directory. Defaults to current directory.")
    run.add_argument("--model", default="mock", help="Model name. Use 'mock' for a local dry run.")
    run.add_argument("--base-url", default=None, help="OpenAI-compatible base URL.")
    run.add_argument("--env-file", type=Path, default=None, help="Load API keys from a local .env-style file.")
    run.add_argument("--max-steps", type=int, default=50, help="Maximum model calls.")
    run.add_argument("--timeout", type=int, default=30, help="Timeout per bash command in seconds.")
    run.add_argument("--test-command", default="python3 -m unittest discover -v", help="Default command for run_tests.")
    run.add_argument("--output", type=Path, default=None, help="Trajectory JSON path.")
    run.add_argument("--allow-shell", action="store_true", help="Allow arbitrary bash tool calls.")
    run.add_argument(
        "--sandbox",
        choices=["auto", "sandbox-exec", "bwrap", "docker", "none"],
        default="auto",
        help="Sandbox shell/test commands when available.",
    )
    run.add_argument("--allow-dirty", action="store_true", help="Allow running in a dirty git worktree.")
    run.add_argument("--require-clean", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--yolo", action="store_true", help="Run commands without confirmation.")
    run.add_argument("--yes", action="store_true", help="Alias for --yolo.")
    run.add_argument("--quiet", action="store_true", help="Hide step output.")

    trace = subparsers.add_parser("trace", help="Summarize a trajectory file.")
    trace.add_argument("trajectory", type=Path)
    trace.add_argument("--diff", action="store_true", help="Print file diffs captured in the trajectory.")

    undo = subparsers.add_parser("undo", help="Undo structured file edits from a trajectory.")
    undo.add_argument("trajectory", type=Path)
    undo.add_argument("--dry-run", action="store_true", help="Show undo actions without writing files.")

    init = subparsers.add_parser("init", help="Create a local env/config starter file.")
    init.add_argument("--path", type=Path, default=Path(".env.local"), help="Env file path to create.")
    return parser


def run_agent(args: argparse.Namespace) -> int:
    auto_approve = args.yolo or args.yes
    if args.env_file:
        load_env_file(args.env_file)
    if not auto_approve and not sys.stdin.isatty():
        raise RuntimeError("Confirmation mode needs an interactive terminal. Re-run with --yes for unattended use.")

    cwd = Path(args.cwd).resolve()
    if not cwd.exists():
        raise FileNotFoundError(f"cwd does not exist: {cwd}")
    if not args.allow_dirty and _git_dirty(cwd):
        raise RuntimeError(f"git worktree is dirty: {cwd}. Commit/stash changes or re-run with --allow-dirty.")

    output = args.output or Path("runs") / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.traj.json"
    agent = MiniCodeAgent(
        create_model(args.model, base_url=args.base_url),
        BashExecutor(
            cwd,
            timeout_seconds=args.timeout,
            approval_mode="yolo" if auto_approve else "confirm",
            allow_shell=args.allow_shell,
            default_test_command=args.test_command,
            sandbox_mode=args.sandbox,
        ),
        max_steps=args.max_steps,
        trajectory_path=output,
        quiet=args.quiet,
    )
    trajectory = agent.run(args.task)
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
    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "run":
            raise SystemExit(run_agent(args))
        if args.command == "trace":
            raise SystemExit(trace_command(args))
        if args.command == "undo":
            raise SystemExit(undo_command(args))
        if args.command == "init":
            raise SystemExit(init_command(args))
        parser.print_help()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


def trace_command(args: argparse.Namespace) -> int:
    data = load_trajectory(args.trajectory)
    print(summarize_trajectory(data))
    if args.diff:
        diff = collect_file_diffs(data)
        print("\nfile_diff:")
        print(diff or "(none)")
    return 0


def undo_command(args: argparse.Namespace) -> int:
    data = load_trajectory(args.trajectory)
    actions = undo_trajectory(data, dry_run=args.dry_run)
    if not actions:
        print("nothing to undo")
        return 0
    for action in actions:
        print(("would " if args.dry_run else "") + action)
    return 0


def init_command(args: argparse.Namespace) -> int:
    if args.path.exists():
        raise RuntimeError(f"file already exists: {args.path}")
    args.path.write_text(
        "DEEPSEEK_API_KEY=\n"
        "# OPENAI_API_KEY=\n"
        "# MCA_BASE_URL=http://localhost:8000/v1\n"
        "# MCA_REDACT=comma,separated,extra,secrets\n",
        encoding="utf-8",
    )
    print(f"created {args.path}")
    return 0


def _git_dirty(cwd: Path) -> bool:
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if inside.returncode != 0:
            return False
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if root.returncode != 0:
            return False
        git_root = Path(root.stdout.strip()).resolve()
        rel = cwd.relative_to(git_root)
        rel_arg = "." if str(rel) == "." else str(rel)
        if rel_arg != ".":
            tracked = subprocess.run(
                ["git", "ls-files", "--", rel_arg],
                cwd=git_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if not tracked.stdout.strip():
                return False
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", rel_arg],
            cwd=git_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
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
