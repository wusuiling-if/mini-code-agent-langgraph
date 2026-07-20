#!/usr/bin/env python3
"""Run a small, offline coding-agent behavior baseline.

The suite deliberately uses scripted local models.  It evaluates the agent loop,
tool policy, verification gate, recovery behavior, and workspace discipline
without spending API tokens or depending on network availability.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from langchain_core.messages import AIMessage  # noqa: E402

from mini_code_agent.agent import MiniCodeAgent  # noqa: E402
from mini_code_agent.executor import BashExecutor  # noqa: E402


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PlannedResponse:
    content: str
    calls: tuple[tuple[str, dict[str, Any]], ...]


@dataclass(frozen=True)
class EvalCase:
    name: str
    category: str
    task: str
    fixture: str
    responses: tuple[PlannedResponse, ...]
    expected_changes: frozenset[str]
    expected_file_fragments: tuple[tuple[str, str], ...] = ()
    expected_submission_fragment: str = ""
    expects_recovery: bool = False


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
            _response(
                "Verify and inspect the result.",
                ("run_tests", {}),
                ("git_diff", {}),
            ),
            _response(
                "Submit the verified fix.",
                ("submit", {"summary": "Fixed add() and verified the test suite."}),
            ),
        ),
        expected_changes=frozenset({"modified:calculator.py"}),
        expected_file_fragments=(("calculator.py", "return a + b"),),
        expected_submission_fragment="Fixed add()",
    ),
    EvalCase(
        name="explain-only",
        category="no_change_explanation",
        task="Explain how apply_discount() works. Do not modify the project.",
        fixture="explain_only",
        responses=(
            _response(
                "Inspect the relevant implementation.",
                ("list_files", {}),
                ("read_file", {"path": "pricing.py"}),
            ),
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
        expected_changes=frozenset(),
        expected_submission_fragment="one minus the rate",
    ),
    EvalCase(
        name="failed-fix-recovery",
        category="failure_recovery",
        task="Fix triple() and recover if the first attempted correction is still wrong.",
        fixture="failure_recovery",
        responses=(
            _response("Reproduce the failure.", ("run_tests", {})),
            _response("Inspect the implementation.", ("read_file", {"path": "transform.py"})),
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
                "The test still fails; apply the correct multiplication.",
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
        expected_changes=frozenset({"modified:transform.py"}),
        expected_file_fragments=(("transform.py", "return value * 3"),),
        expected_submission_fragment="Recovered from a failed attempt",
        expects_recovery=True,
    ),
)


def _flatten_changes(changes: dict[str, list[str]]) -> set[str]:
    return {
        f"{kind}:{path}"
        for kind in ("created", "modified", "deleted")
        for path in changes.get(kind, [])
    }


def _test_outcomes(trajectory: dict[str, Any]) -> list[int]:
    return [
        int(event.get("returncode", -1))
        for event in trajectory.get("events", [])
        if event.get("type") == "tool" and event.get("tool") == "run_tests"
    ]


def _recovered_after_failed_edit(trajectory: dict[str, Any]) -> bool:
    """Return true only when a post-edit test failed before a later pass."""

    has_edit = False
    failed_after_edit = False
    for event in trajectory.get("events", []):
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


def run_case(case: EvalCase) -> dict[str, Any]:
    fixture = REPOSITORY_ROOT / "evals" / "fixtures" / case.fixture
    if not fixture.is_dir():
        raise FileNotFoundError(f"evaluation fixture is missing: {fixture}")

    with tempfile.TemporaryDirectory(prefix=f"mca-eval-{case.name}-") as temporary:
        workspace = Path(temporary) / "workspace"
        shutil.copytree(fixture, workspace)
        test_command = (
            f"{shlex.quote(sys.executable)} -m unittest discover -v"
        )
        executor = BashExecutor(
            workspace,
            approval_mode="yolo",
            sandbox_mode="none",
            default_test_command=test_command,
        )
        agent = MiniCodeAgent(
            ScriptedEvalModel(case.responses),
            executor,
            max_steps=len(case.responses) + 2,
            quiet=True,
        )
        trajectory = agent.run(case.task)

        changes = trajectory.get("workspace_changes", {})
        actual_changes = _flatten_changes(changes)
        unrelated_changes = sorted(actual_changes - case.expected_changes)
        missing_changes = sorted(case.expected_changes - actual_changes)
        validation_errors: list[str] = []
        for relative, fragment in case.expected_file_fragments:
            path = workspace / relative
            if not path.is_file() or fragment not in path.read_text(encoding="utf-8"):
                validation_errors.append(
                    f"{relative} does not contain expected fragment {fragment!r}"
                )

        submission = str(trajectory.get("submission", ""))
        if (
            case.expected_submission_fragment
            and case.expected_submission_fragment not in submission
        ):
            validation_errors.append("submission did not contain the expected result")

        test_outcomes = _test_outcomes(trajectory)
        recovered = _recovered_after_failed_edit(trajectory)
        if case.expects_recovery and not recovered:
            validation_errors.append("case did not demonstrate failed-test recovery")

        verified = (
            trajectory.get("verification_status") == "passed"
            and bool(trajectory.get("verified_fingerprint"))
        )
        success = bool(
            trajectory.get("exit_status") == "Submitted"
            and verified
            and not unrelated_changes
            and not missing_changes
            and not validation_errors
        )
        tool_calls = sum(
            event.get("type") == "tool"
            for event in trajectory.get("events", [])
        )
        return {
            "name": case.name,
            "category": case.category,
            "success": success,
            "verified": verified,
            "steps": int(trajectory.get("steps", 0)),
            "tool_calls": int(tool_calls),
            "duration_ms": int(trajectory.get("duration_ms", 0)),
            "unrelated_changes": unrelated_changes,
            "missing_expected_changes": missing_changes,
            "workspace_changes": changes,
            "test_returncodes": test_outcomes,
            "recovered_from_failure": recovered,
            "exit_status": trajectory.get("exit_status", ""),
            "validation_errors": validation_errors,
        }


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
            results.append(
                {
                    "name": case.name,
                    "category": case.category,
                    "success": False,
                    "verified": False,
                    "steps": 0,
                    "tool_calls": 0,
                    "duration_ms": 0,
                    "unrelated_changes": [],
                    "missing_expected_changes": sorted(case.expected_changes),
                    "workspace_changes": {},
                    "test_returncodes": [],
                    "recovered_from_failure": False,
                    "exit_status": "HarnessError",
                    "validation_errors": [f"{type(exc).__name__}: {exc}"],
                }
            )

    case_count = len(results)
    success_count = sum(bool(result["success"]) for result in results)
    verified_count = sum(bool(result["verified"]) for result in results)
    return {
        "schema_version": SCHEMA_VERSION,
        "aggregate": {
            "cases": case_count,
            "success": success_count,
            "success_rate": success_count / case_count,
            "verified": verified_count,
            "verified_rate": verified_count / case_count,
            "steps": sum(int(result["steps"]) for result in results),
            "tool_calls": sum(int(result["tool_calls"]) for result in results),
            "duration_ms": sum(int(result["duration_ms"]) for result in results),
            "unrelated_changes": sum(
                len(result["unrelated_changes"]) for result in results
            ),
        },
        "cases": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run deterministic, offline mini-code-agent behavior evaluations."
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
        help="Also write the JSON report to this path.",
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
    return 0 if report["aggregate"]["success"] == report["aggregate"]["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
