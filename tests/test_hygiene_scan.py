from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_added_content.py"
PLAN = ROOT / "docs" / "superpowers" / "plans" / "2026-07-20-measured-github-launch.md"


def _scanner_module():
    assert SCRIPT.is_file(), "the documented added-content scanner is missing"
    spec = spec_from_file_location("check_added_content", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _added_line(path: str, content: str) -> str:
    return f"diff --git a/{path} b/{path}\n+++ b/{path}\n+{content}\n"


def test_launch_plan_documents_the_complete_neutral_added_content_scan():
    plan = PLAN.read_text(encoding="utf-8")
    personal_home = "/" + "Users/" + "Zhuanz"

    assert personal_home not in plan
    assert ":(exclude)docs/superpowers/**" not in plan
    assert ".venv/bin/python scripts/check_added_content.py" in plan


def test_scan_allows_only_an_exact_candidate_file_and_context():
    scanner = _scanner_module()
    synthetic = "sk-" + "testsecret123456"
    approved = _added_line(
        "tests/test_agent_cli.py", f'    secret = "{synthetic}"'
    )

    findings = scanner.validate_added_diff(approved)

    assert sum(findings.values()) == 1
    with pytest.raises(ValueError, match="unapproved added-content candidate"):
        scanner.validate_added_diff(
            _added_line("tests/other.py", f'secret = "{synthetic}"')
        )
    with pytest.raises(ValueError, match="unapproved added-content candidate"):
        scanner.validate_added_diff(
            _added_line(
                "tests/test_agent_cli.py", f'other = "{synthetic}"'
            )
        )


@pytest.mark.parametrize(
    "candidate",
    [
        "sk-" + "unrecognized-value-123456",
        "/" + "Users/alice/private/project.txt",
        "/" + "home/bob/private/project.txt",
    ],
)
def test_scan_rejects_unknown_secrets_and_home_paths(candidate: str):
    scanner = _scanner_module()

    with pytest.raises(ValueError, match="unapproved added-content candidate"):
        scanner.validate_added_diff(_added_line("notes.txt", candidate))


def test_scan_patterns_do_not_flag_the_scanner_source_itself():
    scanner = _scanner_module()
    source_diff = "+++ b/scripts/check_added_content.py\n" + "\n".join(
        f"+{line}" for line in SCRIPT.read_text(encoding="utf-8").splitlines()
    )

    assert scanner.scan_added_diff(source_diff) == {}
