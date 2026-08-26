"""Explicit MCA configuration for the optional embedding candidate route."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from memory_core.semantic import (
    EmbeddingCandidateProvider,
    OpenAICompatibleEmbeddingClient,
    SQLiteEmbeddingCache,
)


def semantic_provider_from_args(
    args: Any,
    *,
    state_root: Path,
) -> EmbeddingCandidateProvider | None:
    base_url = getattr(args, "embedding_base_url", None) or os.getenv(
        "MCA_EMBEDDING_BASE_URL"
    )
    model = getattr(args, "embedding_model", None) or os.getenv("MCA_EMBEDDING_MODEL")
    if not base_url and not model:
        return None
    if not base_url or not model:
        raise RuntimeError(
            "embedding retrieval requires both --embedding-base-url and "
            "--embedding-model (or MCA_EMBEDDING_BASE_URL/MCA_EMBEDDING_MODEL)"
        )
    key_name = getattr(args, "embedding_api_key_env", None) or ("MCA_EMBEDDING_API_KEY")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_name):
        raise RuntimeError("embedding API key environment variable name is invalid")
    timeout = float(getattr(args, "embedding_timeout", 30.0))
    namespace = hashlib.sha256(f"{base_url.rstrip('/')}|{model}".encode()).hexdigest()
    return EmbeddingCandidateProvider(
        OpenAICompatibleEmbeddingClient(
            base_url=base_url,
            model=model,
            api_key=os.getenv(key_name),
            timeout_seconds=timeout,
        ),
        namespace=namespace,
        cache=SQLiteEmbeddingCache(
            Path(state_root).expanduser().resolve()
            / "memory"
            / "embedding-cache.sqlite3"
        ),
    )
