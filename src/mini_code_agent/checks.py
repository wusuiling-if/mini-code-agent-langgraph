from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Sequence

from mini_code_agent.utils import DEFAULT_OUTPUT_LIMIT, truncate_text


MAX_VERIFICATION_CHECKS = 16
_CHECK_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


@dataclass(frozen=True)
class VerificationCheck:
    name: str
    command: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _CHECK_NAME.fullmatch(self.name):
            raise ValueError("check name must match [a-z][a-z0-9_-]{0,31}")
        if not isinstance(self.command, str) or not self.command.strip():
            raise ValueError("check command must not be blank")
        object.__setattr__(self, "command", self.command.strip())


@dataclass(frozen=True)
class VerificationCheckEvidence:
    name: str
    returncode: int
    duration_ms: int
    tests_run: int | None = None
    exception_info: str = ""
    blocked: bool = False
    approved: bool = True

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "name": self.name,
            "returncode": self.returncode,
            "duration_ms": self.duration_ms,
            "blocked": self.blocked,
            "approved": self.approved,
        }
        if self.tests_run is not None:
            data["tests_run"] = self.tests_run
        if self.exception_info:
            data["exception_info"] = self.exception_info
        return data


def normalize_verification_checks(
    default_test_command: str | None,
    explicit_checks: Sequence[VerificationCheck],
) -> tuple[VerificationCheck, ...]:
    checks: list[VerificationCheck] = []
    if default_test_command is not None:
        checks.append(VerificationCheck("tests", default_test_command))
    checks.extend(explicit_checks)
    if len(checks) > MAX_VERIFICATION_CHECKS:
        raise ValueError(
            f"configure at most {MAX_VERIFICATION_CHECKS} verification checks"
        )
    seen: set[str] = set()
    for check in checks:
        if check.name in seen:
            raise ValueError(f"duplicate verification check name: {check.name}")
        seen.add(check.name)
    return tuple(checks)


@dataclass(frozen=True)
class VerificationCheckExecution:
    evidence: VerificationCheckEvidence
    output: str = ""


@dataclass(frozen=True)
class VerificationMatrixResult:
    verification_checks: tuple[VerificationCheckEvidence, ...]
    output: str
    returncode: int
    exception_info: str = ""
    blocked: bool = False
    approved: bool = True
    verification_fingerprint: str = ""


def _render_matrix(
    evidence: Sequence[VerificationCheckEvidence],
    outputs: Sequence[str],
    *,
    headline: str = "",
) -> str:
    lines = ["CHECK  STATUS  EXIT  DURATION"]
    for item in evidence:
        if item.blocked:
            status = "BLOCKED"
        elif not item.approved:
            status = "REJECTED"
        else:
            status = "PASS" if item.returncode == 0 else "FAIL"
        lines.append(
            f"{item.name}  {status}  {item.returncode}  {item.duration_ms}ms"
        )
    if headline:
        lines.append("")
        lines.append(headline)
    for item, output in zip(evidence, outputs):
        if item.returncode != 0 and output:
            lines.extend(
                ("", f"## {item.name}", truncate_text(output, 2_000))
            )
    return truncate_text("\n".join(lines), DEFAULT_OUTPUT_LIMIT)


def _matrix_failure(
    evidence: Sequence[VerificationCheckEvidence],
    outputs: Sequence[str],
    *,
    exception_info: str,
    headline: str,
    blocked: bool = False,
    approved: bool = True,
) -> VerificationMatrixResult:
    return VerificationMatrixResult(
        verification_checks=tuple(evidence),
        output=_render_matrix(evidence, outputs, headline=headline),
        returncode=-1,
        exception_info=exception_info,
        blocked=blocked,
        approved=approved,
    )


def run_verification_matrix(
    checks: Sequence[VerificationCheck],
    *,
    capture_fingerprint: Callable[[], str],
    execute_check: Callable[[VerificationCheck], VerificationCheckExecution],
) -> VerificationMatrixResult:
    evidence: list[VerificationCheckEvidence] = []
    outputs: list[str] = []
    try:
        baseline = capture_fingerprint()
    except Exception:
        return _matrix_failure(
            evidence,
            outputs,
            exception_info="WorkspaceFingerprintError",
            headline="Workspace fingerprint capture failed before verification.",
        )

    for check in checks:
        try:
            before = capture_fingerprint()
        except Exception:
            return _matrix_failure(
                evidence,
                outputs,
                exception_info="WorkspaceFingerprintError",
                headline=f"Workspace fingerprint capture failed before {check.name}.",
            )
        if before != baseline:
            return _matrix_failure(
                evidence,
                outputs,
                exception_info="WorkspaceChangedDuringVerification",
                headline=f"Workspace changed before check {check.name}.",
            )
        try:
            execution = execute_check(check)
        except Exception:
            return _matrix_failure(
                evidence,
                outputs,
                exception_info="VerificationCheckExecutionError",
                headline=f"Check {check.name} could not be executed safely.",
            )
        evidence.append(execution.evidence)
        outputs.append(execution.output)
        try:
            after = capture_fingerprint()
        except Exception:
            return _matrix_failure(
                evidence,
                outputs,
                exception_info="WorkspaceFingerprintError",
                headline=f"Workspace fingerprint capture failed after {check.name}.",
            )
        if after != baseline:
            return _matrix_failure(
                evidence,
                outputs,
                exception_info="WorkspaceChangedDuringVerification",
                headline=f"Check {check.name} changed the fingerprinted workspace.",
            )
        item = execution.evidence
        if item.blocked or not item.approved:
            return _matrix_failure(
                evidence,
                outputs,
                exception_info=item.exception_info or "VerificationCheckBlocked",
                headline=f"Check {check.name} was not authorized.",
                blocked=item.blocked,
                approved=item.approved,
            )
        if item.exception_info and item.exception_info != "NoTestsCollected":
            return _matrix_failure(
                evidence,
                outputs,
                exception_info=item.exception_info,
                headline=f"Check {check.name} stopped the verification matrix.",
            )

    passed = all(item.returncode == 0 for item in evidence)
    return VerificationMatrixResult(
        verification_checks=tuple(evidence),
        output=_render_matrix(evidence, outputs),
        returncode=0 if passed else 1,
        exception_info="" if passed else "VerificationCheckFailed",
        verification_fingerprint=baseline if passed else "",
    )
