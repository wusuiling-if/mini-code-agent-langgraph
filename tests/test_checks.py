from __future__ import annotations

import pytest

from mini_code_agent.checks import (
    MAX_VERIFICATION_CHECKS,
    VerificationCheck,
    VerificationCheckEvidence,
    VerificationCheckExecution,
    normalize_verification_checks,
    run_verification_matrix,
)
from mini_code_agent.contracts import ToolResult
from mini_code_agent.utils import DEFAULT_OUTPUT_LIMIT


def test_normalize_verification_checks_preserves_legacy_first_and_explicit_order():
    explicit = (
        VerificationCheck("lint", "ruff check ."),
        VerificationCheck("types", "pyright"),
    )

    checks = normalize_verification_checks(" pytest -q ", explicit)

    assert checks == (
        VerificationCheck("tests", "pytest -q"),
        VerificationCheck("lint", "ruff check ."),
        VerificationCheck("types", "pyright"),
    )


@pytest.mark.parametrize("name", ["", "Tests", "1tests", "has space", "a" * 33])
def test_verification_check_rejects_invalid_names(name: str):
    with pytest.raises(ValueError, match="check name"):
        VerificationCheck(name, "true")


def test_normalize_verification_checks_rejects_duplicates_and_limit():
    with pytest.raises(ValueError, match="duplicate"):
        normalize_verification_checks(
            "pytest -q", (VerificationCheck("tests", "other"),)
        )
    too_many = tuple(
        VerificationCheck(f"check-{index}", "true")
        for index in range(MAX_VERIFICATION_CHECKS + 1)
    )
    with pytest.raises(ValueError, match="at most"):
        normalize_verification_checks(None, too_many)


def test_tool_result_observation_contains_only_structured_check_evidence():
    evidence = VerificationCheckEvidence(
        name="lint",
        returncode=1,
        duration_ms=12,
        exception_info="",
    )
    result = ToolResult(
        tool="run_tests",
        command="<verification matrix>",
        output="lint failed",
        returncode=1,
        duration_ms=12,
        verification_checks=(evidence,),
        verification_boundary_checked=True,
        verification_fingerprint="private-fingerprint",
    )

    observation = result.to_observation()

    assert observation["verification_checks"] == [
        {
            "name": "lint",
            "returncode": 1,
            "duration_ms": 12,
            "blocked": False,
            "approved": True,
        }
    ]
    assert "ruff check" not in str(observation)
    assert "verification_boundary_checked" not in str(observation)
    assert "private-fingerprint" not in str(observation)


def _execution(
    check: VerificationCheck,
    returncode: int = 0,
    *,
    exception_info: str = "",
) -> VerificationCheckExecution:
    return VerificationCheckExecution(
        evidence=VerificationCheckEvidence(
            name=check.name,
            returncode=returncode,
            duration_ms=1,
            exception_info=exception_info,
        ),
        output=f"{check.name} output",
    )


def test_matrix_runs_all_ordinary_results_on_one_fingerprint():
    checks = (
        VerificationCheck("tests", "pytest -q"),
        VerificationCheck("lint", "ruff check ."),
        VerificationCheck("types", "pyright"),
    )
    calls: list[str] = []

    result = run_verification_matrix(
        checks,
        capture_fingerprint=lambda: "stable",
        execute_check=lambda check: (
            calls.append(check.name)
            or _execution(check, 1 if check.name == "lint" else 0)
        ),
    )

    assert calls == ["tests", "lint", "types"]
    assert result.returncode == 1
    assert result.exception_info == "VerificationCheckFailed"
    assert [item.name for item in result.verification_checks] == [
        "tests",
        "lint",
        "types",
    ]


def test_successful_matrix_carries_its_internal_fingerprint():
    check = VerificationCheck("tests", "pytest -q")

    result = run_verification_matrix(
        (check,),
        capture_fingerprint=lambda: "stable",
        execute_check=_execution,
    )

    assert result.returncode == 0
    assert result.verification_fingerprint == "stable"


def test_matrix_stops_when_a_check_changes_the_workspace():
    fingerprints = iter(("f0", "f0", "changed"))
    calls: list[str] = []
    checks = (
        VerificationCheck("tests", "pytest -q"),
        VerificationCheck("lint", "ruff check ."),
    )

    result = run_verification_matrix(
        checks,
        capture_fingerprint=lambda: next(fingerprints),
        execute_check=lambda check: calls.append(check.name) or _execution(check),
    )

    assert calls == ["tests"]
    assert result.returncode == -1
    assert result.exception_info == "WorkspaceChangedDuringVerification"
    assert "tests" in result.output


def test_matrix_fails_closed_when_fingerprinting_raises():
    def fail_capture() -> str:
        raise OSError("private path detail")

    result = run_verification_matrix(
        (VerificationCheck("tests", "pytest -q"),),
        capture_fingerprint=fail_capture,
        execute_check=_execution,
    )

    assert result.returncode == -1
    assert result.exception_info == "WorkspaceFingerprintError"
    assert "private path detail" not in result.output


def test_matrix_stops_on_infrastructure_error_but_not_zero_tests():
    calls: list[str] = []
    checks = (
        VerificationCheck("tests", "pytest -q"),
        VerificationCheck("lint", "ruff check ."),
        VerificationCheck("types", "pyright"),
    )

    def execute(check: VerificationCheck) -> VerificationCheckExecution:
        calls.append(check.name)
        if check.name == "tests":
            return _execution(check, 1, exception_info="NoTestsCollected")
        if check.name == "lint":
            return _execution(check, -1, exception_info="TimeoutExpired")
        return _execution(check)

    result = run_verification_matrix(
        checks,
        capture_fingerprint=lambda: "stable",
        execute_check=execute,
    )

    assert calls == ["tests", "lint"]
    assert result.exception_info == "TimeoutExpired"


def test_matrix_stops_on_blocked_evidence_and_preserves_approval_contract():
    checks = (
        VerificationCheck("tests", "pytest -q"),
        VerificationCheck("lint", "ruff check ."),
    )
    calls: list[str] = []

    def execute(check: VerificationCheck) -> VerificationCheckExecution:
        calls.append(check.name)
        return VerificationCheckExecution(
            evidence=VerificationCheckEvidence(
                name=check.name,
                returncode=-1,
                duration_ms=1,
                exception_info="BlockedByPolicy",
                blocked=True,
            )
        )

    result = run_verification_matrix(
        checks,
        capture_fingerprint=lambda: "stable",
        execute_check=execute,
    )

    assert calls == ["tests"]
    assert result.returncode == -1
    assert result.exception_info == "BlockedByPolicy"
    assert result.blocked is True
    assert result.approved is True


def test_matrix_stops_on_rejected_evidence_with_approval_false():
    checks = (
        VerificationCheck("tests", "pytest -q"),
        VerificationCheck("lint", "ruff check ."),
    )
    calls: list[str] = []

    def execute(check: VerificationCheck) -> VerificationCheckExecution:
        calls.append(check.name)
        return VerificationCheckExecution(
            evidence=VerificationCheckEvidence(
                name=check.name,
                returncode=-1,
                duration_ms=1,
                exception_info="UserRejected",
                approved=False,
            )
        )

    result = run_verification_matrix(
        checks,
        capture_fingerprint=lambda: "stable",
        execute_check=execute,
    )

    assert calls == ["tests"]
    assert result.returncode == -1
    assert result.exception_info == "UserRejected"
    assert result.blocked is False
    assert result.approved is False


def test_matrix_converts_execute_check_exception_to_safe_failure():
    check = VerificationCheck("tests", "pytest -q")

    def execute(_: VerificationCheck) -> VerificationCheckExecution:
        raise RuntimeError("private executor detail")

    result = run_verification_matrix(
        (check,),
        capture_fingerprint=lambda: "stable",
        execute_check=execute,
    )

    assert result.returncode == -1
    assert result.exception_info == "VerificationCheckExecutionError"
    assert result.verification_checks == ()
    assert "private executor detail" not in result.output


def test_matrix_prioritizes_workspace_mutation_over_ordinary_check_failure():
    check = VerificationCheck("tests", "pytest -q")
    fingerprints = iter(("before", "before", "after"))

    result = run_verification_matrix(
        (check,),
        capture_fingerprint=lambda: next(fingerprints),
        execute_check=lambda item: _execution(item, returncode=1),
    )

    assert result.returncode == -1
    assert result.exception_info == "WorkspaceChangedDuringVerification"
    assert [item.returncode for item in result.verification_checks] == [1]


def test_matrix_rendering_bounds_large_failing_check_output():
    checks = tuple(
        VerificationCheck(f"check-{index}", "false") for index in range(8)
    )
    large_output = "A" * 1_500 + "MIDDLE" * 200 + "B" * 1_500

    result = run_verification_matrix(
        checks,
        capture_fingerprint=lambda: "stable",
        execute_check=lambda check: VerificationCheckExecution(
            evidence=VerificationCheckEvidence(
                name=check.name,
                returncode=1,
                duration_ms=1,
            ),
            output=large_output,
        ),
    )

    marker = "...[elided "
    assert marker in result.output
    assert "## check-0" in result.output
    assert "## check-7" in result.output
    assert result.output.startswith("CHECK  STATUS  EXIT  DURATION")
    assert len(result.output) <= DEFAULT_OUTPUT_LIMIT + 64
