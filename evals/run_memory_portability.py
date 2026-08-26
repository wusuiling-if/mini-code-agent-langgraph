"""Evaluate the host-neutral memory core without an MCA transaction or agent."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from memory_core.contracts import (
    ContextEntry,
    EvidenceReference,
    SemanticDocument,
    VerifiedCheck,
    VerifiedExperience,
)
from memory_core.experience import ExperienceFactory
from memory_core.lifecycle import CapacityPolicy, LifecycleRecord, select_retirements
from memory_core.rendering import ContextBudget
from memory_core.runtime import MemoryRuntime
from memory_core.security import SecretDetector
from memory_core.semantic import EmbeddingCandidateProvider, SQLiteEmbeddingCache

SUITE_NAME = "memory-core-portability-v1"


def _experience() -> VerifiedExperience:
    patch = "diff --git a/parser.py b/parser.py\n+raise ParserTimeout()\n"
    return VerifiedExperience(
        evidence=EvidenceReference(
            "ci_receipt",
            "ci:portable:42",
            hashlib.sha256(b"portable-42").hexdigest(),
            "trusted_tool",
        ),
        scope="project",
        scope_key="sha256:portable",
        valid_from="2026-08-18T00:00:00Z",
        task="fix portable parser timeout",
        checks=(VerifiedCheck("tests", hashlib.sha256(b"portable-test").hexdigest()),),
        artifact_text=patch,
        artifact_size_bytes=len(patch.encode()),
    )


def run_portability() -> dict[str, Any]:
    started = time.monotonic()
    cases: list[dict[str, Any]] = []

    def record(name: str, passed: bool) -> None:
        cases.append({"name": name, "passed": passed})

    experience = _experience()
    factory = ExperienceFactory()
    workflow = factory.workflow(experience)
    repair = factory.repair(experience)
    record(
        "generic-ci-evidence-forms-memory",
        workflow.subtype == "verified_workflow"
        and repair is not None
        and repair.subtype == "verified_repair",
    )

    class Provider:
        def resolve(self, reference: str):
            if reference != "ci:portable:42":
                raise KeyError(reference)
            return experience

    class Identity:
        def identity_sha256(self, project: Path, *, create: bool):
            return "portable"

    class Repository:
        def __init__(self):
            self.records = []

        def admit(self, draft, evidence):
            self.records.append((draft, evidence))
            return str(len(self.records))

        def retrieve(self, query, *, scope, scope_key, limit):
            draft, evidence = self.records[-1]
            return (
                ContextEntry(
                    hashlib.sha256(draft.value.encode()).hexdigest(),
                    draft.value,
                    scope,
                    scope_key,
                    draft.authority,
                    (evidence.source_ref,),
                ),
            )[:limit]

    repository = Repository()
    runtime = MemoryRuntime(
        evidence_provider=Provider(),
        identity_provider=Identity(),
        repository=repository,
    )
    with tempfile.TemporaryDirectory(prefix="memory-portable-host-") as temporary:
        formed = runtime.form("ci:portable:42")
        rendered, audit = runtime.context(
            Path(temporary),
            "portable parser timeout",
            budget=ContextBudget(max_chars=4_000, max_item_chars=3_000),
        )
    record(
        "non-mca-host-runs-end-to-end",
        formed == ("1", "2")
        and audit.decision == "use_memory"
        and "ParserTimeout" in rendered.text,
    )

    huge = "Task: parser timeout\n" + "filler\n" * 10_000 + "+ParserTimeout\n"
    huge_experience = VerifiedExperience(
        **{
            **experience.__dict__,
            "artifact_text": huge,
            "artifact_size_bytes": len(huge.encode()),
        }
    )
    huge_repair = factory.repair(huge_experience)
    if huge_repair is None:
        bounded = False
    else:
        repository.records.append((huge_repair, experience.evidence))
        with tempfile.TemporaryDirectory(prefix="memory-portable-budget-") as temporary:
            bounded_render, _ = runtime.context(
                Path(temporary),
                "parser timeout",
                budget=ContextBudget(max_chars=4_000, max_item_chars=3_000),
            )
        bounded = len(bounded_render.text) <= 4_000 and bounded_render.truncated
    record("context-budget-is-hard", bounded)

    retired = select_retirements(
        (
            LifecycleRecord("old", 1, 50),
            LifecycleRecord("new", 2, 50),
        ),
        incoming_chars=50,
        policy=CapacityPolicy(
            max_active_records_per_scope=2,
            max_active_chars_per_scope=100,
        ),
    )
    detector = SecretDetector()
    record(
        "capacity-and-secret-policies-are-host-neutral",
        retired == ("old",)
        and detector.contains_secret("password=portable-secret-value")
        and not detector.contains_secret('TOKEN = os.environ["TOKEN"]'),
    )

    class EmbeddingClient:
        def __init__(self):
            self.calls = 0

        def embed(self, texts):
            self.calls += 1
            values = {
                "parser timeout": (1.0, 0.0),
                "deadline failure": (0.9, 0.1),
                "billing": (0.0, 1.0),
            }
            return tuple(values[text] for text in texts)

    embedding_client = EmbeddingClient()
    with tempfile.TemporaryDirectory(prefix="memory-portable-embedding-") as temporary:
        semantic = EmbeddingCandidateProvider(
            embedding_client,
            namespace="portable-embedding-v1",
            cache=SQLiteEmbeddingCache(Path(temporary) / "vectors.sqlite3"),
            min_similarity=0.5,
        )
        documents = (
            SemanticDocument("repair", "deadline failure"),
            SemanticDocument("other", "billing"),
        )
        first_ranking = semantic.rank("parser timeout", documents, limit=2)
        second_ranking = semantic.rank("parser timeout", documents, limit=2)
    record(
        "embedding-backend-is-optional-and-cached",
        first_ranking == second_ranking
        and first_ranking[0][0] == "repair"
        and embedding_client.calls == 1,
    )

    passed = sum(int(case["passed"]) for case in cases)
    return {
        "suite": SUITE_NAME,
        "scope": {
            "offline": True,
            "deterministic": True,
            "model_calls": 0,
            "mca_transaction_required": False,
            "mca_agent_required": False,
        },
        "aggregate": {
            "cases": len(cases),
            "passed": passed,
            "pass_rate": round(passed / len(cases), 4),
        },
        "cases": cases,
        "acceptance": {"passed": passed == len(cases)},
        "elapsed_seconds": round(time.monotonic() - started, 4),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = run_portability()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"suite: {report['suite']}")
        print(f"cases: {report['aggregate']['passed']}/{report['aggregate']['cases']}")
    return 0 if report["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
