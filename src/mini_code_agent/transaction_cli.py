from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mini_code_agent.utils import command_from_argv, write_json


def _failure_message(manifest: dict[str, Any], trajectory: dict[str, Any]) -> str:
    """Prefer the already-redacted agent error over a generic state failure."""

    failure = str(manifest.get("failure") or "").strip()
    if failure == "agent did not submit":
        detail = str(
            trajectory.get("error") or trajectory.get("exit_status") or ""
        ).strip()
        if detail and detail != "Submitted":
            return detail
    return failure or str(trajectory.get("exit_status", "unknown"))


def _non_negative_int(value: Any) -> int:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else 0
    )


def _recovery_audit(
    previous: dict[str, Any] | None,
    trajectory: dict[str, Any],
    *,
    resumed: bool,
) -> dict[str, Any]:
    """Build a content-free audit of transaction attempts and recovery progress."""

    previous = previous if isinstance(previous, dict) else {}
    prior_audit = previous.get("recovery")
    raw_attempts = (
        prior_audit.get("attempts", []) if isinstance(prior_audit, dict) else []
    )
    attempts = [dict(item) for item in raw_attempts if isinstance(item, dict)]
    started_steps = _non_negative_int(previous.get("steps")) if resumed else 0
    finished_steps = _non_negative_int(trajectory.get("steps"))
    started_fingerprint = (
        str(previous.get("current_fingerprint") or "") if resumed else ""
    )
    finished_fingerprint = str(trajectory.get("current_fingerprint") or "")
    prior_duration_ms = (
        _non_negative_int(previous.get("duration_ms")) if resumed else 0
    )
    total_duration_ms = _non_negative_int(trajectory.get("duration_ms"))
    exit_status = str(trajectory.get("exit_status") or "unknown")
    raw_events = trajectory.get("events", [])
    events = raw_events if isinstance(raw_events, list) else []
    previous_events = previous.get("events", []) if resumed else []
    prior_event_count = len(previous_events) if isinstance(previous_events, list) else 0
    attempt_events = events[prior_event_count:]
    error_events = [
        event
        for event in attempt_events
        if isinstance(event, dict) and event.get("type") == "error"
    ]
    latest_error = error_events[-1] if error_events else {}
    failure_type = ""
    if exit_status.startswith(("Error:", "Interrupted:")):
        failure_type = exit_status.split(":", 1)[1]
    elif latest_error:
        causes = latest_error.get("cause_types")
        if isinstance(causes, list) and causes:
            failure_type = str(causes[0])
        else:
            failure_type = "Error"
    made_progress = finished_steps > started_steps or bool(
        started_fingerprint
        and finished_fingerprint
        and started_fingerprint != finished_fingerprint
    )
    attempt: dict[str, Any] = {
        "sequence": len(attempts) + 1,
        "kind": "resume" if resumed else "initial",
        "started_steps": started_steps,
        "finished_steps": finished_steps,
        "advanced_steps": max(0, finished_steps - started_steps),
        "workspace_changed": bool(
            started_fingerprint
            and finished_fingerprint
            and started_fingerprint != finished_fingerprint
        ),
        "made_progress": made_progress,
        "duration_ms": max(0, total_duration_ms - prior_duration_ms),
        "exit_status": exit_status,
        "verification_status": str(
            trajectory.get("verification_status") or "unknown"
        ),
        "resumable": bool(trajectory.get("resumable", False)),
        "failure_type": failure_type,
    }
    for key in (
        "operation",
        "model_attempt",
        "duration_ms",
        "cause_types",
        "request_id_sha256",
    ):
        if key in latest_error:
            attempt[f"failure_{key}"] = latest_error[key]
    attempts.append(attempt)
    consecutive_no_progress_failures = 0
    for item in reversed(attempts):
        if not item.get("failure_type") or item.get("made_progress"):
            break
        consecutive_no_progress_failures += 1
    last_failure_type = next(
        (
            str(item.get("failure_type"))
            for item in reversed(attempts)
            if item.get("failure_type")
        ),
        "",
    )
    return {
        "schema_version": 1,
        "attempt_count": len(attempts),
        "resume_count": sum(item.get("kind") == "resume" for item in attempts),
        "failure_count": sum(bool(item.get("failure_type")) for item in attempts),
        "progressing_resume_count": sum(
            item.get("kind") == "resume" and bool(item.get("made_progress"))
            for item in attempts
        ),
        "consecutive_no_progress_failures": consecutive_no_progress_failures,
        "last_failure_type": last_failure_type,
        "attempts": attempts,
    }


def _next_resume_max_steps(
    configured_max_steps: int, trajectory: dict[str, Any]
) -> int:
    """Ensure a generated resume command can advance past its checkpoint."""

    used_steps = max(0, int(trajectory.get("steps", 0)))
    if used_steps < configured_max_steps:
        return configured_max_steps
    return max(configured_max_steps * 2, used_steps + 10)


def _pin_model_transport(
    manifest: dict[str, Any],
    args: argparse.Namespace,
    *,
    resumed: bool,
) -> None:
    requested = {
        "streaming": bool(args.streaming),
        "reasoning_effort": args.reasoning_effort,
    }
    configured = manifest.get("model_transport")
    if resumed and configured is not None and configured != requested:
        raise RuntimeError(
            "transaction model transport cannot change during resume; use the "
            "streaming and reasoning-effort values from the original run"
        )
    manifest["model_transport"] = requested


def _resume_command(
    args: argparse.Namespace,
    transaction_id: str,
    trajectory: dict[str, Any],
) -> str:
    argv = [
        "mca",
        "tx",
        "resume",
        transaction_id,
        "--model",
        args.model,
        "--provider",
        args.provider,
        "--max-steps",
        str(_next_resume_max_steps(args.max_steps, trajectory)),
        "--context-chars",
        str(args.context_chars),
        "--timeout",
        str(args.timeout),
        "--request-timeout",
        str(args.request_timeout),
        "--max-retries",
        str(args.max_retries),
        "--sandbox",
        args.sandbox,
    ]
    for option, value in (
        ("--base-url", args.base_url),
        ("--env-file", args.env_file),
        ("--memory", args.memory),
        ("--test-command", args.test_command),
        ("--docker-image", args.docker_image),
    ):
        if value is not None:
            argv.extend((option, str(value)))
    for name, command in args.checks or ():
        argv.extend(("--check", name, command))
    for enabled, flag in (
        (args.deepseek_thinking, "--deepseek-thinking"),
        (args.streaming, "--streaming"),
        (args.allow_zero_tests, "--allow-zero-tests"),
        (args.allow_shell, "--allow-shell"),
        (args.yolo or args.yes, "--yes"),
        (args.quiet, "--quiet"),
    ):
        if enabled:
            argv.append(flag)
    if args.reasoning_effort is not None:
        argv.extend(("--reasoning-effort", args.reasoning_effort))
    return command_from_argv(argv)


def agent_command(args: argparse.Namespace, *, resume: bool) -> int:
    from mini_code_agent import cli
    from mini_code_agent.trajectory import collect_file_diffs, load_trajectory
    from mini_code_agent.transaction import TransactionStore
    from mini_code_agent.transaction_adapter import TransactionExecutor

    combined_checks, _explicit_checks = cli._configured_verification_checks(
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
    embedding_cli_requested = bool(
        getattr(args, "embedding_base_url", None)
        or getattr(args, "embedding_model", None)
    )
    if not resume and embedding_cli_requested and args.memory != "local":
        raise RuntimeError("embedding retrieval requires --memory local")
    if resume and embedding_cli_requested:
        raise RuntimeError(
            "embedding options apply only to a new tx run; resumed context is already persisted"
        )

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
        _pin_model_transport(manifest, args, resumed=True)
        store.save(manifest)
        if args.memory is not None:
            manifest["memory_mode"] = args.memory
            if args.memory == "local":
                from mini_code_agent.memory_adapters.project import (
                    GitProjectIdentityProvider,
                )

                manifest["workspace_identity_sha256"] = (
                    GitProjectIdentityProvider().identity_sha256(
                        Path(manifest["source"]), create=True
                    )
                )
            store.save(manifest)
    else:
        task = (args.task or "").strip()
        if not task:
            raise RuntimeError("transaction task must not be blank")
        manifest = store.create(
            Path(args.cwd or "."),
            task=task,
            memory_mode=args.memory or "off",
        )
        _pin_model_transport(manifest, args, resumed=False)
        store.save(manifest)
        output = store.trajectory(manifest["id"])
        resume_data = None

    executor = cli._load_bash_executor()(
        store.workspace(manifest["id"]),
        timeout_seconds=args.timeout,
        approval_mode="yolo" if auto_approve else "confirm",
        allow_shell=args.allow_shell,
        default_test_command=None,
        verification_checks=combined_checks,
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
    advisory_context = ""
    retrieved_count = 0
    memory_audit: dict[str, Any] | None = None
    if not resume and manifest.get("memory_mode") == "local":
        from mini_code_agent.memory_adapters.agent import render_memory_pack
        from mini_code_agent.memory_adapters.semantic import (
            semantic_provider_from_args,
        )
        from mini_code_agent.memory_retrieval import retrieve_workspace_context

        semantic_provider = semantic_provider_from_args(
            args,
            state_root=cli._state_root(),
        )
        pack = retrieve_workspace_context(
            cli._state_root(),
            Path(manifest["source"]),
            manifest["task"],
            scope_key=f"sha256:{manifest['workspace_identity_sha256']}",
            semantic_provider=semantic_provider,
        )
        rendered, audit = render_memory_pack(pack)
        advisory_context = rendered.text
        retrieved_count = len(rendered.selected_content_sha256)
        memory_audit = asdict(audit)
        if semantic_provider is not None:
            memory_audit["semantic_status"] = (
                f"fallback:{semantic_provider.last_error_type}"
                if semantic_provider.last_error_type
                else "enabled"
            )
    trajectory = agent.run(
        "" if resume else manifest["task"],
        resume_data=resume_data,
        advisory_context=advisory_context,
    )
    if not resume:
        trajectory["memory"] = {
            "mode": manifest.get("memory_mode", "off"),
            "retrieval": memory_audit,
        }
    else:
        previous_memory = resume_data.get("memory") if resume_data else None
        trajectory["memory"] = (
            previous_memory
            if isinstance(previous_memory, dict)
            else {
                "mode": manifest.get("memory_mode", "off"),
                "retrieval": {
                    "decision": "resume_persisted_context",
                    "reason": "legacy_checkpoint_without_retrieval_audit",
                },
            }
        )
    trajectory["recovery"] = _recovery_audit(
        resume_data,
        trajectory,
        resumed=resume,
    )
    # MiniCodeAgent checkpoints before transaction-only metadata is attached.
    # Persist the augmented trajectory for open failures as well as prepared runs.
    write_json(output, trajectory)
    manifest = store.prepare(manifest["id"], trajectory)
    print(f"\ntransaction: {manifest['id']}")
    print(f"status: {manifest['status']}")
    print(f"trajectory: {output.resolve()}")
    if manifest.get("memory_mode") == "local" and not resume:
        print(f"memory_retrieved: {retrieved_count}")
    cli._print_workspace_changes(trajectory.get("workspace_changes", {}))
    diff = collect_file_diffs(trajectory)
    if diff:
        print("\nfile_diff:")
        print(diff)
    if manifest["status"] == "prepared":
        print(f"receipt: {manifest['receipt_id']}")
        if manifest.get("memory_mode") == "local":
            print("memory: pending verified commit")
        print(f"next: mca tx commit {manifest['id']}")
        return 0
    print(f"failure: {_failure_message(manifest, trajectory)}")
    if manifest["status"] == "open" and trajectory.get("resumable", False):
        print("resumable: true")
        print(f"next: {_resume_command(args, manifest['id'], trajectory)}")
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
    if command == "commit" and manifest.get("memory_mode") == "local":
        try:
            from mini_code_agent.memory_admission import (
                form_committed_transaction_memories,
            )

            cards = form_committed_transaction_memories(
                cli._state_root(), manifest["id"]
            )
        except Exception as exc:  # noqa: BLE001 - post-commit auxiliary boundary
            # Memory is explicitly auxiliary: a post-commit indexing failure must
            # never make a successfully committed transaction look rolled back.
            print(f"memory: skipped ({type(exc).__name__})")
        else:
            print(
                "memory: " + (",".join(card.id for card in cards) if cards else "off")
            )
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
            capture_output=True,
            timeout=10,
            check=False,
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
            command_from_argv([sys.executable, "-m", "unittest", "discover", "-v"])
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
    source_unchanged = "return a - b" in (success_source / "calculator.py").read_text(
        encoding="utf-8"
    )
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
        raise RuntimeError(
            "transaction demo expected the concurrent edit to be refused"
        )
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
