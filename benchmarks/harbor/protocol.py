from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SUPPORTED_PROVIDERS = frozenset({"deepseek", "openai"})


def split_harbor_model(model_name: str) -> tuple[str, str]:
    if not isinstance(model_name, str) or "/" not in model_name:
        raise ValueError("model must use Harbor's provider/model format")
    provider, model = model_name.split("/", 1)
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"unsupported MCA provider {provider!r}; expected one of "
            f"{', '.join(sorted(SUPPORTED_PROVIDERS))}"
        )
    if not model.strip():
        raise ValueError("model name must not be empty")
    return provider, model


@dataclass(frozen=True)
class HarborRunConfig:
    max_steps: int = 50
    context_chars: int = 60_000
    command_timeout: int = 120
    request_timeout: int = 180
    max_retries: int = 0
    allow_shell: bool = False
    check_name: str = "patch-integrity"
    check_command: str = "git diff --check"
    state_dir: str = "/logs/agent/state"
    log_path: str = "/logs/agent/mca.txt"

    def __post_init__(self) -> None:
        for name in (
            "max_steps",
            "context_chars",
            "command_timeout",
            "request_timeout",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.max_retries, bool)
            or not isinstance(self.max_retries, int)
            or self.max_retries < 0
        ):
            raise ValueError("max_retries must be a non-negative integer")
        if not self.check_name.strip() or not self.check_command.strip():
            raise ValueError("benchmark check name and command must not be blank")
        for name in ("state_dir", "log_path"):
            value = getattr(self, name)
            if not value.startswith("/logs/agent/"):
                raise ValueError(f"{name} must stay under /logs/agent")


def build_transaction_command(
    instruction: str,
    harbor_model: str,
    *,
    base_url: str | None = None,
    config: HarborRunConfig | None = None,
) -> str:
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("instruction must not be blank")
    provider, model = split_harbor_model(harbor_model)
    config = config or HarborRunConfig()
    argv = [
        "mca",
        "tx",
        "run",
        instruction,
        "--cwd",
        ".",
        "--model",
        model,
        "--provider",
        provider,
        "--memory",
        "off",
        "--max-steps",
        str(config.max_steps),
        "--context-chars",
        str(config.context_chars),
        "--timeout",
        str(config.command_timeout),
        "--request-timeout",
        str(config.request_timeout),
        "--max-retries",
        str(config.max_retries),
        "--check",
        config.check_name,
        config.check_command,
        "--sandbox",
        "none",
        "--yes",
        "--quiet",
    ]
    if base_url:
        argv.extend(("--base-url", base_url))
    if config.allow_shell:
        argv.append("--allow-shell")
    run_command = shlex.join(argv)
    state_dir = shlex.quote(config.state_dir)
    log_path = shlex.quote(config.log_path)
    return (
        "set -euo pipefail\n"
        f"export MCA_STATE_DIR={state_dir}\n"
        f"{run_command} 2>&1 | tee {log_path}\n"
        "transaction_id=$(sed -n 's/^transaction: //p' "
        f"{log_path} | tail -n 1)\n"
        'test -n "$transaction_id"\n'
        f'mca tx commit "$transaction_id" 2>&1 | tee -a {log_path}\n'
    )


def usage_from_trajectory(trajectory: dict[str, Any]) -> dict[str, int]:
    usage = trajectory.get("model_usage") or {}
    keys = (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
        "model_calls",
    )
    normalized: dict[str, int] = {}
    for key in keys:
        value = usage.get(key, 0)
        normalized[key] = (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            else 0
        )
    return normalized


def load_latest_run(log_root: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    transaction_root = log_root / "state" / "transactions"
    trajectories = sorted(
        transaction_root.glob("*/trajectory.json"),
        key=lambda path: path.stat().st_mtime_ns,
    )
    if not trajectories:
        raise FileNotFoundError("MCA transaction trajectory was not collected")
    trajectory_path = trajectories[-1]
    trajectory = json.loads(trajectory_path.read_text(encoding="utf-8"))
    manifest_path = trajectory_path.with_name("manifest.json")
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file()
        else None
    )
    return trajectory, manifest
