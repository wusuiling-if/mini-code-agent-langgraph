"""Chat Completions streaming transport used by the mini-swe-agent baseline."""

from __future__ import annotations

import base64
import shlex

STREAMING_MODEL_MODULE = r'''from __future__ import annotations

import litellm

from minisweagent.models.litellm_model import LitellmModel


class StreamingLitellmModel(LitellmModel):
    """Aggregate a streamed Chat Completions response for mini-swe-agent."""

    def _query(self, messages, **kwargs):
        options = dict(kwargs)
        options["stream"] = True
        options.setdefault("stream_options", {"include_usage": True})
        chunks = list(super()._query(messages, **options))
        response = litellm.stream_chunk_builder(chunks, messages=messages)
        if response is None:
            raise RuntimeError("streaming Chat Completions returned no chunks")
        return response
'''


def build_streaming_model_install_command() -> str:
    """Install the audited model shim into mini-swe-agent's isolated uv tool."""

    encoded = base64.b64encode(STREAMING_MODEL_MODULE.encode("utf-8")).decode("ascii")
    script = (
        "import base64,pathlib,site;"
        "target=pathlib.Path(site.getsitepackages()[0])/'mca_streaming_litellm.py';"
        f"target.write_bytes(base64.b64decode({encoded!r}))"
    )
    verify = (
        "from mca_streaming_litellm import StreamingLitellmModel;"
        "print(StreamingLitellmModel.__name__)"
    )
    tool_python = '"$HOME/.local/share/uv/tools/mini-swe-agent/bin/python"'
    return (
        "set -euo pipefail; "
        f"test -x {tool_python}; "
        f"{tool_python} -c {shlex.quote(script)}; "
        f"{tool_python} -c {shlex.quote(verify)}"
    )
