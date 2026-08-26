"""Build or execute the two arms of the fixed-model Harbor pilot."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from benchmarks.harbor.protocol import split_harbor_model

HERE = Path(__file__).resolve().parent
DEFAULT_PROTOCOL = HERE / "protocol.json"
_EXACT_PACKAGE = re.compile(r"^[A-Za-z0-9_.-]+==[^=<>!~\s]+$")
_VCS_COMMIT = re.compile(r"^git\+[^\s]+@[0-9a-fA-F]{40}(?:[#?].*)?$")
_HASHED_WHEEL_URL = re.compile(r"^https://[^\s]+\.whl#sha256=[0-9a-fA-F]{64}$")


def load_protocol(path: Path = DEFAULT_PROTOCOL) -> dict[str, Any]:
    protocol = json.loads(path.read_text(encoding="utf-8"))
    if protocol.get("schema_version") != 1:
        raise ValueError("unsupported Harbor comparison protocol")
    return protocol


def load_task_names(protocol: dict[str, Any], protocol_path: Path) -> list[str]:
    dataset = protocol["dataset"]
    task_path = protocol_path.parent / dataset["subset_file"]
    payload = json.loads(task_path.read_text(encoding="utf-8"))
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("pilot task file must contain a non-empty task list")
    names: list[str] = []
    for task in tasks:
        if not isinstance(task, str) or "/" not in task:
            raise ValueError("pilot tasks must use org/task references")
        org, name = task.split("/", 1)
        if org != "swe-bench" or not name:
            raise ValueError(f"unexpected pilot task reference: {task!r}")
        names.append(name)
    if len(names) != dataset["subset_size"] or len(names) != len(set(names)):
        raise ValueError("pilot task count or uniqueness does not match protocol")
    return names


def build_harbor_command(
    role: str,
    model: str,
    jobs_dir: Path,
    *,
    protocol_path: Path = DEFAULT_PROTOCOL,
    n_concurrent: int = 1,
    package_spec: str | None = None,
    executable: str = "harbor",
    task_names: Sequence[str] | None = None,
) -> list[str]:
    split_harbor_model(model)
    if role not in {"baseline", "candidate"}:
        raise ValueError("role must be baseline or candidate")
    if isinstance(n_concurrent, bool) or n_concurrent < 1:
        raise ValueError("n_concurrent must be positive")
    protocol = load_protocol(protocol_path)
    comparison = protocol["comparison"]
    pinned_model = comparison.get("model")
    if pinned_model and model != pinned_model:
        raise ValueError(
            f"model {model!r} does not match pinned model {pinned_model!r}"
        )
    arm = comparison[role]
    pinned_tasks = load_task_names(protocol, protocol_path)
    selected_tasks = list(task_names) if task_names is not None else pinned_tasks
    if not selected_tasks:
        raise ValueError("at least one benchmark task must be selected")
    if len(selected_tasks) != len(set(selected_tasks)):
        raise ValueError("selected benchmark tasks must be unique")
    unknown_tasks = sorted(set(selected_tasks) - set(pinned_tasks))
    if unknown_tasks:
        raise ValueError(
            "selected tasks are outside the pinned pilot: " + ", ".join(unknown_tasks)
        )
    argv = [
        executable,
        "run",
        "--dataset",
        protocol["dataset"]["ref"],
        "--agent",
        arm["agent"],
        "--model",
        model,
        "--env",
        protocol["harness"]["environment"],
        "--n-attempts",
        str(comparison["attempts_per_task"]),
        "--max-retries",
        str(comparison["max_retries"]),
        "--n-concurrent",
        str(n_concurrent),
        "--jobs-dir",
        str(jobs_dir),
        "--job-name",
        role,
    ]
    for task_name in selected_tasks:
        argv.extend(("--include-task-name", task_name))
    if role == "baseline":
        argv.extend(("--agent-kwarg", f"version={arm['version']}"))
    else:
        resolved_package = package_spec or arm["package"]
        argv.extend(("--agent-kwarg", f"package_spec={resolved_package}"))
        argv.extend(("--agent-kwarg", f"max_steps={arm['max_steps']}"))
        argv.extend(("--agent-kwarg", f"context_chars={arm['context_chars']}"))
        argv.extend(("--agent-kwarg", "max_retries=0"))
        allow_shell = str(bool(arm["allow_shell"])).lower()
        argv.extend(("--agent-kwarg", f"allow_shell={allow_shell}"))
    return argv


def build_execution_environment(
    protocol: dict[str, Any], environ: dict[str, str] | None = None
) -> dict[str, str]:
    """Resolve one credential and endpoint into both Harbor agent conventions."""

    env = dict(os.environ if environ is None else environ)
    comparison = protocol["comparison"]
    provider, _model = split_harbor_model(comparison["model"])
    base_url = comparison.get("base_url")
    if provider == "openai":
        key = env.get("OPENAI_API_KEY") or env.get("MCA_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY or MCA_API_KEY is required to execute")
        env["OPENAI_API_KEY"] = key
        env["MCA_API_KEY"] = key
        if base_url:
            env["OPENAI_BASE_URL"] = base_url
            env["MCA_BASE_URL"] = base_url
    elif provider == "deepseek":
        key = env.get("DEEPSEEK_API_KEY") or env.get("MCA_API_KEY")
        if not key:
            raise RuntimeError("DEEPSEEK_API_KEY or MCA_API_KEY is required to execute")
        env["DEEPSEEK_API_KEY"] = key
        env["MCA_API_KEY"] = key
        if base_url:
            env["DEEPSEEK_BASE_URL"] = base_url
            env["MCA_BASE_URL"] = base_url
    return env


def validate_package_spec(package_spec: str) -> str:
    """Accept only package references suitable for a reproducible paid run."""

    if any(
        pattern.fullmatch(package_spec)
        for pattern in (_EXACT_PACKAGE, _VCS_COMMIT, _HASHED_WHEEL_URL)
    ):
        return package_spec
    raise ValueError(
        "--package-spec must be an exact name==version, a VCS URL pinned to a "
        "40-character commit, or an HTTPS wheel URL with #sha256=<64 hex>"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Exact provider/model name.")
    parser.add_argument(
        "--jobs-dir", type=Path, default=Path("artifacts/harbor/fixed-model-pilot")
    )
    parser.add_argument("--n-concurrent", type=int, default=1)
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run one pinned task in both arms before the paid pilot.",
    )
    parser.add_argument(
        "--task-name",
        action="append",
        help="Pinned Harbor task name to run; repeat to select a subset.",
    )
    parser.add_argument(
        "--package-spec",
        help="Exact MCA wheel/PyPI/VCS spec; defaults to the released v0.5.0 package.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run both paid benchmark arms. Without this flag, only print commands.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.execute and not args.package_spec:
        raise RuntimeError(
            "--execute requires an explicit immutable --package-spec so the "
            "candidate cannot fail after the paid baseline has started"
        )
    if args.execute:
        validate_package_spec(args.package_spec)
    protocol = load_protocol()
    pinned_model = protocol["comparison"].get("model")
    if pinned_model and args.model != pinned_model:
        raise ValueError(
            f"model {args.model!r} does not match pinned model {pinned_model!r}"
        )
    task_names = args.task_name
    if args.smoke:
        if task_names and len(task_names) > 1:
            raise ValueError("--smoke accepts at most one --task-name")
        task_names = task_names or [load_task_names(protocol, DEFAULT_PROTOCOL)[0]]
    commands = [
        build_harbor_command(
            role,
            args.model,
            args.jobs_dir / role,
            n_concurrent=args.n_concurrent,
            package_spec=args.package_spec,
            task_names=task_names,
        )
        for role in ("baseline", "candidate")
    ]
    for command in commands:
        print(shlex.join(command))
    if not args.execute:
        print("dry-run: add --execute to launch both paid arms")
        return 0
    execution_env = build_execution_environment(protocol)
    for command in commands:
        subprocess.run(command, check=True, env=execution_env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
