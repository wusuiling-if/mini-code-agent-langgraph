from __future__ import annotations

import pytest

from mini_code_agent.checks import (
    MAX_VERIFICATION_CHECKS,
    VerificationCheck,
    VerificationCheckEvidence,
    normalize_verification_checks,
)
from mini_code_agent.contracts import ToolResult


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
