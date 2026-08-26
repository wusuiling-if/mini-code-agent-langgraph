"""Optional embedding retrieval with a derived, persistent vector cache."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from memory_core.contracts import SemanticDocument

MAX_RESPONSE_BYTES = 10_000_000


class EmbeddingClient(Protocol):
    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


def _validate_vector(vector: Sequence[float]) -> tuple[float, ...]:
    values = tuple(float(item) for item in vector)
    if not values or len(values) > 65_536:
        raise ValueError("embedding vector dimensions are invalid")
    if any(not math.isfinite(item) for item in values):
        raise ValueError("embedding vector contains a non-finite value")
    return values


class OpenAICompatibleEmbeddingClient:
    """Call an explicit OpenAI-compatible ``/embeddings`` endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("embedding base URL must use HTTP or HTTPS")
        if not model.strip():
            raise ValueError("embedding model must not be blank")
        if timeout_seconds <= 0:
            raise ValueError("embedding timeout must be positive")
        self.endpoint = base_url.rstrip("/") + "/embeddings"
        self.model = model.strip()
        self.api_key = api_key
        self.timeout_seconds = float(timeout_seconds)

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        inputs = tuple(texts)
        if not inputs or any(not isinstance(text, str) or not text for text in inputs):
            raise ValueError("embedding inputs must be non-empty strings")
        payload = json.dumps(
            {"model": self.model, "input": list(inputs)},
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"embedding endpoint request failed: {type(exc).__name__}"
            ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("embedding endpoint response exceeds the size limit")
        try:
            decoded = json.loads(raw.decode("utf-8"))
            rows = decoded["data"]
            ordered = sorted(rows, key=lambda row: int(row["index"]))
            if [int(row["index"]) for row in ordered] != list(range(len(inputs))):
                raise ValueError("embedding response indexes are invalid")
            vectors = tuple(_validate_vector(row["embedding"]) for row in ordered)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "embedding endpoint returned an invalid response"
            ) from exc
        if len(vectors) != len(inputs):
            raise RuntimeError("embedding endpoint returned the wrong vector count")
        dimensions = len(vectors[0])
        if any(len(vector) != dimensions for vector in vectors):
            raise RuntimeError("embedding endpoint returned inconsistent dimensions")
        return vectors


class SQLiteEmbeddingCache:
    """Unsigned derived cache; source text remains authoritative."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()

    def get(self, namespace: str, text_sha256: str) -> tuple[float, ...] | None:
        if not self.path.is_file():
            return None
        self._check_file()
        with sqlite3.connect(self.path) as connection:
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vectors'"
            ).fetchone()
            if table_exists is None:
                return None
            row = connection.execute(
                "SELECT dimensions, vector_json FROM vectors "
                "WHERE namespace = ? AND text_sha256 = ?",
                (namespace, text_sha256),
            ).fetchone()
        if row is None:
            return None
        vector = _validate_vector(json.loads(row[1]))
        if len(vector) != int(row[0]):
            raise ValueError("cached embedding dimensions do not match")
        return vector

    def put(
        self,
        namespace: str,
        text_sha256: str,
        vector: Sequence[float],
    ) -> None:
        values = _validate_vector(vector)
        self._ensure_private_parent()
        self._ensure_private_file()
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS vectors ("
                "namespace TEXT NOT NULL, text_sha256 TEXT NOT NULL, "
                "dimensions INTEGER NOT NULL, vector_json TEXT NOT NULL, "
                "created_at_ns INTEGER NOT NULL, "
                "PRIMARY KEY(namespace, text_sha256))"
            )
            connection.execute(
                "INSERT OR REPLACE INTO vectors "
                "(namespace, text_sha256, dimensions, vector_json, created_at_ns) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    namespace,
                    text_sha256,
                    len(values),
                    json.dumps(values, separators=(",", ":")),
                    time.time_ns(),
                ),
            )

    def _ensure_private_parent(self) -> None:
        if self.path.is_symlink():
            raise PermissionError("embedding cache must not be a symbolic link")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or not self.path.parent.is_dir():
            raise PermissionError("embedding cache parent must be a real directory")
        if os.name != "nt":
            self.path.parent.chmod(0o700)

    def _ensure_private_file(self) -> None:
        try:
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            self._check_file()
        else:
            os.close(descriptor)

    def _check_file(self) -> None:
        metadata = self.path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PermissionError("embedding cache must be a regular file")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("embedding cache is not owned by this user")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise PermissionError("embedding cache permissions are too broad")


class EmbeddingCandidateProvider:
    """Cosine-rank already-eligible documents using cached embeddings."""

    def __init__(
        self,
        client: EmbeddingClient,
        *,
        namespace: str,
        cache: SQLiteEmbeddingCache | None = None,
        min_similarity: float = 0.25,
        max_input_chars: int = 12_000,
        fail_open: bool = True,
    ) -> None:
        if not namespace.strip():
            raise ValueError("embedding namespace must not be blank")
        if not -1 <= min_similarity <= 1:
            raise ValueError("minimum embedding similarity is invalid")
        if max_input_chars < 100:
            raise ValueError("embedding input budget is too small")
        self.client = client
        self.namespace = namespace
        self.cache = cache
        self.min_similarity = float(min_similarity)
        self.max_input_chars = max_input_chars
        self.fail_open = fail_open
        self.last_error_type = ""

    def rank(
        self,
        query: str,
        documents: Sequence[SemanticDocument],
        *,
        limit: int,
    ) -> tuple[tuple[str, float], ...]:
        self.last_error_type = ""
        if not self.fail_open:
            return self._rank(query, documents, limit=limit)
        try:
            return self._rank(query, documents, limit=limit)
        except Exception as exc:  # noqa: BLE001 - optional candidate route boundary
            self.last_error_type = type(exc).__name__
            return ()

    def _rank(
        self,
        query: str,
        documents: Sequence[SemanticDocument],
        *,
        limit: int,
    ) -> tuple[tuple[str, float], ...]:
        if limit < 1 or not documents:
            return ()
        texts = [query[: self.max_input_chars]]
        texts.extend(document.text[: self.max_input_chars] for document in documents)
        vectors: list[tuple[float, ...] | None] = []
        missing: list[tuple[int, str, str]] = []
        for index, text in enumerate(texts):
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            cached = self.cache.get(self.namespace, digest) if self.cache else None
            vectors.append(cached)
            if cached is None:
                missing.append((index, digest, text))
        if missing:
            generated = self.client.embed(tuple(item[2] for item in missing))
            if len(generated) != len(missing):
                raise RuntimeError("embedding client returned the wrong vector count")
            for (index, digest, _text), raw_vector in zip(missing, generated):
                vector = _validate_vector(raw_vector)
                vectors[index] = vector
                if self.cache:
                    self.cache.put(self.namespace, digest, vector)
        completed = tuple(vector for vector in vectors if vector is not None)
        if len(completed) != len(texts):
            raise RuntimeError("embedding vector resolution was incomplete")
        dimensions = len(completed[0])
        if any(len(vector) != dimensions for vector in completed):
            raise RuntimeError("embedding vector dimensions changed")
        query_vector = completed[0]
        ranked = []
        for document, vector in zip(documents, completed[1:]):
            similarity = self._cosine(query_vector, vector)
            if similarity >= self.min_similarity:
                ranked.append((document.document_id, similarity))
        ranked.sort(key=lambda item: (-item[1], item[0]))
        return tuple(ranked[:limit])

    @staticmethod
    def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
        numerator = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if not left_norm or not right_norm:
            return 0.0
        return max(-1.0, min(1.0, numerator / (left_norm * right_norm)))
