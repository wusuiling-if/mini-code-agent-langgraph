"""Deterministic memory formation from host-authenticated experience."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from memory_core.contracts import MemoryDraft, VerifiedExperience
from memory_core.security import SecretDetector


@dataclass(frozen=True)
class ExperiencePolicy:
    max_artifact_bytes: int = 200_000
    max_cues: int = 24


def _tokens(text: str) -> tuple[str, ...]:
    normalized = " ".join(text.casefold().split())
    values = re.findall(r"[a-z0-9][a-z0-9_.:/+\-]*", normalized)
    for span in re.findall(r"[\u3400-\u9fff]+", normalized):
        values.extend(
            (span,)
            if len(span) == 1
            else (span[i : i + 2] for i in range(len(span) - 1))
        )
    return tuple(dict.fromkeys(item for item in values if len(item) >= 2))


class ExperienceFactory:
    def __init__(
        self,
        *,
        policy: ExperiencePolicy | None = None,
        secret_detector: SecretDetector | None = None,
    ) -> None:
        self.policy = policy or ExperiencePolicy()
        self.secret_detector = secret_detector or SecretDetector()

    def workflow(self, experience: VerifiedExperience) -> MemoryDraft:
        if not experience.checks:
            raise ValueError("verified experience has no checks")
        names: list[str] = []
        bindings = []
        for check in experience.checks:
            if (
                not check.name
                or check.name in names
                or not re.fullmatch(r"[0-9a-f]{64}", check.command_sha256)
            ):
                raise ValueError("verified check is not command-bound")
            names.append(check.name)
            bindings.append(
                {"name": check.name, "command_sha256": check.command_sha256}
            )
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "schema_version": 1,
                    "workspace_identity": experience.scope_key.removeprefix("sha256:"),
                    "checks": bindings,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        anchor = f"workflow-sha256:{fingerprint}"
        rendered = ", ".join(names)
        return MemoryDraft(
            value=(
                "Before submission, run the configured verification matrix. "
                f"Its authenticated checks are: {rendered}."
            ),
            abstraction=(
                "Workspace verification workflow: run_tests executes the configured "
                f"checks before submission ({rendered})."
            ),
            cue_anchors=("run_tests", "verification workflow", *names, anchor),
            kind="procedural",
            subtype="verified_workflow",
            scope=experience.scope,
            scope_key=experience.scope_key,
            origin="agent",
            authority="inform",
            confidence=0.9,
            importance=0.8,
            valid_from=experience.valid_from,
            identity_anchor=anchor,
        )

    def repair(self, experience: VerifiedExperience) -> MemoryDraft | None:
        artifact = experience.artifact_text
        if (
            not artifact
            or experience.artifact_binary
            or experience.artifact_size_bytes > self.policy.max_artifact_bytes
            or self.secret_detector.contains_secret(f"{experience.task}\n{artifact}")
        ):
            return None
        return MemoryDraft(
            value=(
                "Automatically captured verified repair experience.\n"
                f"Task: {experience.task}\n"
                "Authenticated implementation patch:\n"
                f"{artifact}"
            ),
            abstraction=experience.task,
            cue_anchors=_tokens(experience.task)[: self.policy.max_cues],
            kind="episodic",
            subtype="verified_repair",
            scope=experience.scope,
            scope_key=experience.scope_key,
            origin="trusted_tool",
            authority="inform",
            confidence=1.0,
            importance=0.8,
            valid_from=experience.valid_from,
        )
