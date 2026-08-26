from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

from memory_core.contracts import (
    ContextEntry,
    EvidenceReference,
    VerifiedCheck,
    VerifiedExperience,
)
from memory_core.experience import ExperienceFactory
from memory_core.lifecycle import CapacityPolicy, LifecycleRecord, select_retirements
from memory_core.rendering import ContextBudget, render_context
from memory_core.runtime import MemoryRuntime
from memory_core.security import SecretDetector
from memory_core.semantic import (
    EmbeddingCandidateProvider,
    OpenAICompatibleEmbeddingClient,
    SQLiteEmbeddingCache,
)


def _experience(*, artifact: str = "diff --git a/a.py b/a.py\n+VALUE = 2\n"):
    return VerifiedExperience(
        evidence=EvidenceReference(
            "ci_receipt",
            "ci:run:42",
            hashlib.sha256(b"ci-run-42").hexdigest(),
            "trusted_tool",
        ),
        scope="project",
        scope_key="project:portable",
        valid_from="2026-08-18T00:00:00Z",
        task="修复 portable parser timeout",
        checks=(VerifiedCheck("tests", hashlib.sha256(b"pytest -q").hexdigest()),),
        artifact_text=artifact,
        artifact_size_bytes=len(artifact.encode()),
    )


def test_host_neutral_core_forms_experience_without_mca_runtime():
    factory = ExperienceFactory()

    workflow = factory.workflow(_experience())
    repair = factory.repair(_experience())

    assert workflow.subtype == "verified_workflow"
    assert workflow.scope_key == "project:portable"
    assert repair is not None
    assert repair.subtype == "verified_repair"
    assert "portable" in repair.cue_anchors
    assert "解析" not in repair.cue_anchors
    assert "修复" in repair.cue_anchors


def test_memory_core_imports_when_mca_package_is_blocked(tmp_path: Path):
    script = tmp_path / "probe.py"
    script.write_text(
        "import sys\n"
        "sys.modules['mini_code_agent'] = None\n"
        "from memory_core import ExperienceFactory, VerifiedExperience\n"
        "assert ExperienceFactory and VerifiedExperience\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(script)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_non_mca_host_runs_evidence_to_context_end_to_end(tmp_path: Path):
    experience = _experience()
    identity = experience.scope_key.removeprefix("project:")

    class Provider:
        def resolve(self, reference: str):
            assert reference == "ci:run:42"
            return experience

    class Identity:
        def identity_sha256(self, project: Path, *, create: bool):
            assert project == tmp_path
            assert create is True
            return identity

    class Repository:
        def __init__(self):
            self.records = []

        def admit(self, draft, evidence):
            self.records.append((draft, evidence))
            return f"record-{len(self.records)}"

        def retrieve(self, query, *, scope, scope_key, limit):
            assert query == "portable parser timeout"
            assert scope == "project"
            assert scope_key == f"sha256:{identity}"
            draft, evidence = self.records[-1]
            return (
                ContextEntry(
                    content_sha256=hashlib.sha256(draft.value.encode()).hexdigest(),
                    value=draft.value,
                    scope=scope,
                    scope_key=scope_key,
                    authority=draft.authority,
                    evidence_refs=(evidence.source_ref,),
                ),
            )[:limit]

    repository = Repository()
    runtime = MemoryRuntime(
        evidence_provider=Provider(),
        identity_provider=Identity(),
        repository=repository,
    )

    formed = runtime.form("ci:run:42")
    rendered, audit = runtime.context(tmp_path, "portable parser timeout")

    assert formed == ("record-1", "record-2")
    assert "Authenticated implementation patch" in rendered.text
    assert audit.decision == "use_memory"
    assert audit.context_chars == len(rendered.text)


def test_context_renderer_never_exceeds_budget_and_keeps_relevant_hunk():
    filler = " unchanged filler line\n" * 3_000
    value = (
        "Task: fix parser timeout\n"
        + filler
        + "diff --git a/parser.py b/parser.py\n"
        + "@@ timeout handling @@\n"
        + "+raise ParserTimeout()\n"
    )
    entry = ContextEntry(
        content_sha256=hashlib.sha256(value.encode()).hexdigest(),
        value=value,
        scope="project",
        scope_key="portable",
        authority="inform",
        evidence_refs=("ci:run:42",),
    )

    rendered = render_context(
        (entry,),
        query="fix parser timeout",
        budget=ContextBudget(max_chars=4_000, max_item_chars=3_000),
    )

    assert len(rendered.text) <= 4_000
    assert rendered.truncated is True
    assert "ParserTimeout" in rendered.text
    assert "memory truncated" in rendered.text


def test_capacity_policy_retires_oldest_without_deleting_history():
    records = (
        LifecycleRecord("old", 1, 40),
        LifecycleRecord("middle", 2, 40),
        LifecycleRecord("new", 3, 40),
    )

    retired = select_retirements(
        records,
        incoming_chars=40,
        policy=CapacityPolicy(
            max_active_records_per_scope=3,
            max_active_chars_per_scope=120,
        ),
    )

    assert retired == ("old",)


def test_secret_detector_covers_provider_tokens_entropy_and_env_references():
    detector = SecretDetector()

    assert detector.contains_secret("Authorization: Bearer abcdefghijklmnop")
    assert detector.contains_secret("password=correct-horse-battery-staple")
    assert detector.contains_secret("api_key=A1b2C3d4E5f6G7h8I9j0K1l2")
    assert not detector.contains_secret('API_KEY = os.environ["API_KEY"]')


def test_embedding_provider_persists_vectors_and_reuses_cache(tmp_path: Path):
    class Client:
        def __init__(self):
            self.calls = []

        def embed(self, texts):
            self.calls.append(tuple(texts))
            vectors = {
                "parser timeout": (1.0, 0.0),
                "repair parser deadline": (0.9, 0.1),
                "billing preference": (0.0, 1.0),
            }
            return tuple(vectors[text] for text in texts)

    client = Client()
    cache = SQLiteEmbeddingCache(tmp_path / "cache" / "vectors.sqlite3")
    provider = EmbeddingCandidateProvider(
        client,
        namespace="fixture-model-v1",
        cache=cache,
        min_similarity=0.5,
    )
    documents = (
        SimpleSemanticDocument("parser", "repair parser deadline"),
        SimpleSemanticDocument("billing", "billing preference"),
    )

    first = provider.rank("parser timeout", documents, limit=2)
    second = provider.rank("parser timeout", documents, limit=2)

    assert [item[0] for item in first] == ["parser"]
    assert second == first
    assert len(client.calls) == 1
    assert cache.path.stat().st_mode & 0o077 == 0


def test_embedding_cache_recovers_from_an_empty_private_file(tmp_path: Path):
    path = tmp_path / "vectors.sqlite3"
    path.touch(mode=0o600)
    path.chmod(0o600)
    cache = SQLiteEmbeddingCache(path)

    assert cache.get("model", "digest") is None

    cache.put("model", "digest", (1.0, 0.0))
    assert cache.get("model", "digest") == (1.0, 0.0)


def test_embedding_provider_falls_back_when_optional_backend_is_down():
    class FailingClient:
        def embed(self, texts):
            raise RuntimeError("offline")

    provider = EmbeddingCandidateProvider(FailingClient(), namespace="offline-backend")

    result = provider.rank(
        "query",
        (SimpleSemanticDocument("doc", "document"),),
        limit=1,
    )

    assert result == ()
    assert provider.last_error_type == "RuntimeError"


def test_openai_compatible_embedding_client_uses_explicit_endpoint(
    monkeypatch,
):
    observed = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read(_limit):
            return json.dumps(
                {
                    "data": [
                        {"index": 1, "embedding": [0.0, 1.0]},
                        {"index": 0, "embedding": [1.0, 0.0]},
                    ]
                }
            ).encode()

    def fake_urlopen(request, *, timeout):
        observed["url"] = request.full_url
        observed["authorization"] = request.headers.get("Authorization")
        observed["timeout"] = timeout
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = OpenAICompatibleEmbeddingClient(
        base_url="http://127.0.0.1:9999/v1",
        model="local-embed",
        api_key="private-key",
        timeout_seconds=5,
    )

    vectors = client.embed(("one", "two"))

    assert vectors == ((1.0, 0.0), (0.0, 1.0))
    assert observed == {
        "url": "http://127.0.0.1:9999/v1/embeddings",
        "authorization": "Bearer private-key",
        "timeout": 5.0,
    }


class SimpleSemanticDocument:
    def __init__(self, document_id: str, text: str):
        self.document_id = document_id
        self.text = text
