# Multi-Check Verification Matrix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add repeatable named verification checks whose successful results are
all bound to one unchanged workspace fingerprint, and close the equivalent
legacy mutation gap without changing stable `--test-command` output or events.

**Architecture:** A standard-library-only `checks.py` module owns immutable check/evidence values, validation, aggregation, and strict fingerprint transaction logic through injected callbacks. `BashExecutor` supplies command execution, sandboxing, approval, redaction, zero-test handling, and fingerprint capture; the existing scalar `VerificationGate` consumes one aggregate `run_tests` result and remains unchanged.

**Tech Stack:** Python 3.10–3.13, dataclasses, argparse, existing `BashExecutor`/LangGraph runtime, pytest, existing workspace fingerprinter and sandbox backends.

**Execution prerequisite:** This plan and its approved design clarifications are
committed together before Task 1 begins. Implementers start from that planning
commit; they do not recreate or amend the planning files during Tasks 1–6.

## Global Constraints

- Keep runtime dependencies unchanged.
- Keep the model-facing tool named `run_tests` and argument-free.
- Preserve the stable legacy output/event surface when only `--test-command`
  is configured; newly reject commands that leave fingerprinted mutations.
- Use strict matrix behavior whenever at least one explicit `--check` is configured.
- Run checks serially in declaration order with one approval and at most 16 checks.
- Every before/after fingerprint in a matrix must equal the matrix baseline `F0`.
- The post-tool fingerprint at the executor/gate handoff must also equal `F0`.
- Stop immediately on a persisted boundary mutation, fingerprint failure,
  timeout, interruption, sandbox/policy failure, or rejected approval.
- Continue after ordinary nonzero results only while the fingerprint remains `F0`.
- Never directly serialize raw matrix command configuration, unbounded output,
  or trusted ignore-path configuration. Bounded command output may echo
  arbitrary text; apply existing best-effort redaction and continue to treat
  trajectories as sensitive.
- Keep `ToolExecutor`, `VerificationGate`, checkpoint schemas, fingerprint coverage, sandbox selection, and the 11-case v0.3.2 benchmark contract unchanged.
- Do not add TrustBench extraction, third-party adapters, parallel checks, auto-discovery, selective reruns, or Windows runtime support.

**Approved design:** `docs/superpowers/specs/2026-07-22-multi-check-verification-matrix-design.md`

---

## File Structure

- Create `src/mini_code_agent/checks.py`: immutable check/evidence values, normalization, strict transaction runner, and bounded summary rendering.
- Modify `src/mini_code_agent/contracts.py`: additive per-check evidence plus
  non-serialized boundary-attestation fields on `ToolResult`.
- Modify `src/mini_code_agent/executor.py`: legacy/matrix routing, full preflight, one approval, sandboxed execution callbacks, and result conversion.
- Modify `src/mini_code_agent/verification.py`: trusted ignore-path injection
  and fail-closed executor-to-gate fingerprint comparison.
- Modify `src/mini_code_agent/cli.py`: repeatable `--check NAME COMMAND`, run/chat validation, and executor plumbing.
- Modify `src/mini_code_agent/agent.py` and `src/mini_code_agent/chat.py`: persist additive redacted check evidence.
- Modify `src/mini_code_agent/model.py` and `src/mini_code_agent/prompts.py`: describe `run_tests` as the configured verification matrix.
- Create `tests/test_checks.py`: pure normalization and strict transaction tests.
- Modify `tests/test_executor.py`: executor matrix, compatibility, approval, redaction, and zero-test tests.
- Modify `tests/test_process_cleanup.py`: matrix timeout and child cleanup coverage.
- Modify `tests/test_cli_launch.py`, `tests/test_hardening.py`, and `tests/test_agent_cli.py`: CLI, lifecycle, same-batch, resume, and end-to-end verification tests.
- Modify `README.md`, `README.zh-CN.md`, `SECURITY.md`, `CHANGELOG.md`, `pyproject.toml`, `src/mini_code_agent/__init__.py`, and the version assertion in `tests/test_cli_launch.py`.

---

### Task 1: Define named-check configuration and serializable evidence

**Files:**
- Create: `src/mini_code_agent/checks.py`
- Modify: `src/mini_code_agent/contracts.py:3-62`
- Create: `tests/test_checks.py`

**Interfaces:**
- Produces: `VerificationCheck`, `VerificationCheckEvidence`, `MAX_VERIFICATION_CHECKS`, and `normalize_verification_checks(default_test_command, explicit_checks)`.
- Produces: additive `ToolResult.verification_checks`,
  `verification_boundary_checked`, and `verification_fingerprint` fields; the
  latter two remain internal and non-serialized.
- Consumes: only the Python standard library and existing `truncate_text` through `ToolResult.to_observation()`.

- [ ] **Step 1: Write failing normalization and observation tests**

Create `tests/test_checks.py` with:

```python
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
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_checks.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'mini_code_agent.checks'`.

- [ ] **Step 3: Implement immutable values and normalization**

Create `src/mini_code_agent/checks.py`:

```python
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
            raise ValueError(
                "check name must match [a-z][a-z0-9_-]{0,31}"
            )
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
```

Modify `src/mini_code_agent/contracts.py`:

```python
from mini_code_agent.checks import VerificationCheckEvidence
```

Add these fields after `tests_run`:

```python
    verification_checks: tuple[VerificationCheckEvidence, ...] = ()
    verification_boundary_checked: bool = False
    verification_fingerprint: str = ""
```

Add this block at the end of `ToolResult.to_observation()`, before `return data`:

```python
        if self.verification_checks:
            data["verification_checks"] = [
                evidence.to_dict() for evidence in self.verification_checks
            ]
```

- [ ] **Step 4: Run focused and compatibility tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_checks.py tests/test_architecture.py -q
```

Expected: all tests pass and the fake `ToolExecutor` remains valid.

- [ ] **Step 5: Commit the configuration contract**

```bash
git add src/mini_code_agent/checks.py src/mini_code_agent/contracts.py tests/test_checks.py
git commit -m "feat: define named verification checks"
```

---

### Task 2: Implement the strict same-fingerprint transaction

**Files:**
- Modify: `src/mini_code_agent/checks.py`
- Modify: `tests/test_checks.py`

**Interfaces:**
- Consumes: `VerificationCheck` and `VerificationCheckEvidence` from Task 1.
- Produces: `VerificationCheckExecution`, `VerificationMatrixResult`, and `run_verification_matrix(checks, capture_fingerprint=..., execute_check=...)`.
- Guarantees: all successful evidence refers to one unchanged fingerprint; ordinary failures continue, while safety/infrastructure failures stop.

- [ ] **Step 1: Add failing transaction tests**

Append to `tests/test_checks.py`:

```python
from mini_code_agent.checks import (
    VerificationCheckExecution,
    run_verification_matrix,
)


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
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_checks.py -q
```

Expected: import fails because `VerificationCheckExecution` and `run_verification_matrix` do not exist.

- [ ] **Step 3: Implement the pure transaction runner**

Change the existing typing import and add the utility import at the top of
`src/mini_code_agent/checks.py`, then append the runner definitions:

```python
from typing import Callable, Sequence

from mini_code_agent.utils import DEFAULT_OUTPUT_LIMIT, truncate_text


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
```

- [ ] **Step 4: Run pure runner tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_checks.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit the strict transaction**

```bash
git add src/mini_code_agent/checks.py tests/test_checks.py
git commit -m "feat: enforce one fingerprint across checks"
```

---

### Task 3: Integrate the matrix into `BashExecutor`

**Files:**
- Modify: `src/mini_code_agent/executor.py:124-226,620-743`
- Modify: `src/mini_code_agent/verification.py:180-280`
- Modify: `tests/test_executor.py:37-175`
- Modify: `tests/test_process_cleanup.py`
- Modify: `tests/test_agent_cli.py`
- Modify: `tests/test_hardening.py`

**Interfaces:**
- Consumes: all values and `run_verification_matrix` from Tasks 1–2.
- Produces: `BashExecutor(..., verification_checks: Sequence[VerificationCheck] = ())`.
- Produces: a trusted internal ignore-path handoff and a post-tool fingerprint
  equality check before the gate can mint verification.
- Preserves: the stable direct legacy `run_tests(default_command)` result/event
  surface, zero-test behavior, sandbox selection, and process cleanup; a
  legacy command that leaves a fingerprinted mutation is intentionally newly
  rejected.

- [ ] **Step 1: Add failing executor matrix tests**

Append to `tests/test_executor.py`:

```python
from mini_code_agent.checks import VerificationCheck


def test_matrix_runs_in_order_and_collects_ordinary_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executor = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        verification_checks=(
            VerificationCheck("tests", "tests command"),
            VerificationCheck("lint", "lint command"),
            VerificationCheck("types", "types command"),
        ),
    )
    calls: list[str] = []

    def fake_run(command: str) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 1 if command == "lint command" else 0, command
        )

    monkeypatch.setattr(executor, "_run", fake_run)

    result = executor.execute_tool("run_tests", {})

    assert calls == ["tests command", "lint command", "types command"]
    assert result.returncode == 1
    assert result.exception_info == "VerificationCheckFailed"
    assert result.command == "<verification matrix>"
    assert [item.name for item in result.verification_checks] == [
        "tests",
        "lint",
        "types",
    ]


def test_matrix_preflights_every_command_before_running_any(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executor = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        verification_checks=(
            VerificationCheck("tests", "safe command"),
            VerificationCheck("lint", "blocked command"),
        ),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        executor,
        "_blocked_command_reason",
        lambda command: "BlockedForTest" if command == "blocked command" else "",
    )
    monkeypatch.setattr(
        executor,
        "_run",
        lambda command: calls.append(command)
        or subprocess.CompletedProcess(command, 0, ""),
    )

    result = executor.execute_tool("run_tests", {})

    assert calls == []
    assert result.blocked is True
    assert result.exception_info == "BlockedForTest"


def test_matrix_uses_one_approval_for_all_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executor = BashExecutor(
        tmp_path,
        approval_mode="confirm",
        sandbox_mode="none",
        verification_checks=(
            VerificationCheck("tests", "tests command"),
            VerificationCheck("lint", "lint command"),
        ),
    )
    approvals: list[tuple[str, str]] = []
    monkeypatch.setattr(
        executor,
        "_confirm",
        lambda tool, detail: approvals.append((tool, detail)) or False,
    )

    result = executor.execute_tool("run_tests", {})

    assert len(approvals) == 1
    assert "tests:" in approvals[0][1]
    assert "lint:" in approvals[0][1]
    assert result.approved is False


def test_matrix_rejects_a_check_that_mutates_fingerprinted_workspace(
    tmp_path: Path,
):
    result = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        verification_checks=(
            VerificationCheck(
                "tests",
                (
                    f"{shlex.quote(sys.executable)} -c "
                    "\"from pathlib import Path; Path('generated.txt').write_text('x')\""
                ),
            ),
        ),
    ).execute_tool("run_tests", {})

    assert result.returncode == -1
    assert result.exception_info == "WorkspaceChangedDuringVerification"
    assert (tmp_path / "generated.txt").read_text(encoding="utf-8") == "x"


def test_legacy_test_command_rejects_a_persisted_mutation(tmp_path: Path):
    command = (
        f"{shlex.quote(sys.executable)} -c "
        + shlex.quote(
            "from pathlib import Path; "
            "Path('generated.txt').write_text('x'); "
            "print('legacy passed')"
        )
    )
    result = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        default_test_command=command,
    ).execute_tool("run_tests", {})

    assert result.returncode == -1
    assert result.exception_info == "WorkspaceChangedDuringVerification"
    assert result.verification_boundary_checked is True
    assert result.verification_checks == ()
    assert (tmp_path / "generated.txt").read_text(encoding="utf-8") == "x"


def test_matrix_observation_omits_commands_and_redacts_recognized_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    secret = "sk-testsecret123456"
    executor = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        verification_checks=(VerificationCheck("lint", f"echo {secret}"),),
    )
    monkeypatch.setattr(
        executor,
        "_run",
        lambda command: subprocess.CompletedProcess(command, 1, secret),
    )

    result = executor.execute_tool("run_tests", {})
    rendered = str(result.to_observation())

    assert secret not in rendered
    assert "echo" not in rendered
    assert "[REDACTED_SECRET]" in rendered
```

Add these imports and the stop-on-timeout test to
`tests/test_process_cleanup.py`:

```python
from mini_code_agent.checks import VerificationCheck
from mini_code_agent.contracts import ToolResult


def test_matrix_stops_before_the_next_check_after_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        verification_checks=(
            VerificationCheck("slow", "slow command"),
            VerificationCheck("later", "later command"),
        ),
    )
    calls: list[str] = []

    def fake_run_command(
        tool: str, command: str, *, args: dict
    ) -> ToolResult:
        calls.append(command)
        return ToolResult(
            tool=tool,
            output="timed out",
            returncode=-1,
            duration_ms=1,
            exception_info="TimeoutExpired",
        )

    monkeypatch.setattr(executor, "_run_command", fake_run_command)

    result = executor.execute_tool("run_tests", {})

    assert calls == ["slow command"]
    assert result.exception_info == "TimeoutExpired"
```

Add the end-to-end mutation/recovery test below to
`tests/test_agent_cli.py` before implementing executor support. It is RED
because the constructor does not yet accept `verification_checks`:

```python
from mini_code_agent.checks import VerificationCheck


class MatrixMutationRecoveryModel:
    def __init__(self):
        self.step = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.step += 1
        name, args = {
            1: ("run_tests", {}),
            2: (
                "apply_patch",
                {"path": "value.py", "old": "VALUE = 2", "new": "VALUE = 1"},
            ),
            3: ("run_tests", {}),
            4: ("submit", {"summary": "recovered and verified"}),
        }[self.step]
        return AIMessage(
            content=name,
            tool_calls=[
                {
                    "name": name,
                    "args": args,
                    "id": f"recover-{self.step}",
                    "type": "tool_call",
                }
            ],
        )


def test_matrix_mutation_is_refused_then_repaired_and_rerun(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    marker = tmp_path / "mutated-once"
    command = (
        f"{shlex.quote(sys.executable)} -c "
        + shlex.quote(
            "from pathlib import Path; "
            f"marker=Path({str(marker)!r}); "
            "value=Path('value.py'); "
            "first=not marker.exists(); "
            "value.write_text('VALUE = 2\\n') if first else None; "
            "marker.write_text('done') if first else None; "
            "print('Ran 1 test')"
        )
    )
    trajectory = MiniCodeAgent(
        MatrixMutationRecoveryModel(),
        BashExecutor(
            repo,
            approval_mode="yolo",
            sandbox_mode="none",
            verification_checks=(VerificationCheck("tests", command),),
        ),
        quiet=True,
    ).run("verify without accepting check mutations")

    test_events = [
        event
        for event in trajectory["events"]
        if event.get("tool") == "run_tests"
    ]
    assert test_events[0]["exception_info"] == (
        "WorkspaceChangedDuringVerification"
    )
    assert test_events[1]["returncode"] == 0
    assert trajectory["exit_status"] == "Submitted"
    assert (repo / "value.py").read_text(encoding="utf-8") == "VALUE = 1\n"
```

Add these imports and the ignore-path/handoff tests to
`tests/test_hardening.py`:

```python
import shlex
import sys

from mini_code_agent.checks import (
    VerificationCheck,
    VerificationCheckEvidence,
)
from mini_code_agent.contracts import ToolResult


def test_matrix_uses_the_same_trusted_ignore_paths_inside_executor_and_gate(
    tmp_path: Path,
):
    artifact = tmp_path / "run.json"
    command = (
        f"{shlex.quote(sys.executable)} -c "
        + shlex.quote(
            "from pathlib import Path; "
            "Path('run.json').write_text('runtime artifact'); "
            "print('check passed')"
        )
    )
    executor = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        verification_checks=(VerificationCheck("tests", command),),
    )
    baseline = capture_workspace_fingerprint(
        executor, ignore_paths={artifact}
    )
    gate = VerificationGate.create(baseline, require_verification=True)

    outcome = execute_tool_batch(
        executor,
        [{"name": "run_tests", "args": {}, "id": "checks"}],
        gate,
        ignore_paths={artifact},
    )
    result = outcome.calls[0].result

    assert artifact.read_text(encoding="utf-8") == "runtime artifact"
    assert result.returncode == 0
    assert result.verification_boundary_checked is True
    assert result.verification_fingerprint == baseline
    assert gate.status == "passed"


@pytest.mark.parametrize(
    ("matrix_evidence", "internal_fingerprint", "post_fingerprint"),
    [(True, "f0", "f1"), (True, "", "f0"), (False, "", "f0")],
    ids=("matrix-different", "matrix-missing", "legacy-missing"),
)
def test_verification_handoff_requires_matching_internal_fingerprint(
    tmp_path: Path,
    matrix_evidence: bool,
    internal_fingerprint: str,
    post_fingerprint: str,
):
    artifact = tmp_path / "run.json"

    class IdentityRedactor:
        def redact_text(self, value: str) -> str:
            return value

        def redact_data(self, value):
            return value

    class HandoffExecutor:
        cwd = tmp_path
        redactor = IdentityRedactor()

        def __init__(self):
            self.captures = 0
            self.received_args = None

        def workspace_fingerprint(self, *, ignore_paths=None):
            self.captures += 1
            return "f0" if self.captures == 1 else post_fingerprint

        def execute_tool(self, name: str, args: dict) -> ToolResult:
            self.received_args = args
            return ToolResult(
                tool=name,
                output="passed",
                returncode=0,
                duration_ms=1,
                verification_checks=(
                    (
                        VerificationCheckEvidence(
                            name="tests", returncode=0, duration_ms=1
                        ),
                    )
                    if matrix_evidence
                    else ()
                ),
                verification_boundary_checked=not matrix_evidence,
                verification_fingerprint=internal_fingerprint,
            )

        def sandbox_status(self) -> str:
            return "fake"

    executor = HandoffExecutor()
    gate = VerificationGate.create("f0", require_verification=True)
    outcome = execute_tool_batch(
        executor,
        [
            {
                "name": "run_tests",
                "args": {"_verification_ignore_paths": ["malicious"]},
                "id": "checks",
            },
            {
                "name": "submit",
                "args": {"summary": "must fail"},
                "id": "submit",
            },
        ],
        gate,
        ignore_paths={artifact},
    )
    results = {call.tool_call_id: call.result for call in outcome.calls}

    assert executor.received_args == {
        "_verification_ignore_paths": {artifact}
    }
    assert results["checks"].exception_info == (
        "WorkspaceChangedDuringVerification"
    )
    assert results["submit"].blocked is True
    assert gate.status == "failed"
```

- [ ] **Step 2: Run executor tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_executor.py \
  tests/test_process_cleanup.py \
  tests/test_agent_cli.py \
  tests/test_hardening.py -q
```

Expected: failures report that `BashExecutor.__init__()` does not accept `verification_checks`.

- [ ] **Step 3: Add constructor normalization and legacy/matrix routing**

Add imports to `src/mini_code_agent/executor.py`:

```python
from typing import Sequence

from mini_code_agent.checks import (
    VerificationCheck,
    VerificationCheckEvidence,
    VerificationCheckExecution,
    normalize_verification_checks,
    run_verification_matrix,
)
```

Extend the constructor:

```python
        verification_checks: Sequence[VerificationCheck] = (),
```

After validating `default_test_command`, normalize configuration:

```python
        explicit_checks = tuple(verification_checks)
        self.verification_checks = normalize_verification_checks(
            default_test_command, explicit_checks
        )
        self._strict_verification_matrix = bool(explicit_checks)
```

Replace the `execute_tool` dispatch for `run_tests` with the trusted internal
ignore-path handoff:

```python
                case "run_tests":
                    raw_ignore_paths = args.get("_verification_ignore_paths")
                    ignore_paths = (
                        raw_ignore_paths
                        if isinstance(raw_ignore_paths, set)
                        and all(
                            isinstance(path, Path)
                            for path in raw_ignore_paths
                        )
                        else None
                    )
                    return self.run_tests(
                        args.get("command"), ignore_paths=ignore_paths
                    )
```

Refactor `run_tests` so its first routing block is:

```python
    def run_tests(
        self,
        command: str | None = None,
        *,
        ignore_paths: set[Path] | None = None,
    ) -> ToolResult:
        if not self.verification_checks:
            return ToolResult(
                tool="run_tests",
                command="",
                output=(
                    "No authoritative verification check is configured. Restart "
                    "with --test-command '<command>' or --check NAME '<command>'."
                ),
                returncode=-1,
                duration_ms=0,
                exception_info="TestCommandRequired",
                blocked=True,
            )
        if command is not None:
            if not isinstance(command, str) or not command.strip():
                return ToolResult(
                    tool="run_tests",
                    command="",
                    output="The configured test command must not be blank.",
                    returncode=-1,
                    duration_ms=0,
                    exception_info="InvalidTestCommand",
                    blocked=True,
                )
            command = command.strip()
            legacy_match = (
                not self._strict_verification_matrix
                and self.default_test_command is not None
                and command == self.default_test_command
            )
            if not legacy_match:
                return ToolResult(
                    tool="run_tests",
                    command=self.redactor.redact_text(command),
                    output=(
                        "Custom verification commands are disabled. Call run_tests "
                        "without arguments and configure commands before startup."
                    ),
                    returncode=-1,
                    duration_ms=0,
                    exception_info="CustomTestCommandDisabled",
                    blocked=True,
                )
        if not self._strict_verification_matrix:
            return self._run_legacy_test(
                self.default_test_command or "",
                ignore_paths=ignore_paths or set(),
            )
        return self._run_test_matrix(ignore_paths=ignore_paths or set())
```

Add the legacy helper. Stable commands retain their existing public fields and
output; only persisted mutation and fingerprint-failure cases gain fail-closed
behavior. The attestation fields are internal and never serialized:

```python
    def _run_legacy_test(
        self, command: str, *, ignore_paths: set[Path]
    ) -> ToolResult:
        blocked_reason = self._blocked_command_reason(command)
        if blocked_reason:
            return ToolResult(
                tool="run_tests",
                command=self.redactor.redact_text(command),
                output="Test command was blocked by the safety policy.",
                returncode=-1,
                duration_ms=0,
                args={"command": self.redactor.redact_text(command)},
                exception_info=blocked_reason,
                blocked=True,
            )
        if self.approval_mode == "confirm" and not self._confirm(
            "run_tests", command
        ):
            return ToolResult(
                tool="run_tests",
                command=self.redactor.redact_text(command),
                output="Test command was rejected by the user.",
                returncode=-1,
                duration_ms=0,
                args={"command": self.redactor.redact_text(command)},
                exception_info="User rejected test command.",
                approved=False,
            )
        try:
            baseline = self.workspace_fingerprint(
                ignore_paths=ignore_paths
            ).fingerprint
        except Exception:
            return ToolResult(
                tool="run_tests",
                command=self.redactor.redact_text(command),
                output=(
                    "Workspace fingerprint capture failed before "
                    "verification."
                ),
                returncode=-1,
                duration_ms=0,
                args={"command": self.redactor.redact_text(command)},
                exception_info="WorkspaceFingerprintError",
            )
        result = self._apply_test_count(
            self._run_command(
                "run_tests", command, args={"command": command}
            )
        )
        try:
            after = self.workspace_fingerprint(
                ignore_paths=ignore_paths
            ).fingerprint
        except Exception:
            result.output = truncate_text(
                f"{result.output}\n\n"
                "Workspace fingerprint capture failed after verification.",
                DEFAULT_OUTPUT_LIMIT,
            )
            result.returncode = -1
            result.exception_info = "WorkspaceFingerprintError"
            return result
        result.verification_boundary_checked = True
        if after != baseline:
            result.output = truncate_text(
                f"{result.output}\n\n"
                "Test command changed the fingerprinted workspace; "
                "submission evidence was not minted.",
                DEFAULT_OUTPUT_LIMIT,
            )
            result.returncode = -1
            result.exception_info = (
                "WorkspaceChangedDuringVerification"
            )
            return result
        if result.returncode == 0:
            result.verification_fingerprint = baseline
        return result
```

Extract the shared count handling:

```python
    def _apply_test_count(self, result: ToolResult) -> ToolResult:
        unittest_match = UNITTEST_COUNT.search(result.output)
        if unittest_match:
            result.tests_run = int(unittest_match.group(1))
        elif PYTEST_ZERO.search(result.output):
            result.tests_run = 0
        if result.tests_run == 0 and result.returncode in {0, 5}:
            if self.allow_zero_tests:
                result.returncode = 0
            else:
                result.returncode = 1
                result.exception_info = "NoTestsCollected"
        return result
```

- [ ] **Step 4: Implement matrix preflight, approval, execution, and conversion**

Add to `BashExecutor`:

```python
    def _run_test_matrix(self, *, ignore_paths: set[Path]) -> ToolResult:
        for check in self.verification_checks:
            blocked_reason = self._blocked_command_reason(check.command)
            if blocked_reason:
                evidence = VerificationCheckEvidence(
                    name=check.name,
                    returncode=-1,
                    duration_ms=0,
                    exception_info=blocked_reason,
                    blocked=True,
                )
                return ToolResult(
                    tool="run_tests",
                    command="<verification matrix>",
                    output=f"Verification check {check.name} was blocked by policy.",
                    returncode=-1,
                    duration_ms=0,
                    exception_info=blocked_reason,
                    blocked=True,
                    verification_checks=(evidence,),
                )

        detail = "\n".join(
            f"{check.name}: {self.redactor.redact_text(check.command)}"
            for check in self.verification_checks
        )
        if self.approval_mode == "confirm" and not self._confirm(
            "run_tests", detail
        ):
            return ToolResult(
                tool="run_tests",
                command="<verification matrix>",
                output="Verification matrix was rejected by the user.",
                returncode=-1,
                duration_ms=0,
                exception_info="User rejected verification matrix.",
                approved=False,
            )

        def execute(check: VerificationCheck) -> VerificationCheckExecution:
            result = self._apply_test_count(
                self._run_command("run_tests", check.command, args={})
            )
            return VerificationCheckExecution(
                evidence=VerificationCheckEvidence(
                    name=check.name,
                    returncode=result.returncode,
                    duration_ms=result.duration_ms,
                    tests_run=result.tests_run,
                    exception_info=(
                        result.exception_info.partition(":")[0]
                        if result.exception_info
                        else ""
                    ),
                    blocked=result.blocked,
                    approved=result.approved,
                ),
                output=result.output,
            )

        matrix = run_verification_matrix(
            self.verification_checks,
            capture_fingerprint=lambda: self.workspace_fingerprint(
                ignore_paths=ignore_paths
            ).fingerprint,
            execute_check=execute,
        )
        return ToolResult(
            tool="run_tests",
            command="<verification matrix>",
            output=self.redactor.redact_text(matrix.output),
            returncode=matrix.returncode,
            duration_ms=sum(
                item.duration_ms for item in matrix.verification_checks
            ),
            exception_info=matrix.exception_info,
            approved=matrix.approved,
            blocked=matrix.blocked,
            verification_checks=matrix.verification_checks,
            verification_boundary_checked=matrix.returncode == 0,
            verification_fingerprint=matrix.verification_fingerprint,
        )
```

The matrix evidence stores only the stable exception class/code before the
first colon. Detailed redacted diagnostics remain in the bounded top-level
output and raw commands never enter per-check evidence.

- [ ] **Step 5: Close the executor-to-gate fingerprint handoff**

In `execute_tool_batch` inside `src/mini_code_agent/verification.py`, first
change `ignored = ignore_paths or set()` to make a defensive copy:

```python
    ignored = set(ignore_paths or set())
```

Then replace the existing `effective_args` assignment with:

```python
        effective_args = (
            {"_verification_ignore_paths": set(ignored)}
            if name == "run_tests"
            else args
        )
```

Immediately before `gate.record_test(...)`, add:

```python
        if name == "run_tests":
            if (
                result.returncode == 0
                and (
                    result.verification_boundary_checked
                    or bool(result.verification_checks)
                )
                and (
                    not result.verification_fingerprint
                    or result.verification_fingerprint
                    != current_fingerprint
                )
            ):
                result.returncode = -1
                result.exception_info = (
                    "WorkspaceChangedDuringVerification"
                )
                result.output = (
                    f"{result.output}\n\n"
                    "The verification fingerprint was missing or changed "
                    "at the executor handoff; submission evidence was not "
                    "minted."
                )
            gate.record_test(
                passed=result.returncode == 0,
                fingerprint=current_fingerprint,
            )
        else:
            gate.sync(current_fingerprint)
```

Replace the old `if name == "run_tests" ... else ...` block rather than adding
a second gate update. The internal ignore-path argument is never copied into
`ExecutedToolCall.args`, model messages, events, or trajectories.

- [ ] **Step 6: Run executor, cleanup, hardening, and legacy tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_executor.py \
  tests/test_process_cleanup.py \
  tests/test_agent_cli.py \
  tests/test_hardening.py \
  tests/test_architecture.py -q
```

Expected: all tests pass, including every pre-existing legacy `run_tests` test.

- [ ] **Step 7: Commit executor integration**

```bash
git add src/mini_code_agent/executor.py src/mini_code_agent/verification.py
git add tests/test_executor.py tests/test_process_cleanup.py tests/test_hardening.py
git add tests/test_agent_cli.py
git commit -m "feat: execute strict verification matrices"
```

---

### Task 4: Add repeatable CLI checks to run and chat

**Files:**
- Modify: `src/mini_code_agent/cli.py:1-105,266-550`
- Modify: `tests/test_cli_launch.py:36-137`
- Modify: `tests/test_hardening.py`

**Interfaces:**
- Consumes: `VerificationCheck` and `normalize_verification_checks`.
- Produces: repeatable `--check NAME COMMAND` for `run` and `chat`.
- Produces: `_configured_verification_checks(args, required=...) -> tuple[tuple[VerificationCheck, ...], tuple[VerificationCheck, ...]]`, returning combined and explicit sequences.

- [ ] **Step 1: Write failing parser and validation tests**

Add the named-check tests below to `tests/test_cli_launch.py`. Replace the
existing `test_run_requires_explicit_model_and_test_command` with
`test_run_requires_model_at_parse_time_and_verification_at_runtime` rather
than keeping both contracts:

```python
def test_parser_accepts_named_checks_and_preserves_order():
    args = cli_module.build_parser().parse_args(
        [
            "run",
            "task",
            "--model",
            "deepseek",
            "--check",
            "tests",
            "pytest -q",
            "--check",
            "lint",
            "ruff check .",
        ]
    )

    combined, explicit = cli_module._configured_verification_checks(
        args, required=True
    )

    assert [(item.name, item.command) for item in combined] == [
        ("tests", "pytest -q"),
        ("lint", "ruff check ."),
    ]
    assert explicit == combined


def test_cli_combines_legacy_test_first_and_rejects_duplicate_tests():
    parser = cli_module.build_parser()
    args = parser.parse_args(
        [
            "run",
            "task",
            "--model",
            "deepseek",
            "--test-command",
            "pytest -q",
            "--check",
            "lint",
            "ruff check .",
        ]
    )
    combined, explicit = cli_module._configured_verification_checks(
        args, required=True
    )
    assert [item.name for item in combined] == ["tests", "lint"]
    assert [item.name for item in explicit] == ["lint"]

    duplicate = parser.parse_args(
        [
            "run",
            "task",
            "--model",
            "deepseek",
            "--test-command",
            "pytest -q",
            "--check",
            "tests",
            "other",
        ]
    )
    with pytest.raises(ValueError, match="duplicate"):
        cli_module._configured_verification_checks(duplicate, required=True)


def test_run_requires_model_at_parse_time_and_verification_at_runtime():
    parser = cli_module.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "task", "--check", "tests", "pytest -q"])

    args = parser.parse_args(["run", "task", "--model", "deepseek"])
    with pytest.raises(RuntimeError, match="--check"):
        cli_module._configured_verification_checks(args, required=True)


def test_chat_named_check_enables_coding_configuration():
    args = cli_module.build_parser().parse_args(
        ["chat", "--check", "tests", "pytest -q"]
    )

    combined, explicit = cli_module._configured_verification_checks(
        args, required=False
    )

    assert combined
    assert explicit[0].name == "tests"
```

Add this lazy-loader plumbing test to `tests/test_hardening.py`:

```python
def test_run_agent_passes_named_checks_to_the_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    created: dict[str, object] = {}

    class FakeExecutor:
        def __init__(self, cwd: Path, **kwargs):
            self.cwd = Path(cwd)
            created["executor_kwargs"] = kwargs

    class FakeAgent:
        def __init__(self, model, executor, **kwargs):
            created["agent_executor"] = executor

        def run(self, task: str, *, resume_data=None):
            return {
                "exit_status": "Submitted",
                "workspace_changes": {
                    "created": [],
                    "deleted": [],
                    "modified": [],
                },
                "sandbox": "disabled",
                "submission": "",
                "events": [],
            }

    monkeypatch.setattr(cli_module, "_load_runtime_env", lambda _path: None)
    monkeypatch.setattr(cli_module, "_model_from_args", lambda _args: object())
    monkeypatch.setattr(
        cli_module, "_require_working_sandbox", lambda _executor: None
    )
    monkeypatch.setattr(
        cli_module,
        "_resume_output_path",
        lambda _resume, _output, _kind: tmp_path / "run.json",
    )
    monkeypatch.setattr(cli_module, "_load_bash_executor", lambda: FakeExecutor)
    monkeypatch.setattr(cli_module, "_load_mini_code_agent", lambda: FakeAgent)

    args = build_parser().parse_args(
        [
            "run",
            "task",
            "--cwd",
            str(tmp_path),
            "--model",
            "deepseek",
            "--check",
            "tests",
            "pytest -q",
            "--check",
            "lint",
            "ruff check .",
            "--yes",
            "--sandbox",
            "none",
            "--allow-dirty",
        ]
    )

    assert cli_module.run_agent(args) == 0
    kwargs = created["executor_kwargs"]
    assert kwargs["default_test_command"] is None
    assert [item.name for item in kwargs["verification_checks"]] == [
        "tests",
        "lint",
    ]


def test_chat_command_named_check_enables_code_and_sandbox_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    created: dict[str, object] = {}
    sandbox_probes: list[object] = []

    class FakeExecutor:
        def __init__(self, cwd: Path, **kwargs):
            self.cwd = Path(cwd)
            created["executor"] = self
            created["executor_kwargs"] = kwargs

    class FakeSession:
        def __init__(self, model, executor, **kwargs):
            created["access"] = executor

        def respond_turn(self, user_text: str, *, coding_mode: bool):
            created["turn"] = (user_text, coding_mode)
            return TurnResult(
                text="done",
                status="submitted",
                completed=True,
                verified=True,
                steps=1,
            )

        def close(self):
            created["closed"] = True

    class TtyInput:
        @staticmethod
        def isatty() -> bool:
            return True

    inputs = iter(["/code fix it", "/exit"])
    monkeypatch.setattr(cli_module.sys, "stdin", TtyInput())
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))
    monkeypatch.setattr(cli_module, "_load_runtime_env", lambda _path: None)
    monkeypatch.setattr(cli_module, "_model_from_args", lambda _args: object())
    monkeypatch.setattr(cli_module, "_load_bash_executor", lambda: FakeExecutor)
    monkeypatch.setattr(
        cli_module, "_load_conversational_code_agent", lambda: FakeSession
    )
    monkeypatch.setattr(
        cli_module,
        "_require_working_sandbox",
        sandbox_probes.append,
    )
    monkeypatch.setattr(
        cli_module,
        "_resume_output_path",
        lambda _resume, _output, _kind: tmp_path / "session.chat.json",
    )
    args = build_parser().parse_args(
        [
            "chat",
            "--cwd",
            str(tmp_path),
            "--model",
            "deepseek",
            "--check",
            "tests",
            "pytest -q",
            "--yes",
            "--sandbox",
            "none",
            "--allow-dirty",
        ]
    )

    assert cli_module.chat_command(args) == 0
    kwargs = created["executor_kwargs"]
    assert kwargs["default_test_command"] is None
    assert [item.name for item in kwargs["verification_checks"]] == ["tests"]
    assert sandbox_probes == [created["executor"]]
    assert created["turn"][1] is True
    assert created["access"]._coding_enabled is True
    assert created["closed"] is True
```

- [ ] **Step 2: Run CLI tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_cli_launch.py tests/test_hardening.py -q
```

Expected: argparse rejects `--check` and the helper is absent.

- [ ] **Step 3: Implement lightweight parsing and validation**

Add the lightweight imports to `src/mini_code_agent/cli.py`:

```python
from mini_code_agent.checks import (
    VerificationCheck,
    normalize_verification_checks,
)
```

Add the shared helper:

```python
def _configured_verification_checks(
    args: argparse.Namespace, *, required: bool
) -> tuple[tuple[VerificationCheck, ...], tuple[VerificationCheck, ...]]:
    explicit = tuple(
        VerificationCheck(name, command)
        for name, command in (getattr(args, "checks", None) or ())
    )
    combined = normalize_verification_checks(
        getattr(args, "test_command", None), explicit
    )
    if required and not combined:
        raise RuntimeError(
            "configure --test-command '<command>' or at least one "
            "--check NAME '<command>'"
        )
    return combined, explicit
```

For `run`, make `--test-command` optional and add:

```python
    run.add_argument(
        "--check",
        dest="checks",
        action="append",
        nargs=2,
        metavar=("NAME", "COMMAND"),
        default=[],
        help="Add a named authoritative verification check; repeatable.",
    )
```

For `chat`, retain the optional `--test-command` and add:

```python
    chat.add_argument(
        "--check",
        dest="checks",
        action="append",
        nargs=2,
        metavar=("NAME", "COMMAND"),
        default=[],
        help="Add a named authoritative verification check; repeatable.",
    )
```

- [ ] **Step 4: Plumb trusted checks before model and sandbox startup**

At the beginning of `run_agent`:

```python
    _combined_checks, explicit_checks = _configured_verification_checks(
        args, required=True
    )
```

Pass:

```python
        default_test_command=args.test_command,
        verification_checks=explicit_checks,
```

At the beginning of `chat_command`:

```python
    combined_checks, explicit_checks = _configured_verification_checks(
        args, required=False
    )
    coding_enabled = bool(combined_checks)
```

Use `coding_enabled` for sandbox startup, `ChatAccessController`, and the
`/code` guard. Pass the same executor arguments as `run_agent`. Replace
user-facing “test command” guidance with:

```text
restart with --test-command '<command>' or --check NAME '<command>'
```

- [ ] **Step 5: Run CLI, architecture, and chat access tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_cli_launch.py \
  tests/test_hardening.py \
  tests/test_architecture.py -q
```

Expected: all tests pass and help/informational commands still avoid importing
LangGraph or provider adapters.

- [ ] **Step 6: Commit CLI support**

```bash
git add src/mini_code_agent/cli.py tests/test_cli_launch.py tests/test_hardening.py
git commit -m "feat: configure named checks from the CLI"
```

---

### Task 5: Expose redacted matrix evidence through agent and chat trajectories

**Files:**
- Modify: `src/mini_code_agent/agent.py:269-315`
- Modify: `src/mini_code_agent/chat.py:294-312`
- Modify: `src/mini_code_agent/model.py:61-64`
- Modify: `src/mini_code_agent/prompts.py:1-50`
- Modify: `tests/test_agent_cli.py`
- Modify: `tests/test_hardening.py`

**Interfaces:**
- Consumes: additive `ToolResult.verification_checks`.
- Produces: `verification_checks` arrays in model observations, run events, and chat events.
- Preserves: top-level verification fields, checkpoint schema, legacy event fields, and argument-free tool calls.

- [ ] **Step 1: Add failing agent, same-batch, and event tests**

Add a scripted model and test to `tests/test_agent_cli.py`:

```python
class MatrixModel:
    def __init__(self):
        self.step = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.step += 1
        name, args = {
            1: ("run_tests", {}),
            2: ("submit", {"summary": "matrix verified"}),
        }[self.step]
        return AIMessage(
            content=name,
            tool_calls=[
                {
                    "name": name,
                    "args": args,
                    "id": f"matrix-{self.step}",
                    "type": "tool_call",
                }
            ],
        )


def test_agent_persists_redacted_matrix_evidence_and_submits(tmp_path: Path):
    trajectory_path = tmp_path / "run.json"
    tests_command = (
        f"{shlex.quote(sys.executable)} -c 'print(\"Ran 2 tests\")'"
    )
    lint_command = (
        f"{shlex.quote(sys.executable)} -c 'print(\"clean\")'"
    )
    agent = MiniCodeAgent(
        MatrixModel(),
        BashExecutor(
            tmp_path,
            approval_mode="yolo",
            sandbox_mode="none",
            verification_checks=(
                VerificationCheck("tests", tests_command),
                VerificationCheck("lint", lint_command),
            ),
        ),
        trajectory_path=trajectory_path,
        quiet=True,
    )

    trajectory = agent.run("verify")
    event = next(
        item for item in trajectory["events"] if item.get("tool") == "run_tests"
    )

    assert trajectory["exit_status"] == "Submitted"
    assert [item["name"] for item in event["verification_checks"]] == [
        "tests",
        "lint",
    ]
    rendered = trajectory_path.read_text(encoding="utf-8")
    assert tests_command not in rendered
    assert lint_command not in rendered
    assert "_verification_ignore_paths" not in rendered
    assert "verification_fingerprint" not in rendered


def test_chat_event_contains_only_redacted_matrix_evidence(tmp_path: Path):
    tests_command = (
        f"{shlex.quote(sys.executable)} -c 'print(\"Ran 2 tests\")'"
    )
    lint_command = (
        f"{shlex.quote(sys.executable)} -c 'print(\"clean\")'"
    )
    session = ConversationalCodeAgent(
        MatrixModel(),
        BashExecutor(
            tmp_path,
            approval_mode="yolo",
            sandbox_mode="none",
            verification_checks=(
                VerificationCheck("tests", tests_command),
                VerificationCheck("lint", lint_command),
            ),
        ),
        quiet=True,
    )

    result = session.respond_turn("verify", coding_mode=True)
    event = next(
        item for item in session.events if item.get("tool") == "run_tests"
    )

    assert result.status == "submitted"
    assert [item["name"] for item in event["verification_checks"]] == [
        "tests",
        "lint",
    ]
    assert tests_command not in str(event)
    assert lint_command not in str(event)
```

The `VerificationCheck`, `shlex`, and `sys` imports were added in Task 3. Add
the same-batch test to `tests/test_hardening.py`:

```python
def test_matrix_pass_then_edit_in_one_batch_blocks_submit(tmp_path: Path):
    (tmp_path / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    executor = BashExecutor(
        tmp_path,
        approval_mode="yolo",
        sandbox_mode="none",
        verification_checks=(
            VerificationCheck(
                "tests",
                f"{shlex.quote(sys.executable)} -c 'print(\"Ran 1 test\")'",
            ),
        ),
    )
    baseline = capture_workspace_fingerprint(executor)
    gate = VerificationGate.create(baseline, require_verification=True)

    outcome = execute_tool_batch(
        executor,
        [
            {"name": "run_tests", "args": {}, "id": "checks"},
            {
                "name": "apply_patch",
                "args": {
                    "path": "value.py",
                    "old": "VALUE = 1",
                    "new": "VALUE = 2",
                },
                "id": "edit",
            },
            {
                "name": "submit",
                "args": {"summary": "too early"},
                "id": "submit",
            },
        ],
        gate,
    )
    results = {call.tool_call_id: call.result for call in outcome.calls}

    assert results["checks"].returncode == 0
    assert results["edit"].returncode == 0
    assert results["submit"].blocked is True
    assert results["submit"].exception_info == "VerificationRequired"
```

Add the fresh-matrix-after-resume test:

```python
def test_resume_discards_prior_matrix_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MCA_STATE_DIR", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    trajectory_path = tmp_path / "resume.json"
    check = VerificationCheck(
        "tests",
        f"{shlex.quote(sys.executable)} -c 'print(\"Ran 1 test\")'",
    )

    class CheckOnceModel:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            return AIMessage(
                content="verify",
                tool_calls=[tool_call("run_tests", "initial-check")],
            )

    class CheckThenSubmitModel:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(
                    content="verify again",
                    tool_calls=[tool_call("run_tests", "fresh-check")],
                )
            return AIMessage(
                content="submit",
                tool_calls=[tool_call("submit", "submit")],
            )

    first = MiniCodeAgent(
        CheckOnceModel(),
        BashExecutor(
            repo,
            approval_mode="yolo",
            sandbox_mode="none",
            verification_checks=(check,),
        ),
        max_steps=1,
        trajectory_path=trajectory_path,
        quiet=True,
    ).run("verify")
    checkpoint_steps = first["steps"]

    resumed = MiniCodeAgent(
        CheckThenSubmitModel(),
        BashExecutor(
            repo,
            approval_mode="yolo",
            sandbox_mode="none",
            verification_checks=(check,),
        ),
        max_steps=3,
        trajectory_path=trajectory_path,
        quiet=True,
    ).run(resume_data=load_trajectory(trajectory_path))
    fresh_checks = [
        event
        for event in resumed["events"]
        if event.get("tool") == "run_tests"
        and int(event.get("step", 0)) > checkpoint_steps
    ]

    assert len(fresh_checks) == 1
    assert resumed["exit_status"] == "Submitted"
```

- [ ] **Step 2: Run lifecycle tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_agent_cli.py tests/test_hardening.py -q
```

Expected: matrix execution can pass, but trajectory events lack
`verification_checks`.

- [ ] **Step 3: Add evidence to run and chat events**

In `MiniCodeAgent._run_tools`, after the existing `tests_run` block:

```python
            if result.verification_checks:
                event["verification_checks"] = [
                    item.to_dict() for item in result.verification_checks
                ]
```

In `ConversationalCodeAgent._tool_event`, add this block before redacting and
returning the event:

```python
        if result.verification_checks:
            event["verification_checks"] = [
                item.to_dict() for item in result.verification_checks
            ]
```

No checkpoint schema field is added; evidence remains inside the existing
bounded event list.

- [ ] **Step 4: Update the model tool and prompts without changing its schema**

Change the `run_tests` tool docstring in `model.py` to:

```python
    """Run every user-configured authoritative verification check."""
```

Replace prompt references to a singular command with this exact policy:

```text
- Prefer run_tests for configured tests, lint, type, and policy checks.
- Never invent, select, skip, reorder, or override a verification command.
  run_tests always executes the complete matrix configured by the user.
- A passing matrix is valid only when every check begins and ends with the same
  workspace fingerprint. Any later file change requires another complete
  run_tests call.
```

- [ ] **Step 5: Run agent, chat, context, and benchmark regression tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_agent_cli.py \
  tests/test_hardening.py \
  tests/test_evals.py \
  tests/test_architecture.py -q
.venv/bin/python -m evals.run_evals --json
```

Expected: pytest passes; the published benchmark remains schema 2, suite
`verified-patch-v0.3.2`, 11/11 passing, and its exact 50-call oracle is
unchanged.

- [ ] **Step 6: Commit trajectory and prompt integration**

```bash
git add \
  src/mini_code_agent/agent.py \
  src/mini_code_agent/chat.py \
  src/mini_code_agent/model.py \
  src/mini_code_agent/prompts.py \
  tests/test_agent_cli.py \
  tests/test_hardening.py
git commit -m "feat: record verification matrix evidence"
```

---

### Task 6: Publish versioned user and security documentation

**Files:**
- Modify: `tests/test_cli_launch.py:176-186`
- Modify: `src/mini_code_agent/__init__.py`
- Modify: `pyproject.toml:5-8`
- Modify: `README.md:45-120`
- Modify: `README.zh-CN.md`
- Modify: `SECURITY.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: the complete PR1 runtime behavior.
- Produces: version `0.3.4`, user-facing CLI examples, and strict mutation
  guidance. The end-to-end recovery test was written RED in Task 3 and remains
  outside the v0.3.2 benchmark.

- [ ] **Step 1: Change the version assertion and verify RED**

Change the existing assertion in `tests/test_cli_launch.py` to:

```python
assert result.stdout.strip() == "mca 0.3.4"
```

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_cli_launch.py::test_cli_reports_package_version_without_a_subcommand -q
```

Expected: FAIL because the current package reports `mca 0.3.3`.

- [ ] **Step 2: Bump the package development version**

Set both:

```toml
version = "0.3.4"
```

and:

```python
__version__ = "0.3.4"
```

- [ ] **Step 3: Document the exact user and security contract**

Add this primary example to both READMEs, translated naturally in the Chinese
version:

```bash
mca run "Fix the issue" \
  --model deepseek \
  --check tests "pytest -q" \
  --check lint "ruff check ." \
  --check types "pyright"
```

Add these exact English claims to `README.md` and `SECURITY.md`, and add a
faithful Chinese translation of the same contract to `README.zh-CN.md`:

```text
Named checks run serially and must all begin and end with one unchanged
workspace fingerprint. A check that leaves a fingerprinted file changed
invalidates the entire matrix with WorkspaceChangedDuringVerification; run
generators before the matrix. Ignored cache paths retain the existing
fingerprint policy.

--test-command remains the backward-compatible single-check form. Configure at
most 16 checks. Worst-case matrix time is approximately the number of checks
multiplied by the per-command timeout.

Stable --test-command output and event fields remain compatible, but the
single legacy command now also fails closed if it leaves a fingerprinted file
changed. Use ignored cache paths only through the existing trusted runtime
artifact policy.

This evidence shows that the configured commands passed under the runtime
policy for one workspace state. It does not prove test completeness, code
correctness, model quality, or overall system safety.

Fingerprint capture occurs at check boundaries. It detects persisted changes
but cannot prove that a command did not modify and restore a file entirely
between captures; this feature does not claim immutable-snapshot execution.

Matrix configuration commands are not directly serialized into structured
evidence and output is bounded. Redaction is best effort for known patterns,
environment values, and values configured through the existing redaction
controls; arbitrary command output can echo command text or values that cannot
be classified perfectly. Treat trajectory files as sensitive and do not
publish them without review.
```

Add a `0.3.4` changelog entry dated `2026-07-22` covering named checks,
strict mutation refusal, additive redacted evidence, compatibility, tests, and
the unchanged TrustBench boundary.

- [ ] **Step 4: Run documentation, version, end-to-end, and hygiene tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_agent_cli.py \
  tests/test_cli_launch.py \
  tests/test_hygiene_scan.py \
  tests/test_workflow_policy.py -q
.venv/bin/mca --version
```

Expected: tests pass and `mca --version` prints `mca 0.3.4`.

- [ ] **Step 5: Commit the release documentation**

```bash
git add \
  tests/test_cli_launch.py \
  src/mini_code_agent/__init__.py \
  pyproject.toml \
  README.md \
  README.zh-CN.md \
  SECURITY.md \
  CHANGELOG.md
git commit -m "docs: release strict multi-check verification"
```

---

### Task 7: Run the complete verification gate

**Files:**
- Verify only; no planned source edits.

**Interfaces:**
- Consumes: all six implementation commits.
- Produces: local evidence required before push or PR creation.

- [ ] **Step 1: Run the complete test suite**

```bash
.venv/bin/python -m pytest -q
```

Expected: all tests pass with only the existing platform-conditional skip.

- [ ] **Step 2: Reproduce the published policy benchmark**

```bash
EVAL_OUT="$(mktemp -d)"
.venv/bin/python -m evals.run_evals --json \
  --output "$EVAL_OUT/v0.3.2.json"
```

Expected: schema 2, suite `verified-patch-v0.3.2`, 11/11 passing, zero
unexpected submissions, and zero unrelated changes.

- [ ] **Step 3: Run package, dependency, CLI, and demo verification**

```bash
BUILD_OUT="$(mktemp -d)"
.venv/bin/python -m build --sdist --wheel --outdir "$BUILD_OUT"
.venv/bin/python -m twine check "$BUILD_OUT"/*
.venv/bin/python -m pip check
.venv/bin/mca --version
.venv/bin/mca doctor --sandbox none
.venv/bin/mca demo
```

Expected: build and Twine checks pass, pip reports no broken requirements,
version is `0.3.4`, doctor has only expected configuration/isolation warnings,
and the no-key demo submits a verified patch.

- [ ] **Step 4: Run real sandbox capability verification available on the host**

On macOS:

```bash
.venv/bin/mca sandbox probe --sandbox sandbox-exec
```

On Linux, run every installed backend:

```bash
.venv/bin/mca sandbox probe --sandbox bwrap
.venv/bin/mca sandbox probe --sandbox docker
```

Expected: every selected backend reports PASS for workspace write and explicit
denial evidence for outside write, network, and socket checks. Unavailable
backends are reported as unavailable rather than treated as passing.

- [ ] **Step 5: Verify the branch is clean and review the final diff**

```bash
git status --short
git diff --check codex/v0.3.3-sandbox-hardening...HEAD
git log --oneline codex/v0.3.3-sandbox-hardening..HEAD
```

Expected: clean status, no whitespace errors, one design commit, one plan
commit, six implementation/documentation commits, and no TrustBench or
Windows-runtime files.
