"""Harbor 0.22 installed-agent adapter for MCA transactional benchmark runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from benchmarks.harbor.protocol import (
    HarborRunConfig,
    build_agent_install_command,
    build_transaction_command,
    load_latest_run,
    split_harbor_model,
    usage_from_trajectory,
)
from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.model_connection import ModelConnectionSpec
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


class MiniCodeAgentHarborAdapter(BaseInstalledAgent):
    """Run MCA in a Git transaction, then expose the committed patch to Harbor."""

    MODEL_CONNECTION = ModelConnectionSpec(
        api_key_envs=("MCA_API_KEY",),
        base_url_envs=("MCA_BASE_URL",),
        passthrough=True,
    )

    def __init__(
        self,
        package_spec: str = "mini-code-agent-langgraph==0.5.0",
        max_steps: int = 50,
        context_chars: int = 60_000,
        command_timeout: int = 120,
        request_timeout: int = 180,
        max_retries: int = 0,
        resume_attempts: int = 3,
        resume_backoff_seconds: int = 10,
        allow_shell: bool = False,
        streaming: bool = True,
        reasoning_effort: str | None = "low",
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if not isinstance(package_spec, str) or not package_spec.strip():
            raise ValueError("package_spec must not be blank")
        self._package_spec = package_spec
        self._run_metadata: dict[str, Any] = {}
        self._run_config = HarborRunConfig(
            max_steps=max_steps,
            context_chars=context_chars,
            command_timeout=command_timeout,
            request_timeout=request_timeout,
            max_retries=max_retries,
            resume_attempts=resume_attempts,
            resume_backoff_seconds=resume_backoff_seconds,
            allow_shell=allow_shell,
            streaming=streaming,
            reasoning_effort=reasoning_effort,
        )

    @staticmethod
    def name() -> str:
        return "mini-code-agent"

    def get_version_command(self) -> str | None:
        return (
            'if [ -f "$HOME/.local/bin/env" ]; then . "$HOME/.local/bin/env"; '
            'else export PATH="$HOME/.local/bin:$PATH"; fi; mca --version'
        )

    async def install(self, environment: BaseEnvironment) -> None:
        await self.ensure_system_dependencies(
            environment,
            ("curl", "bash", "git", "python3"),
        )
        await self.exec_as_agent(
            environment,
            command=build_agent_install_command(self._package_spec),
        )

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if not self.model_name:
            raise ValueError("Harbor must provide a fixed provider/model")
        provider, _model = split_harbor_model(self.model_name)
        access = self.model_connection
        if access.api_key is None:
            raise ValueError(f"no API key resolved for {self.model_name!r}")
        env = dict(access.env)
        env.setdefault("MCA_API_KEY", access.api_key)
        env["MCA_STATE_DIR"] = self._run_config.state_dir
        command = build_transaction_command(
            instruction,
            self.model_name,
            base_url=access.configured_base_url,
            config=self._run_config,
        )
        self._run_metadata = {
            "mca": {
                "provider": provider,
                "memory": "off",
                "shell_enabled": self._run_config.allow_shell,
                "inner_sandbox": "none",
                "outer_sandbox": "harbor_environment",
                "agent_visible_check": self._run_config.check_command,
                "hidden_verifier_exposed": False,
                "transport_api": "chat_completions",
                "streaming": self._run_config.streaming,
                "reasoning_effort": self._run_config.reasoning_effort,
            }
        }
        await self.exec_as_agent(environment, command=command, env=env)

    def populate_context_post_run(self, context: AgentContext) -> None:
        context.metadata = {**(context.metadata or {}), **self._run_metadata}
        try:
            trajectory, manifest = load_latest_run(Path(self.logs_dir))
        except (FileNotFoundError, OSError, UnicodeError, ValueError):
            return
        usage = usage_from_trajectory(trajectory)
        recovery = trajectory.get("recovery") or {}
        if not isinstance(recovery, dict):
            recovery = {}
        context.n_input_tokens = usage["input_tokens"]
        context.n_output_tokens = usage["output_tokens"]
        context.n_cache_tokens = usage["cached_input_tokens"]
        context.metadata = {
            **(context.metadata or {}),
            "mca_result": {
                "steps": int(trajectory.get("steps", 0)),
                "model_calls": usage["model_calls"],
                "model_attempts": usage["model_attempts"],
                "model_failures": usage["model_failures"],
                "reasoning_tokens": usage["reasoning_tokens"],
                "exit_status": str(trajectory.get("exit_status", "unknown")),
                "verification_status": str(
                    trajectory.get("verification_status", "unknown")
                ),
                "transaction_status": (
                    str(manifest.get("status", "unknown")) if manifest else "unknown"
                ),
                "recovery_attempts": int(recovery.get("attempt_count", 0)),
                "resume_count": int(recovery.get("resume_count", 0)),
                "last_failure_type": str(
                    recovery.get("last_failure_type", "")
                ),
            },
        }
