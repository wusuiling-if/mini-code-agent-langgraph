from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mini_code_agent.memory_adapters.semantic import semantic_provider_from_args


def _args(**overrides):
    values = {
        "embedding_base_url": None,
        "embedding_model": None,
        "embedding_api_key_env": "MCA_EMBEDDING_API_KEY",
        "embedding_timeout": 30.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_embedding_adapter_is_off_and_read_only_without_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("MCA_EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("MCA_EMBEDDING_MODEL", raising=False)

    provider = semantic_provider_from_args(_args(), state_root=tmp_path / "state")

    assert provider is None
    assert not (tmp_path / "state").exists()


def test_embedding_adapter_requires_model_and_endpoint_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("MCA_EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("MCA_EMBEDDING_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="requires both"):
        semantic_provider_from_args(
            _args(embedding_model="embed-v1"),
            state_root=tmp_path / "state",
        )


def test_embedding_adapter_builds_lazy_openai_compatible_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("PRIVATE_EMBED_KEY", "not-written-to-cache")
    state_root = tmp_path / "state"

    provider = semantic_provider_from_args(
        _args(
            embedding_base_url="http://127.0.0.1:9999/v1",
            embedding_model="local-embed",
            embedding_api_key_env="PRIVATE_EMBED_KEY",
            embedding_timeout=5.0,
        ),
        state_root=state_root,
    )

    assert provider is not None
    assert provider.client.endpoint == "http://127.0.0.1:9999/v1/embeddings"
    assert provider.client.model == "local-embed"
    assert provider.client.api_key == "not-written-to-cache"
    assert provider.cache is not None
    assert provider.cache.path == state_root / "memory" / "embedding-cache.sqlite3"
    assert not state_root.exists()


def test_embedding_adapter_rejects_invalid_key_environment_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("MCA_EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("MCA_EMBEDDING_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="variable name"):
        semantic_provider_from_args(
            _args(
                embedding_base_url="http://127.0.0.1:9999/v1",
                embedding_model="local-embed",
                embedding_api_key_env="BAD-NAME",
            ),
            state_root=tmp_path / "state",
        )
