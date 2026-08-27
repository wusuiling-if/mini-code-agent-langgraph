from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SUPPORTED_PROVIDERS = frozenset({"deepseek", "openai"})
UV_VERSION = "0.7.13"
UV_TOOL_INSTALL_ATTEMPTS = 3


def build_agent_install_command(package_spec: str) -> str:
    """Install MCA while reusing the task image's pinned uv when available."""

    package = shlex.quote(package_spec)
    load_uv = (
        'if [ -f "$HOME/.local/bin/env" ]; then . "$HOME/.local/bin/env"; '
        'else export PATH="$HOME/.local/bin:$PATH"; fi'
    )
    return (
        "set -euo pipefail; "
        f"{load_uv}; "
        "if ! command -v uv >/dev/null 2>&1; then "
        f"curl -LsSf https://astral.sh/uv/{UV_VERSION}/install.sh | sh; "
        f"{load_uv}; "
        "fi; "
        "install_status=1; "
        f"for install_attempt in $(seq 1 {UV_TOOL_INSTALL_ATTEMPTS}); do "
        f"if uv tool install --python 3.11 {package}; then "
        "install_status=0; break; fi; done; "
        'test "$install_status" -eq 0; '
        "mca --version"
    )


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
    resume_attempts: int = 3
    resume_backoff_seconds: int = 10
    allow_shell: bool = False
    streaming: bool = True
    reasoning_effort: str | None = "low"
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
        for name in ("max_retries", "resume_attempts", "resume_backoff_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not self.check_name.strip() or not self.check_command.strip():
            raise ValueError("benchmark check name and command must not be blank")
        if not isinstance(self.streaming, bool):
            raise TypeError("streaming must be a boolean")
        if self.reasoning_effort is not None and not self.reasoning_effort.strip():
            raise ValueError("reasoning_effort must not be blank")
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
    if config.streaming:
        argv.append("--streaming")
    if config.reasoning_effort is not None:
        argv.extend(("--reasoning-effort", config.reasoning_effort))
    if base_url:
        argv.extend(("--base-url", base_url))
    if config.allow_shell:
        argv.append("--allow-shell")
    run_command = shlex.join(argv)
    resume_argv = [
        "mca",
        "tx",
        "resume",
        "MCA_TRANSACTION_ID_PLACEHOLDER",
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
    if config.streaming:
        resume_argv.append("--streaming")
    if config.reasoning_effort is not None:
        resume_argv.extend(("--reasoning-effort", config.reasoning_effort))
    if base_url:
        resume_argv.extend(("--base-url", base_url))
    if config.allow_shell:
        resume_argv.append("--allow-shell")
    resume_command = shlex.join(resume_argv).replace(
        "MCA_TRANSACTION_ID_PLACEHOLDER", '"$transaction_id"'
    )
    state_dir = shlex.quote(config.state_dir)
    log_path = shlex.quote(config.log_path)
    return (
        "set -euo pipefail\n"
        'if [ -f "$HOME/.local/bin/env" ]; then . "$HOME/.local/bin/env"; '
        'else export PATH="$HOME/.local/bin:$PATH"; fi\n'
        f"export MCA_STATE_DIR={state_dir}\n"
        "set +e\n"
        f"{run_command} 2>&1 | tee {log_path}\n"
        "run_status=${PIPESTATUS[0]}\n"
        "set -e\n"
        "transaction_id=$(sed -n 's/^transaction: //p' "
        f"{log_path} | tail -n 1)\n"
        'test -n "$transaction_id"\n'
        "resume_count=0\n"
        f'while [ "$run_status" -ne 0 ] && [ "$resume_count" -lt {config.resume_attempts} ]; do\n'
        "  resume_count=$((resume_count + 1))\n"
        f'  resume_backoff=$((resume_count * {config.resume_backoff_seconds}))\n'
        "  printf 'recovery: attempt=%s backoff_seconds=%s\\n' "
        '"$resume_count" "$resume_backoff" | tee -a '
        f"{log_path}\n"
        '  sleep "$resume_backoff"\n'
        "  resume_started=$(date +%s)\n"
        "  set +e\n"
        f"  {resume_command} 2>&1 | tee -a {log_path}\n"
        "  run_status=${PIPESTATUS[0]}\n"
        "  set -e\n"
        "  resume_finished=$(date +%s)\n"
        "  printf 'recovery: attempt=%s status=%s duration_seconds=%s\\n' "
        '"$resume_count" "$run_status" "$((resume_finished - resume_started))" '
        f"| tee -a {log_path}\n"
        "done\n"
        'test "$run_status" -eq 0\n'
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
        "model_attempts",
        "model_failures",
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
