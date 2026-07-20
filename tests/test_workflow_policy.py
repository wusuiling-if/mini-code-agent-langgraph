from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
CHECKOUT_SHA = "34e114876b0b11c390a56381ad16ebd13914f8d5"
SETUP_PYTHON_SHA = "a26af69be951a213d495a4c3e4e4022e16d87065"


def _workflow(name: str) -> str:
    return (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def test_release_requires_main_ancestry_before_build_and_preserves_artifact_gate():
    workflow = _workflow("release.yml")
    checkout = f"actions/checkout@{CHECKOUT_SHA}"
    ancestry_gate = "git merge-base --is-ancestor HEAD origin/main"
    build = "python -m build --sdist --wheel"

    assert checkout in workflow
    checkout_tail = workflow[workflow.index(checkout) :]
    assert "fetch-depth: 0" in checkout_tail.split("- uses:", 1)[0]
    assert ancestry_gate in workflow
    assert "reachable from origin/main" in workflow
    assert workflow.index(ancestry_gate) < workflow.index(build)
    assert "source version" in workflow
    assert 'expected = f"v{version}"' in workflow
    publish_job = workflow[workflow.index("  publish:") :]
    assert "needs: build" in publish_job
    assert "actions/download-artifact@" in publish_job
    assert "id-token: write" in publish_job
    assert "id-token: write" not in workflow[: workflow.index("  publish:")]


def test_tests_workflow_uses_least_privilege_and_immutable_reviewed_actions():
    workflow = _workflow("tests.yml")
    assert workflow.index("permissions:\n  contents: read") < workflow.index("jobs:")
    assert not re.search(r"actions/(?:checkout|setup-python)@v\d+", workflow)
    checkout_uses = re.findall(r"actions/checkout@([^\s]+)", workflow)
    setup_python_uses = re.findall(r"actions/setup-python@([^\s]+)", workflow)

    assert checkout_uses
    assert setup_python_uses
    assert set(checkout_uses) == {CHECKOUT_SHA}
    assert set(setup_python_uses) == {SETUP_PYTHON_SHA}
