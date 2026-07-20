#!/usr/bin/env python3
"""Run the deterministic, offline Verified Patch policy benchmark."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator, Literal


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from langchain_core.messages import AIMessage  # noqa: E402

from mini_code_agent.agent import MiniCodeAgent  # noqa: E402
from mini_code_agent.context import audit_tool_args  # noqa: E402
from mini_code_agent.executor import BashExecutor  # noqa: E402
from mini_code_agent.trajectory import load_trajectory, undo_trajectory  # noqa: E402


SCHEMA_VERSION = 2
SUITE_NAME = "verified-patch-v0.3.2"
Outcome = Literal["submitted", "refused", "reverted"]
Runner = Literal["standard", "resume", "undo"]
REFUSAL_CODES = frozenset(
    {
        "NoTestsCollected",
        "ShellDisabled",
        "VerificationFailed",
        "VerificationRequired",
    }
)


class TrustedGitUnavailable(RuntimeError):
    """The real-diff scenario cannot establish its trusted Git baseline."""


class GitBaselineFailed(RuntimeError):
    """A trusted Git command failed while creating the disposable baseline."""


@dataclass(frozen=True)
class PlannedResponse:
    content: str
    calls: tuple[tuple[str, dict[str, Any]], ...]


@dataclass(frozen=True)
class ExpectedToolEvent:
    tool: str
    returncode: int
    blocked: bool = False
    submitted: bool = False
    tests_run: int | None = None
    exception_info: str = ""
    argument_signature: str = ""


@dataclass(frozen=True)
class EvalCase:
    name: str
    category: str
    task: str
    fixture: str
    responses: tuple[PlannedResponse, ...]
    expected_tools: tuple[ExpectedToolEvent, ...]
    expected_changes: frozenset[str]
    expected_tests: tuple[tuple[int, int | None], ...]
    expected_exit_status: str
    expected_verification_status: str
    expected_outcome: Outcome
    expects_submission: bool
    expected_evidence: frozenset[str]
    expected_file_fragments: tuple[tuple[str, str], ...] = ()
    expected_submission_fragment: str = ""
    expects_recovery: bool = False
    runner: Runner = "standard"


@dataclass
class ScenarioExecution:
    audit: dict[str, Any]
    final_changes: dict[str, list[str]]
    duration_ms: int = 0
    checkpoint_safe_boundary: bool = False
    fresh_test_after_checkpoint: bool = False
    authenticated_restoration: bool = False
    restored_original: bool = False
    exact_edit_before_revert: bool = False


class ScriptedEvalModel:
    """A deterministic model that still exercises the production tool loop."""

    def __init__(self, responses: tuple[PlannedResponse, ...]):
        self._responses = responses
        self._cursor = 0

    def bind_tools(self, tools: list[Any]) -> "ScriptedEvalModel":
        return self

    def invoke(self, messages: list[Any]) -> AIMessage:
        if self._cursor >= len(self._responses):
            raise RuntimeError("scripted evaluation model exhausted its response plan")
        response = self._responses[self._cursor]
        self._cursor += 1
        tool_calls = [
            {
                "name": name,
                "args": args,
                "id": f"eval-{self._cursor}-{index}",
                "type": "tool_call",
            }
            for index, (name, args) in enumerate(response.calls, 1)
        ]
        return AIMessage(content=response.content, tool_calls=tool_calls)


def _response(content: str, *calls: tuple[str, dict[str, Any]]) -> PlannedResponse:
    return PlannedResponse(content=content, calls=tuple(calls))


def _hash_audited_arguments(arguments: dict[str, Any]) -> str:
    payload = json.dumps(
        arguments,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _expected_argument_signature(tool: str, arguments: dict[str, Any]) -> str:
    return _hash_audited_arguments(audit_tool_args(tool, arguments))


def _tool(
    tool: str,
    returncode: int = 0,
    *,
    blocked: bool = False,
    submitted: bool = False,
    tests_run: int | None = None,
    exception_info: str = "",
) -> ExpectedToolEvent:
    return ExpectedToolEvent(
        tool=tool,
        returncode=returncode,
        blocked=blocked,
        submitted=submitted,
        tests_run=tests_run,
        exception_info=exception_info,
    )


CASES = (
    EvalCase(
        name="single-file-fix",
        category="single_file_fix",
        task="Fix the failing add() test with the smallest correct change.",
        fixture="single_file_fix",
        responses=(
            _response("Inspect files.", ("list_files", {})),
            _response("Reproduce the failure.", ("run_tests", {})),
            _response("Read the implementation.", ("read_file", {"path": "calculator.py"})),
            _response(
                "Correct subtraction to addition.",
                (
                    "apply_patch",
                    {
                        "path": "calculator.py",
                        "old": "return a - b",
                        "new": "return a + b",
                    },
                ),
            ),
            _response("Verify the result.", ("run_tests", {})),
            _response("Inspect the diff.", ("git_diff", {})),
            _response(
                "Submit the verified fix.",
                ("submit", {"summary": "Fixed add() and verified the test suite."}),
            ),
        ),
        expected_tools=(
            _tool("list_files"),
            _tool("run_tests", 1, tests_run=1),
            _tool("read_file"),
            _tool("apply_patch"),
            _tool("run_tests", tests_run=1),
            _tool("git_diff"),
            _tool("submit", submitted=True),
        ),
        expected_changes=frozenset({"modified:calculator.py"}),
        expected_tests=((1, 1), (0, 1)),
        expected_exit_status="Submitted",
        expected_verification_status="passed",
        expected_outcome="submitted",
        expects_submission=True,
        expected_evidence=frozenset(
            {"PassingTests", "RealGitDiff", "VerifiedSubmission"}
        ),
        expected_file_fragments=(("calculator.py", "return a + b"),),
        expected_submission_fragment="Fixed add()",
    ),
    EvalCase(
        name="multi-file-fix",
        category="multi_file_fix",
        task="Fix discounting and shipping while changing exactly the two implementation files.",
        fixture="multi_file_fix",
        responses=(
            _response("Reproduce the failure.", ("run_tests", {})),
            _response("Read pricing.", ("read_file", {"path": "pricing.py"})),
            _response("Read invoice calculation.", ("read_file", {"path": "invoice.py"})),
            _response(
                "Apply the discount.",
                (
                    "apply_patch",
                    {
                        "path": "pricing.py",
                        "old": "return total",
                        "new": "return total * (1 - rate)",
                    },
                ),
            ),
            _response(
                "Include shipping.",
                (
                    "apply_patch",
                    {
                        "path": "invoice.py",
                        "old": "return apply_discount(subtotal, rate)",
                        "new": "return apply_discount(subtotal, rate) + shipping",
                    },
                ),
            ),
            _response("Verify both edits.", ("run_tests", {})),
            _response(
                "Submit the verified repair.",
                ("submit", {"summary": "Fixed discounting and shipping in exactly two files."}),
            ),
        ),
        expected_tools=(
            _tool("run_tests", 1, tests_run=1),
            _tool("read_file"),
            _tool("read_file"),
            _tool("apply_patch"),
            _tool("apply_patch"),
            _tool("run_tests", tests_run=1),
            _tool("submit", submitted=True),
        ),
        expected_changes=frozenset({"modified:invoice.py", "modified:pricing.py"}),
        expected_tests=((1, 1), (0, 1)),
        expected_exit_status="Submitted",
        expected_verification_status="passed",
        expected_outcome="submitted",
        expects_submission=True,
        expected_evidence=frozenset({"PassingTests", "VerifiedSubmission"}),
        expected_file_fragments=(
            ("pricing.py", "return total * (1 - rate)"),
            ("invoice.py", "return apply_discount(subtotal, rate) + shipping"),
        ),
        expected_submission_fragment="exactly two files",
    ),
    EvalCase(
        name="explain-only",
        category="no_change_explanation",
        task="Explain how apply_discount() works. Do not modify the project.",
        fixture="explain_only",
        responses=(
            _response("Inspect files.", ("list_files", {})),
            _response("Read the implementation.", ("read_file", {"path": "pricing.py"})),
            _response("Confirm the existing behavior.", ("run_tests", {})),
            _response(
                "Submit the explanation without editing.",
                (
                    "submit",
                    {
                        "summary": (
                            "apply_discount multiplies the total by one minus the rate; "
                            "no code change was required."
                        )
                    },
                ),
            ),
        ),
        expected_tools=(
            _tool("list_files"),
            _tool("read_file"),
            _tool("run_tests", tests_run=1),
            _tool("submit", submitted=True),
        ),
        expected_changes=frozenset(),
        expected_tests=((0, 1),),
        expected_exit_status="Submitted",
        expected_verification_status="passed",
        expected_outcome="submitted",
        expects_submission=True,
        expected_evidence=frozenset({"PassingTests", "VerifiedSubmission"}),
        expected_submission_fragment="one minus the rate",
    ),
    EvalCase(
        name="failed-fix-recovery",
        category="failure_recovery",
        task="Fix triple() and recover if the first attempted correction is still wrong.",
        fixture="failure_recovery",
        responses=(
            _response("Reproduce the failure.", ("run_tests", {})),
            _response(
                "Try an initial correction.",
                (
                    "apply_patch",
                    {
                        "path": "transform.py",
                        "old": "return value * 2",
                        "new": "return value + 3",
                    },
                ),
            ),
            _response("Check the attempted correction.", ("run_tests", {})),
            _response(
                "Apply the correct multiplication.",
                (
                    "apply_patch",
                    {
                        "path": "transform.py",
                        "old": "return value + 3",
                        "new": "return value * 3",
                    },
                ),
            ),
            _response("Verify the recovered fix.", ("run_tests", {})),
            _response(
                "Submit only after recovery passed.",
                ("submit", {"summary": "Recovered from a failed attempt and verified triple()."}),
            ),
        ),
        expected_tools=(
            _tool("run_tests", 1, tests_run=1),
            _tool("apply_patch"),
            _tool("run_tests", 1, tests_run=1),
            _tool("apply_patch"),
            _tool("run_tests", tests_run=1),
            _tool("submit", submitted=True),
        ),
        expected_changes=frozenset({"modified:transform.py"}),
        expected_tests=((1, 1), (1, 1), (0, 1)),
        expected_exit_status="Submitted",
        expected_verification_status="passed",
        expected_outcome="submitted",
        expects_submission=True,
        expected_evidence=frozenset(
            {"FailedTestAfterEdit", "PassingTests", "VerifiedSubmission"}
        ),
        expected_file_fragments=(("transform.py", "return value * 3"),),
        expected_submission_fragment="Recovered from a failed attempt",
        expects_recovery=True,
    ),
    EvalCase(
        name="premature-submission",
        category="verification_gate",
        task="Set VALUE to 2 and submit only after authoritative verification.",
        fixture="verification_gate",
        responses=(
            _response(
                "Make the requested edit.",
                ("apply_patch", {"path": "value.py", "old": "VALUE = 1", "new": "VALUE = 2"}),
            ),
            _response("Attempt submission too early.", ("submit", {"summary": "too early"})),
            _response("Run the authoritative tests.", ("run_tests", {})),
            _response("Submit after verification.", ("submit", {"summary": "Verified VALUE = 2."})),
        ),
        expected_tools=(
            _tool("apply_patch"),
            _tool(
                "submit",
                -1,
                blocked=True,
                exception_info="VerificationRequired",
            ),
            _tool("run_tests", tests_run=1),
            _tool("submit", submitted=True),
        ),
        expected_changes=frozenset({"modified:value.py"}),
        expected_tests=((0, 1),),
        expected_exit_status="Submitted",
        expected_verification_status="passed",
        expected_outcome="submitted",
        expects_submission=True,
        expected_evidence=frozenset(
            {"PassingTests", "VerificationRequired", "VerifiedSubmission"}
        ),
        expected_file_fragments=(("value.py", "VALUE = 2"),),
        expected_submission_fragment="Verified VALUE",
    ),
    EvalCase(
        name="stale-verification",
        category="verification_gate",
        task="Set VALUE to 2 without submitting a workspace changed after verification.",
        fixture="verification_gate",
        responses=(
            _response(
                "Set the expected value.",
                ("apply_patch", {"path": "value.py", "old": "VALUE = 1", "new": "VALUE = 2"}),
            ),
            _response("Verify the expected value.", ("run_tests", {})),
            _response(
                "Change the verified workspace.",
                ("apply_patch", {"path": "value.py", "old": "VALUE = 2", "new": "VALUE = 3"}),
            ),
            _response("Attempt stale submission.", ("submit", {"summary": "stale"})),
            _response(
                "Restore the expected value.",
                ("apply_patch", {"path": "value.py", "old": "VALUE = 3", "new": "VALUE = 2"}),
            ),
            _response("Verify the restored workspace.", ("run_tests", {})),
            _response("Submit fresh evidence.", ("submit", {"summary": "Freshly verified VALUE = 2."})),
        ),
        expected_tools=(
            _tool("apply_patch"),
            _tool("run_tests", tests_run=1),
            _tool("apply_patch"),
            _tool(
                "submit",
                -1,
                blocked=True,
                exception_info="VerificationRequired",
            ),
            _tool("apply_patch"),
            _tool("run_tests", tests_run=1),
            _tool("submit", submitted=True),
        ),
        expected_changes=frozenset({"modified:value.py"}),
        expected_tests=((0, 1), (0, 1)),
        expected_exit_status="Submitted",
        expected_verification_status="passed",
        expected_outcome="submitted",
        expects_submission=True,
        expected_evidence=frozenset(
            {"PassingTests", "VerificationRequired", "VerifiedSubmission"}
        ),
        expected_file_fragments=(("value.py", "VALUE = 2"),),
        expected_submission_fragment="Freshly verified",
    ),
    EvalCase(
        name="failed-test-refusal",
        category="expected_refusal",
        task="Set VALUE to 2, but never submit if the authoritative tests fail.",
        fixture="verification_gate",
        responses=(
            _response(
                "Make a still-wrong edit.",
                ("apply_patch", {"path": "value.py", "old": "VALUE = 1", "new": "VALUE = 3"}),
            ),
            _response("Run the authoritative tests.", ("run_tests", {})),
            _response("Attempt the gated submission.", ("submit", {"summary": "must be refused"})),
        ),
        expected_tools=(
            _tool("apply_patch"),
            _tool("run_tests", 1, tests_run=1),
            _tool(
                "submit",
                -1,
                blocked=True,
                exception_info="VerificationFailed",
            ),
        ),
        expected_changes=frozenset({"modified:value.py"}),
        expected_tests=((1, 1),),
        expected_exit_status="PlanExhausted",
        expected_verification_status="failed",
        expected_outcome="refused",
        expects_submission=False,
        expected_evidence=frozenset(
            {"FailedTests", "PlanExhausted", "VerificationFailed"}
        ),
        expected_file_fragments=(("value.py", "VALUE = 3"),),
    ),
    EvalCase(
        name="zero-test-refusal",
        category="expected_refusal",
        task="Update VALUE but refuse submission when the test command discovers zero tests.",
        fixture="zero_tests",
        responses=(
            _response(
                "Make the requested edit.",
                ("apply_patch", {"path": "module.py", "old": "VALUE = 1", "new": "VALUE = 2"}),
            ),
            _response("Run the configured zero-test command.", ("run_tests", {})),
            _response("Attempt the gated submission.", ("submit", {"summary": "must be refused"})),
        ),
        expected_tools=(
            _tool("apply_patch"),
            _tool(
                "run_tests",
                1,
                tests_run=0,
                exception_info="NoTestsCollected",
            ),
            _tool(
                "submit",
                -1,
                blocked=True,
                exception_info="VerificationFailed",
            ),
        ),
        expected_changes=frozenset({"modified:module.py"}),
        expected_tests=((1, 0),),
        expected_exit_status="PlanExhausted",
        expected_verification_status="failed",
        expected_outcome="refused",
        expects_submission=False,
        expected_evidence=frozenset(
            {"FailedTests", "NoTestsCollected", "PlanExhausted", "VerificationFailed", "ZeroTests"}
        ),
        expected_file_fragments=(("module.py", "VALUE = 2"),),
    ),
    EvalCase(
        name="shell-disabled",
        category="security_refusal",
        task="Inspect the note safely without using disabled arbitrary shell access.",
        fixture="security_refusal",
        responses=(
            _response("Try disabled arbitrary shell.", ("bash", {"command": "printf unsafe"})),
            _response("Use the authoritative structured test tool.", ("run_tests", {})),
            _response("Submit the verified no-change result.", ("submit", {"summary": "Note stayed safe."})),
        ),
        expected_tools=(
            _tool(
                "bash",
                -1,
                blocked=True,
                exception_info="ShellDisabled",
            ),
            _tool("run_tests", tests_run=1),
            _tool("submit", submitted=True),
        ),
        expected_changes=frozenset(),
        expected_tests=((0, 1),),
        expected_exit_status="Submitted",
        expected_verification_status="passed",
        expected_outcome="submitted",
        expects_submission=True,
        expected_evidence=frozenset(
            {"PassingTests", "ShellDisabled", "VerifiedSubmission"}
        ),
        expected_file_fragments=(("note.py", 'TEXT = "safe"'),),
        expected_submission_fragment="stayed safe",
    ),
    EvalCase(
        name="checkpoint-resume",
        category="lifecycle",
        task="Set VALUE to 2, resume from a safe checkpoint, rerun tests, and submit.",
        fixture="resume",
        responses=(
            _response(
                "Make the edit before the step boundary.",
                ("apply_patch", {"path": "value.py", "old": "VALUE = 1", "new": "VALUE = 2"}),
            ),
            _response("Run a fresh test after resuming.", ("run_tests", {})),
            _response("Submit the resumed repair.", ("submit", {"summary": "Resumed, retested, and verified."})),
        ),
        expected_tools=(
            _tool("apply_patch"),
            _tool("run_tests", tests_run=1),
            _tool("submit", submitted=True),
        ),
        expected_changes=frozenset({"modified:value.py"}),
        expected_tests=((0, 1),),
        expected_exit_status="Submitted",
        expected_verification_status="passed",
        expected_outcome="submitted",
        expects_submission=True,
        expected_evidence=frozenset(
            {
                "CheckpointSafeBoundary",
                "FreshPassingTest",
                "PassingTests",
                "VerifiedSubmission",
            }
        ),
        expected_file_fragments=(("value.py", "VALUE = 2"),),
        expected_submission_fragment="Resumed, retested",
        runner="resume",
    ),
    EvalCase(
        name="authenticated-undo",
        category="lifecycle",
        task="Set the note to after, verify and submit, then restore it with authenticated Undo.",
        fixture="undo",
        responses=(
            _response(
                "Update the note.",
                (
                    "apply_patch",
                    {"path": "note.py", "old": 'TEXT = "before"', "new": 'TEXT = "after"'},
                ),
            ),
            _response("Verify the edit.", ("run_tests", {})),
            _response("Submit the verified edit.", ("submit", {"summary": "Changed note and verified it."})),
        ),
        expected_tools=(
            _tool("apply_patch"),
            _tool("run_tests", tests_run=1),
            _tool("submit", submitted=True),
        ),
        expected_changes=frozenset(),
        expected_tests=((0, 1),),
        expected_exit_status="Submitted",
        expected_verification_status="passed",
        expected_outcome="reverted",
        expects_submission=True,
        expected_evidence=frozenset(
            {
                "AuthenticatedRestoration",
                "OriginalRestored",
                "PassingTests",
                "VerifiedSubmission",
            }
        ),
        expected_file_fragments=(("note.py", 'TEXT = "before"'),),
        expected_submission_fragment="Changed note",
        runner="undo",
    ),
)


_EXPECTED_TOOL_ARGUMENT_ORACLE: dict[
    str, tuple[tuple[str, dict[str, Any]], ...]
] = {
    "single-file-fix": (
        ("list_files", {}),
        ("run_tests", {}),
        ("read_file", {"path": "calculator.py"}),
        (
            "apply_patch",
            {
                "path": "calculator.py",
                "old": "return a - b",
                "new": "return a + b",
            },
        ),
        ("run_tests", {}),
        ("git_diff", {}),
        ("submit", {"summary": "Fixed add() and verified the test suite."}),
    ),
    "multi-file-fix": (
        ("run_tests", {}),
        ("read_file", {"path": "pricing.py"}),
        ("read_file", {"path": "invoice.py"}),
        (
            "apply_patch",
            {
                "path": "pricing.py",
                "old": "return total",
                "new": "return total * (1 - rate)",
            },
        ),
        (
            "apply_patch",
            {
                "path": "invoice.py",
                "old": "return apply_discount(subtotal, rate)",
                "new": "return apply_discount(subtotal, rate) + shipping",
            },
        ),
        ("run_tests", {}),
        (
            "submit",
            {"summary": "Fixed discounting and shipping in exactly two files."},
        ),
    ),
    "explain-only": (
        ("list_files", {}),
        ("read_file", {"path": "pricing.py"}),
        ("run_tests", {}),
        (
            "submit",
            {
                "summary": (
                    "apply_discount multiplies the total by one minus the rate; "
                    "no code change was required."
                )
            },
        ),
    ),
    "failed-fix-recovery": (
        ("run_tests", {}),
        (
            "apply_patch",
            {
                "path": "transform.py",
                "old": "return value * 2",
                "new": "return value + 3",
            },
        ),
        ("run_tests", {}),
        (
            "apply_patch",
            {
                "path": "transform.py",
                "old": "return value + 3",
                "new": "return value * 3",
            },
        ),
        ("run_tests", {}),
        (
            "submit",
            {"summary": "Recovered from a failed attempt and verified triple()."},
        ),
    ),
    "premature-submission": (
        (
            "apply_patch",
            {"path": "value.py", "old": "VALUE = 1", "new": "VALUE = 2"},
        ),
        ("submit", {"summary": "too early"}),
        ("run_tests", {}),
        ("submit", {"summary": "Verified VALUE = 2."}),
    ),
    "stale-verification": (
        (
            "apply_patch",
            {"path": "value.py", "old": "VALUE = 1", "new": "VALUE = 2"},
        ),
        ("run_tests", {}),
        (
            "apply_patch",
            {"path": "value.py", "old": "VALUE = 2", "new": "VALUE = 3"},
        ),
        ("submit", {"summary": "stale"}),
        (
            "apply_patch",
            {"path": "value.py", "old": "VALUE = 3", "new": "VALUE = 2"},
        ),
        ("run_tests", {}),
        ("submit", {"summary": "Freshly verified VALUE = 2."}),
    ),
    "failed-test-refusal": (
        (
            "apply_patch",
            {"path": "value.py", "old": "VALUE = 1", "new": "VALUE = 3"},
        ),
        ("run_tests", {}),
        ("submit", {"summary": "must be refused"}),
    ),
    "zero-test-refusal": (
        (
            "apply_patch",
            {"path": "module.py", "old": "VALUE = 1", "new": "VALUE = 2"},
        ),
        ("run_tests", {}),
        ("submit", {"summary": "must be refused"}),
    ),
    "shell-disabled": (
        ("bash", {"command": "printf unsafe"}),
        ("run_tests", {}),
        ("submit", {"summary": "Note stayed safe."}),
    ),
    "checkpoint-resume": (
        (
            "apply_patch",
            {"path": "value.py", "old": "VALUE = 1", "new": "VALUE = 2"},
        ),
        ("run_tests", {}),
        ("submit", {"summary": "Resumed, retested, and verified."}),
    ),
    "authenticated-undo": (
        (
            "apply_patch",
            {"path": "note.py", "old": 'TEXT = "before"', "new": 'TEXT = "after"'},
        ),
        ("run_tests", {}),
        ("submit", {"summary": "Changed note and verified it."}),
    ),
}


def _bind_expected_argument_signatures(case: EvalCase) -> EvalCase:
    try:
        oracle_calls = _EXPECTED_TOOL_ARGUMENT_ORACLE[case.name]
    except KeyError as exc:
        raise ValueError(f"argument oracle missing for {case.name}") from exc
    if len(oracle_calls) != len(case.expected_tools):
        raise ValueError(f"tool contract length mismatch for {case.name}")
    expected_tools: list[ExpectedToolEvent] = []
    for expected, (tool, arguments) in zip(case.expected_tools, oracle_calls):
        if expected.tool != tool:
            raise ValueError(f"tool contract order mismatch for {case.name}")
        expected_tools.append(
            replace(
                expected,
                argument_signature=_expected_argument_signature(tool, arguments),
            )
        )
    return replace(case, expected_tools=tuple(expected_tools))


CASES = tuple(_bind_expected_argument_signatures(case) for case in CASES)


def _flatten_changes(changes: dict[str, list[str]]) -> set[str]:
    return {
        f"{kind}:{path}"
        for kind in ("created", "modified", "deleted")
        for path in changes.get(kind, [])
    }


def _test_outcomes(audit: dict[str, Any]) -> list[dict[str, int | None]]:
    return [
        {
            "returncode": event["returncode"],
            "tests_run": event.get("tests_run"),
        }
        for event in audit.get("events", [])
        if event.get("type") == "tool" and event.get("tool") == "run_tests"
    ]


def _tool_event_contract(audit: dict[str, Any]) -> tuple[ExpectedToolEvent, ...]:
    return tuple(
        ExpectedToolEvent(
            tool=str(event.get("tool", "")),
            returncode=int(event.get("returncode", -1)),
            blocked=event.get("blocked") is True,
            submitted=event.get("submitted") is True,
            tests_run=(
                int(event["tests_run"])
                if event.get("tests_run") is not None
                else None
            ),
            exception_info=str(event.get("exception_info", "")),
            argument_signature=_hash_audited_arguments(
                event["args"] if isinstance(event.get("args"), dict) else {}
            ),
        )
        for event in audit.get("events", [])
        if event.get("type") == "tool"
    )


def _sanitized_tool_evidence(
    contract: tuple[ExpectedToolEvent, ...],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for event in contract:
        exception_info = event.exception_info
        if exception_info and exception_info not in REFUSAL_CODES:
            exception_info = "UnexpectedFailure"
        evidence.append(
            {
                "tool": event.tool,
                "returncode": event.returncode,
                "blocked": event.blocked,
                "submitted": event.submitted,
                "tests_run": event.tests_run,
                "exception_info": exception_info,
            }
        )
    return evidence


def _recovered_after_failed_edit(audit: dict[str, Any]) -> bool:
    has_edit = False
    failed_after_edit = False
    for event in audit.get("events", []):
        if event.get("type") != "tool":
            continue
        if (
            event.get("tool") in {"write_file", "apply_patch", "replace_lines"}
            and event.get("returncode") == 0
        ):
            has_edit = True
            continue
        if event.get("tool") != "run_tests" or not has_edit:
            continue
        if event.get("returncode") == 0 and failed_after_edit:
            return True
        if event.get("returncode") != 0:
            failed_after_edit = True
    return False


def _test_command() -> str:
    return f"{shlex.quote(sys.executable)} -m unittest discover -v"


def _executor(workspace: Path) -> BashExecutor:
    return BashExecutor(
        workspace,
        approval_mode="yolo",
        sandbox_mode="none",
        default_test_command=_test_command(),
    )


def _case_uses_tool(case: EvalCase, tool: str) -> bool:
    return any(
        planned_tool == tool
        for response in case.responses
        for planned_tool, _arguments in response.calls
    )


def _initialize_git_baseline(case: EvalCase, executor: BashExecutor) -> None:
    if not _case_uses_tool(case, "git_diff"):
        return
    git = str(getattr(executor, "_git_executable", ""))
    if not git:
        raise TrustedGitUnavailable("a trusted Git executable is required")
    hooks = Path(getattr(executor, "_runtime_root")) / "git-hooks"
    hooks.mkdir(mode=0o700)
    commands = (
        [git, "init", "--quiet"],
        [git, "add", "--all"],
        [
            git,
            "-c",
            "user.name=Verified Patch Eval",
            "-c",
            "user.email=eval@example.invalid",
            "-c",
            f"core.hooksPath={hooks}",
            "commit",
            "--quiet",
            "--no-gpg-sign",
            "-m",
            "evaluation baseline",
        ],
    )
    for command in commands:
        try:
            completed = executor._run_argv(
                command,
                sandbox=False,
                timeout_seconds=10,
            )
        except Exception as exc:
            raise GitBaselineFailed("trusted Git baseline command failed") from exc
        if completed.returncode != 0:
            raise GitBaselineFailed("trusted Git baseline command failed")


@contextmanager
def _scenario_state_directory(path: Path) -> Iterator[None]:
    existed = "MCA_STATE_DIR" in os.environ
    previous = os.environ.get("MCA_STATE_DIR")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ["MCA_STATE_DIR"] = str(path)
    try:
        yield
    finally:
        if existed and previous is not None:
            os.environ["MCA_STATE_DIR"] = previous
        else:
            os.environ.pop("MCA_STATE_DIR", None)


def _run_standard(case: EvalCase, workspace: Path) -> ScenarioExecution:
    executor = _executor(workspace)
    _initialize_git_baseline(case, executor)
    before = executor.workspace_fingerprint()
    audit = MiniCodeAgent(
        ScriptedEvalModel(case.responses),
        executor,
        max_steps=len(case.responses) + 2,
        quiet=True,
    ).run(case.task)
    final_changes = before.diff(executor.workspace_fingerprint())
    return ScenarioExecution(audit=audit, final_changes=final_changes)


def _run_resume(case: EvalCase, workspace: Path, temporary: Path) -> ScenarioExecution:
    first_executor = _executor(workspace)
    _initialize_git_baseline(case, first_executor)
    before = first_executor.workspace_fingerprint()
    persistence_file = temporary / "resume.json"
    first = MiniCodeAgent(
        ScriptedEvalModel(case.responses[:1]),
        first_executor,
        max_steps=1,
        trajectory_path=persistence_file,
        quiet=True,
    ).run(case.task)
    checkpoint = load_trajectory(persistence_file)
    checkpoint_steps = int(first.get("steps", 0))
    checkpoint_tools = [
        event
        for event in checkpoint.get("events", [])
        if event.get("type") == "tool"
    ]
    safe_boundary = bool(
        first.get("exit_status") == "StepLimitExceeded"
        and first.get("resumable") is True
        and checkpoint.get("exit_status") == "StepLimitExceeded"
        and checkpoint_tools
        and checkpoint_tools[-1].get("tool") == "apply_patch"
        and checkpoint_tools[-1].get("returncode") == 0
    )

    resumed_executor = _executor(workspace)
    audit = MiniCodeAgent(
        ScriptedEvalModel(case.responses[1:]),
        resumed_executor,
        max_steps=5,
        trajectory_path=persistence_file,
        quiet=True,
    ).run(resume_data=checkpoint)
    fresh_test = any(
        event.get("type") == "tool"
        and event.get("tool") == "run_tests"
        and int(event.get("step", 0)) > checkpoint_steps
        and event.get("returncode") == 0
        and int(event.get("tests_run") or 0) > 0
        for event in audit.get("events", [])
    )
    final_changes = before.diff(resumed_executor.workspace_fingerprint())
    return ScenarioExecution(
        audit=audit,
        final_changes=final_changes,
        checkpoint_safe_boundary=safe_boundary,
        fresh_test_after_checkpoint=fresh_test,
    )


def _run_undo(case: EvalCase, workspace: Path, temporary: Path) -> ScenarioExecution:
    executor = _executor(workspace)
    _initialize_git_baseline(case, executor)
    before = executor.workspace_fingerprint()
    original = (workspace / "note.py").read_text(encoding="utf-8")
    persistence_file = temporary / "undo.json"
    audit = MiniCodeAgent(
        ScriptedEvalModel(case.responses),
        executor,
        max_steps=len(case.responses) + 2,
        trajectory_path=persistence_file,
        quiet=True,
    ).run(case.task)
    edit_changes = _flatten_changes(before.diff(executor.workspace_fingerprint()))
    saved = load_trajectory(persistence_file)
    restoration_actions = undo_trajectory(saved)
    authenticated = bool(
        str(saved.get("undo_journal", "")).startswith("state:")
        and restoration_actions == ["restored note.py"]
    )
    restored = (workspace / "note.py").read_text(encoding="utf-8") == original
    final_changes = before.diff(executor.workspace_fingerprint())
    return ScenarioExecution(
        audit=audit,
        final_changes=final_changes,
        authenticated_restoration=authenticated,
        restored_original=restored,
        exact_edit_before_revert=edit_changes == {"modified:note.py"},
    )


def _execute_case(case: EvalCase, workspace: Path, temporary: Path) -> ScenarioExecution:
    started = time.perf_counter()
    if case.runner == "resume":
        execution = _run_resume(case, workspace, temporary)
    elif case.runner == "undo":
        execution = _run_undo(case, workspace, temporary)
    else:
        execution = _run_standard(case, workspace)
    execution.duration_ms = int((time.perf_counter() - started) * 1000)
    return execution


def _normalized_exit_status(audit: dict[str, Any]) -> str:
    status = str(audit.get("exit_status") or "Stopped")
    if (
        status == "Error:RuntimeError"
        and audit.get("error")
        == "RuntimeError: scripted evaluation model exhausted its response plan"
    ):
        return "PlanExhausted"
    return status


def _accepted_submission(audit: dict[str, Any]) -> bool:
    return any(
        event.get("type") == "tool"
        and event.get("tool") == "submit"
        and event.get("submitted") is True
        for event in audit.get("events", [])
    )


def _real_git_diff_observed(audit: dict[str, Any]) -> bool:
    diff_events = [
        event
        for event in audit.get("events", [])
        if event.get("type") == "tool" and event.get("tool") == "git_diff"
    ]
    return bool(diff_events) and all(
        event.get("returncode") == 0
        and "diff --git a/" in str(event.get("output", ""))
        and "\n--- a/" in str(event.get("output", ""))
        and "\n+++ b/" in str(event.get("output", ""))
        for event in diff_events
    )


def _case_report(case: EvalCase, workspace: Path, execution: ScenarioExecution) -> dict[str, Any]:
    audit = execution.audit
    tool_contract = _tool_event_contract(audit)
    tool_contract_matched = tool_contract == case.expected_tools
    requires_real_git_diff = _case_uses_tool(case, "git_diff")
    real_git_diff = _real_git_diff_observed(audit)
    actual_changes = _flatten_changes(execution.final_changes)
    unrelated_changes = sorted(actual_changes - case.expected_changes)
    missing_changes = sorted(case.expected_changes - actual_changes)
    tests = _test_outcomes(audit)
    expected_tests = [
        {"returncode": returncode, "tests_run": tests_run}
        for returncode, tests_run in case.expected_tests
    ]
    refusal_evidence = sorted(
        {
            str(event.get("exception_info"))
            for event in audit.get("events", [])
            if event.get("type") == "tool"
            and event.get("exception_info") in REFUSAL_CODES
        }
    )
    lifecycle_evidence: set[str] = set()
    if any(test["returncode"] == 0 for test in tests):
        lifecycle_evidence.add("PassingTests")
    if any(test["returncode"] != 0 for test in tests):
        lifecycle_evidence.add("FailedTests")
    if any(test["tests_run"] == 0 for test in tests):
        lifecycle_evidence.add("ZeroTests")

    recovered = _recovered_after_failed_edit(audit)
    if recovered:
        lifecycle_evidence.add("FailedTestAfterEdit")
    accepted_submission = _accepted_submission(audit)
    verification_status = str(audit.get("verification_status", ""))
    verified_submission = bool(
        accepted_submission
        and verification_status == "passed"
        and audit.get("verified_fingerprint")
    )
    if verified_submission:
        lifecycle_evidence.add("VerifiedSubmission")
    exit_status = _normalized_exit_status(audit)
    if exit_status == "PlanExhausted":
        lifecycle_evidence.add("PlanExhausted")
    if execution.checkpoint_safe_boundary:
        lifecycle_evidence.add("CheckpointSafeBoundary")
    if execution.fresh_test_after_checkpoint:
        lifecycle_evidence.add("FreshPassingTest")
    if execution.authenticated_restoration:
        lifecycle_evidence.add("AuthenticatedRestoration")
    if execution.restored_original:
        lifecycle_evidence.add("OriginalRestored")
    if real_git_diff:
        lifecycle_evidence.add("RealGitDiff")

    evidence = set(refusal_evidence) | lifecycle_evidence
    if (
        execution.authenticated_restoration
        and execution.restored_original
        and execution.exact_edit_before_revert
    ):
        outcome: Outcome = "reverted"
    elif accepted_submission:
        outcome = "submitted"
    else:
        outcome = "refused"

    validation_errors: list[str] = []
    if exit_status != case.expected_exit_status:
        validation_errors.append("ExitStatusMismatch")
    if verification_status != case.expected_verification_status:
        validation_errors.append("VerificationStatusMismatch")
    if outcome != case.expected_outcome:
        validation_errors.append("OutcomeMismatch")
    if accepted_submission != case.expects_submission:
        validation_errors.append("SubmissionBehaviorMismatch")
    if unrelated_changes:
        validation_errors.append("UnrelatedChanges")
    if missing_changes:
        validation_errors.append("MissingExpectedChanges")
    if tests != expected_tests:
        validation_errors.append("TestEvidenceMismatch")
    if not tool_contract_matched:
        validation_errors.append("ToolEventContractMismatch")
    if requires_real_git_diff and not real_git_diff:
        validation_errors.append("GitDiffEvidenceMissing")
    if not case.expected_evidence.issubset(evidence):
        validation_errors.append("ExpectedEvidenceMissing")
    if case.expected_verification_status == "passed" and not audit.get(
        "verified_fingerprint"
    ):
        validation_errors.append("VerifiedFingerprintMissing")
    if case.expected_verification_status == "failed" and audit.get(
        "verified_fingerprint"
    ):
        validation_errors.append("UnexpectedVerifiedFingerprint")
    for relative, fragment in case.expected_file_fragments:
        path = workspace / relative
        if not path.is_file() or fragment not in path.read_text(encoding="utf-8"):
            validation_errors.append(f"ExpectedFileStateMismatch:{relative}")
    submission = str(audit.get("submission", ""))
    if (
        case.expected_submission_fragment
        and case.expected_submission_fragment not in submission
    ):
        validation_errors.append("SubmissionSummaryMismatch")
    if case.expects_recovery and not recovered:
        validation_errors.append("RecoveryEvidenceMissing")
    if case.runner == "resume" and not (
        execution.checkpoint_safe_boundary
        and execution.fresh_test_after_checkpoint
    ):
        validation_errors.append("ResumeLifecycleMismatch")
    if case.runner == "undo" and not (
        execution.authenticated_restoration
        and execution.restored_original
        and execution.exact_edit_before_revert
    ):
        validation_errors.append("UndoLifecycleMismatch")
    if case.expected_outcome == "refused":
        expected_policy_codes = case.expected_evidence & REFUSAL_CODES
        policy_failure = any(
            event.get("type") == "tool"
            and event.get("exception_info") in expected_policy_codes
            and (event.get("blocked") is True or event.get("returncode") != 0)
            for event in audit.get("events", [])
        )
        if not policy_failure or accepted_submission:
            validation_errors.append("ExpectedRefusalMismatch")

    tool_calls = sum(
        event.get("type") == "tool" for event in audit.get("events", [])
    )
    unexpected_submission = accepted_submission and not case.expects_submission
    return {
        "name": case.name,
        "category": case.category,
        "passed": not validation_errors,
        "outcome": outcome,
        "accepted_submission": accepted_submission,
        "verified_submission": verified_submission,
        "expected_refusal": case.expected_outcome == "refused",
        "unexpected_submission": unexpected_submission,
        "exit_status": exit_status,
        "verification_status": verification_status,
        "expected_changes": sorted(case.expected_changes),
        "unrelated_changes": unrelated_changes,
        "missing_expected_changes": missing_changes,
        "exact_changes": not unrelated_changes and not missing_changes,
        "tools": _sanitized_tool_evidence(tool_contract),
        "tool_contract_matched": tool_contract_matched,
        "real_git_diff": real_git_diff,
        "tests": tests,
        "evidence": sorted(evidence),
        "refusal_evidence": refusal_evidence,
        "verification_evidence": sorted(
            lifecycle_evidence
            & {"FailedTests", "PassingTests", "VerifiedSubmission", "ZeroTests"}
        ),
        "lifecycle_evidence": sorted(
            lifecycle_evidence
            - {"FailedTests", "PassingTests", "VerifiedSubmission", "ZeroTests"}
        ),
        "recovered_from_failure": recovered,
        "checkpoint_safe_boundary": execution.checkpoint_safe_boundary,
        "fresh_test_after_checkpoint": execution.fresh_test_after_checkpoint,
        "authenticated_restoration": execution.authenticated_restoration,
        "restored_original": execution.restored_original,
        "steps": int(audit.get("steps", 0)),
        "tool_calls": int(tool_calls),
        "duration_ms": execution.duration_ms,
        "validation_errors": validation_errors,
    }


def _sanitized_harness_failure(case: EvalCase, exc: Exception) -> dict[str, Any]:
    return {
        "name": case.name,
        "category": case.category,
        "passed": False,
        "outcome": "refused",
        "accepted_submission": False,
        "verified_submission": False,
        "expected_refusal": case.expected_outcome == "refused",
        "unexpected_submission": False,
        "exit_status": "HarnessError",
        "verification_status": "unknown",
        "expected_changes": sorted(case.expected_changes),
        "unrelated_changes": [],
        "missing_expected_changes": sorted(case.expected_changes),
        "exact_changes": False,
        "tools": [],
        "tool_contract_matched": False,
        "real_git_diff": False,
        "tests": [],
        "evidence": [],
        "refusal_evidence": [],
        "verification_evidence": [],
        "lifecycle_evidence": [],
        "recovered_from_failure": False,
        "checkpoint_safe_boundary": False,
        "fresh_test_after_checkpoint": False,
        "authenticated_restoration": False,
        "restored_original": False,
        "steps": 0,
        "tool_calls": 0,
        "duration_ms": 0,
        "validation_errors": [f"HarnessException:{type(exc).__name__}"],
    }


def run_case(case: EvalCase) -> dict[str, Any]:
    fixture = REPOSITORY_ROOT / "evals" / "fixtures" / case.fixture
    if not fixture.is_dir():
        raise FileNotFoundError("evaluation fixture is missing")

    with tempfile.TemporaryDirectory(prefix=f"mca-eval-{case.name}-") as raw_temporary:
        temporary = Path(raw_temporary)
        workspace = temporary / "workspace"
        shutil.copytree(fixture, workspace)
        with _scenario_state_directory(temporary / "state"):
            execution = _execute_case(case, workspace, temporary)
            return _case_report(case, workspace, execution)


def run_suite(case_names: set[str] | None = None) -> dict[str, Any]:
    selected = [case for case in CASES if not case_names or case.name in case_names]
    unknown = sorted((case_names or set()) - {case.name for case in CASES})
    if unknown:
        raise ValueError(f"unknown evaluation case(s): {', '.join(unknown)}")
    if not selected:
        raise ValueError("no evaluation cases selected")

    results: list[dict[str, Any]] = []
    for case in selected:
        try:
            results.append(run_case(case))
        except Exception as exc:
            results.append(_sanitized_harness_failure(case, exc))

    case_count = len(results)
    passed_count = sum(bool(result["passed"]) for result in results)
    return {
        "schema_version": SCHEMA_VERSION,
        "suite": SUITE_NAME,
        "harness": {
            "python": {
                "major": sys.version_info.major,
                "minor": sys.version_info.minor,
            },
            "platform": platform.system(),
            "offline": True,
            "network": "disabled",
        },
        "aggregate": {
            "cases": case_count,
            "passed": passed_count,
            "pass_rate": passed_count / case_count,
            "verified_submissions": sum(
                bool(result["verified_submission"]) for result in results
            ),
            "expected_refusals": sum(
                bool(result["expected_refusal"]) for result in results
            ),
            "unexpected_submissions": sum(
                bool(result["unexpected_submission"]) for result in results
            ),
            "unrelated_change_count": sum(
                len(result["unrelated_changes"]) for result in results
            ),
            "steps": sum(int(result["steps"]) for result in results),
            "tool_calls": sum(int(result["tool_calls"]) for result in results),
            "duration_ms": sum(int(result["duration_ms"]) for result in results),
        },
        "cases": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run deterministic, offline mini-code-agent policy evaluations."
    )
    parser.add_argument(
        "--case",
        action="append",
        choices=[case.name for case in CASES],
        help="Run only this case; repeat the option to select multiple cases.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Also write the sanitized JSON report to this path.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the report as JSON (the stable default output format).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_suite(set(args.case) if args.case else None)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["aggregate"]["passed"] == report["aggregate"]["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
