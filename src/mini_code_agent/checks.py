from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence


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
