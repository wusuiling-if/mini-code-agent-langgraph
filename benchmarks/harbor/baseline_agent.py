"""Harbor adapter keeping mini-swe-agent on streamed Chat Completions."""

from __future__ import annotations

from typing import Any

from benchmarks.harbor.baseline_transport import (
    build_streaming_model_install_command,
)
from harbor.agents.installed.mini_swe_agent import MiniSweAgent
from harbor.environments.base import BaseEnvironment


class StreamingMiniSweAgent(MiniSweAgent):
    """Use mini-swe-agent unchanged except for the fixed transport shim."""

    def __init__(
        self,
        reasoning_effort: str = "low",
        streaming: bool = True,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        if not isinstance(streaming, bool) or not streaming:
            raise ValueError("the fixed baseline requires streaming=true")
        if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
            raise ValueError("reasoning_effort must not be blank")
        supplied_config = kwargs.pop("config", None)
        supplied_config_file = kwargs.pop("config_file", None)
        if supplied_config is not None or supplied_config_file is not None:
            raise ValueError("the fixed baseline does not accept a custom config")
        config = {
            "model": {
                "model_class": ("mca_streaming_litellm.StreamingLitellmModel"),
                "model_kwargs": {
                    "drop_params": True,
                    "reasoning_effort": reasoning_effort.strip(),
                },
            }
        }
        super().__init__(
            *args,
            reasoning_effort=None,
            config=config,
            **kwargs,
        )

    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)
        await self.exec_as_agent(
            environment,
            command=build_streaming_model_install_command(),
        )
