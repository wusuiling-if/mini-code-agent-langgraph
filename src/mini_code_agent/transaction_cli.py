from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from mini_code_agent.utils import command_from_argv


def agent_command(args: argparse.Namespace, *, resume: bool) -> int:
    from mini_code_agent import cli
    from mini_code_agent.trajectory import collect_file_diffs, load_trajectory
    from mini_code_agent.transaction import TransactionStore
    from mini_code_agent.transaction_adapter import TransactionExecutor

    _combined_checks, explicit_checks = cli._configured_verification_checks(
        args, required=True
    )
    if args.model == "mock":
        raise RuntimeError("transactional runs require a real coding model")
    auto_approve = args.yolo or args.yes
    if not auto_approve and not sys.stdin.isatty():
        raise RuntimeError(
            "Confirmation mode needs an interactive terminal. Re-run with --yes for unattended use."
        )
    cli._load_runtime_env(args.env_file)

    store = TransactionStore(cli._state_root())
    if resume:
        manifest = store.load(args.transaction_id)
        if manifest["status"] != "open":
            raise RuntimeError(
                f"only an open transaction can resume; status={manifest['status']}"
            )
        output = store.trajectory(args.transaction_id)
        if not output.exists():
            raise RuntimeError("transaction has no trajectory checkpoint to resume")
        resume_data = load_trajectory(output)
    else:
        task = (args.task or "").strip()
        if not task:
            raise RuntimeError("transaction task must not be blank")
        manifest = store.create(Path(args.cwd or "."), task=task)
        output = store.trajectory(manifest["id"])
        resume_data = None

    executor = cli._load_bash_executor()(
        store.workspace(manifest["id"]),
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
    transactional_executor = TransactionExecutor(executor, store, manifest)
    cli._require_working_sandbox(transactional_executor)
    agent = cli._load_mini_code_agent()(
        cli._model_from_args(args),
        transactional_executor,
        max_steps=args.max_steps,
        context_char_budget=args.context_chars,
        trajectory_path=output,
        quiet=args.quiet,
    )
    trajectory = agent.run("" if resume else manifest["task"], resume_data=resume_data)
    manifest = store.prepare(manifest["id"], trajectory)
    print(f"\ntransaction: {manifest['id']}")
    print(f"status: {manifest['status']}")
    print(f"trajectory: {output.resolve()}")
    cli._print_workspace_changes(trajectory.get("workspace_changes", {}))
    diff = collect_file_diffs(trajectory)
    if diff:
        print("\nfile_diff:")
        print(diff)
    if manifest["status"] == "prepared":
        print(f"receipt: {manifest['receipt_id']}")
        print(f"next: mca tx commit {manifest['id']}")
        return 0
    print(f"failure: {manifest.get('failure') or trajectory.get('exit_status', 'unknown')}")
    return 2


def state_command(args: argparse.Namespace) -> int:
    from mini_code_agent import cli
    from mini_code_agent.transaction import TransactionStore

    store = TransactionStore(cli._state_root())
    command = args.transaction_command
    if command == "receipt":
        manifest = store.load(args.transaction_id)
        envelope = store.receipt(args.transaction_id)
        payload = envelope["payload"]
        verification = payload["verification"]
        prepared = payload["prepared"]
        print(f"receipt: {envelope['receipt_id']}")
        print(f"transaction: {manifest['id']}")
        print(f"status: {manifest['status']}")
        print(f"baseline_commit: {payload['baseline']['commit']}")
        print(f"patch_sha256: {prepared['patch_sha256']}")
        print(f"verification: {verification['status']}")
        print(f"verification_fingerprint: {verification['fingerprint']}")
        for check in verification["checks"]:
            print(
                "check: "
                f"{check['name']} returncode={check['returncode']} "
                f"duration_ms={check['duration_ms']}"
            )
        return 0
    if command == "status":
        manifest = store.load(args.transaction_id)
    elif command == "commit":
        manifest = store.commit(args.transaction_id)
    elif command == "abort":
        manifest = store.abort(args.transaction_id)
    else:
        raise RuntimeError(f"unsupported transaction command: {command}")
    print(f"transaction: {manifest['id']}")
    print(f"status: {manifest['status']}")
    print(f"source: {manifest['source']}")
    print(f"reads: {len(manifest['read_set'])}")
    print(f"writes: {len(manifest['write_set'])}")
    print(f"broad_read: {str(bool(manifest['broad_read'])).lower()}")
    print(f"broad_write: {str(bool(manifest['broad_write'])).lower()}")
    if manifest.get("failure"):
        print(f"failure: {manifest['failure']}")
    return 0


def _initialize_demo_repository(root: Path) -> None:
    from mini_code_agent import cli

    cli._write_demo_fixture(root)
    git_path = shutil.which("git")
    if not git_path:
        raise RuntimeError("git is required for the transaction demo")
    git_path = str(Path(git_path).resolve())
    try:
        Path(git_path).relative_to(root)
    except ValueError:
        pass
    else:
        raise RuntimeError("refusing to execute git from inside the demo workspace")
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
    }
    for command in (
        ("init",),
        ("config", "user.email", "demo@mini-code-agent.invalid"),
        ("config", "user.name", "mini-code-agent demo"),
        ("add", "."),
        ("commit", "-m", "transaction demo baseline"),
    ):
        result = subprocess.run(
            [git_path, *command],
            cwd=root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip() or f"git {' '.join(command)} failed"
            )


def _prepare_demo_case(store: Any, source: Path) -> tuple[dict, dict]:
    from mini_code_agent import cli
    from mini_code_agent.transaction_adapter import TransactionExecutor

    manifest = store.create(source, task="Fix the failing calculator tests")
    executor = cli._load_bash_executor()(
        store.workspace(manifest["id"]),
        approval_mode="yolo",
        sandbox_mode="none",
        default_test_command=(
            command_from_argv(
                [sys.executable, "-m", "unittest", "discover", "-v"]
            )
        ),
    )
    agent = cli._load_mini_code_agent()(
        cli._load_create_model()("mock"),
        TransactionExecutor(executor, store, manifest),
        trajectory_path=store.trajectory(manifest["id"]),
        quiet=True,
    )
    trajectory = agent.run(manifest["task"])
    prepared = store.prepare(manifest["id"], trajectory)
    if prepared["status"] != "prepared":
        raise RuntimeError(prepared.get("failure") or "transaction did not prepare")
    return prepared, store.receipt(manifest["id"])


def demo_command(_args: argparse.Namespace) -> int:
    from mini_code_agent import cli
    from mini_code_agent.transaction import TransactionError, TransactionStore

    if not cli._platform_supports_demo():
        raise RuntimeError("mca tx demo requires macOS, Linux, or WSL2")
    root = cli._create_demo_workspace()
    success_source = root / "success"
    conflict_source = root / "conflict"
    _initialize_demo_repository(success_source)
    _initialize_demo_repository(conflict_source)
    store = TransactionStore(root / "state")

    success, success_receipt = _prepare_demo_case(store, success_source)
    source_unchanged = "return a - b" in (
        success_source / "calculator.py"
    ).read_text(encoding="utf-8")
    committed = store.commit(success["id"])

    conflict, conflict_receipt = _prepare_demo_case(store, conflict_source)
    conflict_file = conflict_source / "calculator.py"
    conflict_file.write_text(
        conflict_file.read_text(encoding="utf-8") + "\n# concurrent user edit\n",
        encoding="utf-8",
    )
    try:
        store.commit(conflict["id"])
    except TransactionError as error:
        refusal = str(error)
    else:
        raise RuntimeError("transaction demo expected the concurrent edit to be refused")
    user_change_preserved = "# concurrent user edit" in conflict_file.read_text(
        encoding="utf-8"
    )
    store.abort(conflict["id"])

    print(f"transaction_demo: {root}")
    print(f"success.transaction: {success['id']}")
    print(f"success.receipt: {success_receipt['receipt_id']}")
    print(f"success.source_unchanged_before_commit: {str(source_unchanged).lower()}")
    print(f"success.commit: {committed['status']}")
    print(f"conflict.transaction: {conflict['id']}")
    print(f"conflict.receipt: {conflict_receipt['receipt_id']}")
    print("conflict.commit: refused")
    print(f"conflict.reason: {refusal}")
    print(f"conflict.user_change_preserved: {str(user_change_preserved).lower()}")
    return 0
