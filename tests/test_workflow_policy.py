from pathlib import Path
import re
import sys

import pytest
import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIRECTORY = ROOT / ".github" / "workflows"
EXPECTED_WORKFLOWS = {
    "codeql.yml",
    "release.yml",
    "sandbox.yml",
    "supply-chain.yml",
    "tests.yml",
}
APPROVED_ACTIONS = {
    "actions/checkout": "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0",
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "actions/download-artifact": "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
    "actions/dependency-review-action": "a1d282b36b6f3519aa1f3fc636f609c47dddb294",
    "github/codeql-action/init": "7188fc363630916deb702c7fdcf4e481b751f97a",
    "github/codeql-action/analyze": "7188fc363630916deb702c7fdcf4e481b751f97a",
    "pypa/gh-action-pypi-publish": "ba38be9e461d3875417946c167d0b5f3d385a247",
}
JOB_PERMISSION_EXCEPTIONS = {
    ("codeql.yml", "analysis"): {"contents": "read", "security-events": "write"},
    ("release.yml", "publish"): {"id-token": "write"},
}


def _required_file(relative_path: str) -> Path:
    path = ROOT / relative_path
    assert path.is_file(), f"required policy file is missing: {relative_path}"
    return path


def _workflow_paths(directory: Path | None = None) -> tuple[Path, ...]:
    directory = WORKFLOW_DIRECTORY if directory is None else directory
    return tuple(sorted({*directory.glob("*.yml"), *directory.glob("*.yaml")}))


def _workflow(name: str) -> str:
    workflows = {path.name: path for path in _workflow_paths()}
    assert name in workflows, f"required workflow is missing: {name}"
    return workflows[name].read_text(encoding="utf-8")


def _workflow_prefix(workflow: str) -> str:
    assert "\njobs:\n" in workflow
    return workflow.split("\njobs:\n", 1)[0]


def _job_blocks(workflow: str) -> dict[str, str]:
    _, jobs_text = workflow.split("\njobs:\n", 1)
    starts = list(re.finditer(r"(?m)^  ([a-zA-Z0-9_-]+):\s*$", jobs_text))
    return {
        match.group(1): jobs_text[
            match.start() : starts[index + 1].start()
            if index + 1 < len(starts)
            else len(jobs_text)
        ]
        for index, match in enumerate(starts)
    }


def _compose_workflow(workflow: str, label: str) -> MappingNode:
    try:
        documents = tuple(yaml.compose_all(workflow, Loader=yaml.SafeLoader))
    except yaml.YAMLError as exc:
        raise AssertionError(f"{label} must contain valid YAML") from exc
    assert len(documents) == 1, f"{label} must contain exactly one YAML document"
    document = documents[0]
    assert isinstance(document, MappingNode), f"{label} root must be a mapping"
    return document


def _node_mapping(node: Node, context: str) -> dict[str, Node]:
    assert isinstance(node, MappingNode), f"{context} must be a mapping"
    values: dict[str, Node] = {}
    for key_node, value_node in node.value:
        assert isinstance(key_node, ScalarNode), f"{context} keys must be scalar"
        key = key_node.value
        assert key != "<<", f"{context} must not use YAML merge keys"
        assert key not in values, f"duplicate {context} key: {key}"
        values[key] = value_node
    return values


def _node_sequence(node: Node, context: str) -> tuple[Node, ...]:
    assert isinstance(node, SequenceNode), f"{context} must be a sequence"
    return tuple(node.value)


def _scalar_value(node: Node, context: str) -> str:
    assert isinstance(node, ScalarNode), f"{context} must be a scalar"
    return node.value


def _scalar_mapping(node: Node, context: str) -> dict[str, str]:
    return {
        key: _scalar_value(value_node, f"{context}.{key}")
        for key, value_node in _node_mapping(node, context).items()
    }


def _workflow_nodes(workflow: str, label: str) -> tuple[dict[str, Node], dict[str, Node]]:
    root = _node_mapping(_compose_workflow(workflow, label), f"{label} root")
    assert "jobs" in root, f"{label} must define jobs"
    jobs = _node_mapping(root["jobs"], f"{label} jobs")
    return root, jobs


def _assert_trigger_has_main_branch(workflow: str, event: str) -> None:
    prefix = _workflow_prefix(workflow)
    event_match = re.search(
        rf"(?ms)^  {re.escape(event)}:\n(?P<body>.*?)(?=^  [a-z_]+:|^permissions:)",
        prefix,
    )
    assert event_match, f"missing {event} trigger"
    assert re.search(r"(?m)^    branches:\s*\[main\]\s*$", event_match.group("body"))


def _assert_reviewed_action(value_node: Node, context: str) -> None:
    uses_value = _scalar_value(value_node, f"{context} uses")
    match = re.fullmatch(r"([^@]+)@([0-9a-f]{40})", uses_value)
    assert match, f"{context} has a mutable or malformed action: {uses_value}"
    action, sha = match.groups()
    assert action in APPROVED_ACTIONS, f"{context} uses unapproved action: {action}"
    assert sha == APPROVED_ACTIONS[action], f"{context} uses unreviewed SHA for {action}"


def _assert_reviewed_action_pins(directory: Path | None = None) -> None:
    for path in _workflow_paths(directory):
        workflow = path.read_text(encoding="utf-8")
        _, jobs = _workflow_nodes(workflow, path.name)
        for job_name, job_node in jobs.items():
            job_context = f"{path.name} job {job_name}"
            job = _node_mapping(job_node, job_context)
            if "uses" in job:
                _assert_reviewed_action(job["uses"], job_context)
            if "steps" not in job:
                continue
            for index, step_node in enumerate(
                _node_sequence(job["steps"], f"{job_context} steps")
            ):
                step_context = f"{job_context} step {index + 1}"
                step = _node_mapping(step_node, step_context)
                if "uses" in step:
                    _assert_reviewed_action(step["uses"], step_context)


def _assert_default_permissions_are_read_only() -> None:
    for path in _workflow_paths():
        workflow = path.read_text(encoding="utf-8")
        assert "pull_request_target" not in workflow
        root, jobs = _workflow_nodes(workflow, path.name)
        assert "permissions" in root, f"{path.name} needs top-level permissions"
        assert _scalar_mapping(
            root["permissions"], f"{path.name} permissions"
        ) == {"contents": "read"}, f"{path.name} needs read-only default permissions"

        for job_name, job_node in jobs.items():
            job_context = f"{path.name} job {job_name}"
            job = _node_mapping(job_node, job_context)
            expected = JOB_PERMISSION_EXCEPTIONS.get((path.name, job_name))
            if expected is None:
                assert "permissions" not in job, (
                    f"{job_context} must inherit read-only permissions"
                )
                continue
            assert "permissions" in job, f"{job_context} needs explicit permissions"
            assert _scalar_mapping(
                job["permissions"], f"{job_context} permissions"
            ) == expected, f"{job_context} permissions must match the approved exception"


def _assert_codeql_permissions_are_narrow(workflow: str) -> None:
    _, jobs = _workflow_nodes(workflow, "codeql.yml")
    analysis = _node_mapping(jobs["analysis"], "codeql.yml job analysis")
    assert _scalar_mapping(
        analysis["permissions"], "codeql.yml job analysis permissions"
    ) == JOB_PERMISSION_EXCEPTIONS[("codeql.yml", "analysis")]


def _assert_release_permissions_are_narrow(workflow: str) -> None:
    _, jobs = _workflow_nodes(workflow, "release.yml")
    build = _node_mapping(jobs["build"], "release.yml job build")
    publish = _node_mapping(jobs["publish"], "release.yml job publish")
    assert "permissions" not in build
    assert _scalar_mapping(
        publish["permissions"], "release.yml job publish permissions"
    ) == JOB_PERMISSION_EXCEPTIONS[("release.yml", "publish")]


def test_required_supply_chain_files_exist():
    assert {path.name for path in _workflow_paths()} == EXPECTED_WORKFLOWS
    _required_file(".github/dependabot.yml")
    _required_file("requirements-ci.txt")


def test_all_workflow_actions_use_only_reviewed_immutable_shas():
    _assert_reviewed_action_pins()


def test_yaml_workflow_cannot_bypass_action_pin_policy(tmp_path):
    (tmp_path / "bypass.yaml").write_text(
        "name: bypass\njobs:\n  unsafe:\n    steps:\n      - uses: attacker/action@v1\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="mutable or malformed action"):
        _assert_reviewed_action_pins(tmp_path)


def test_action_policy_rejects_quoted_uses_key_and_alias(tmp_path):
    (tmp_path / "quoted.yml").write_text(
        """name: quoted
jobs:
  unsafe:
    steps:
      - &unsafe
        "uses": attacker/action@v1
      - *unsafe
""",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="mutable or malformed action"):
        _assert_reviewed_action_pins(tmp_path)


def test_action_policy_rejects_spaced_uses_key(tmp_path):
    (tmp_path / "spaced.yml").write_text(
        """name: spaced
jobs:
  unsafe:
    steps:
      - uses : attacker/action@v1
""",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="mutable or malformed action"):
        _assert_reviewed_action_pins(tmp_path)


def test_action_policy_rejects_job_level_uses(tmp_path):
    (tmp_path / "reusable.yml").write_text(
        """name: reusable
jobs:
  unsafe:
    "uses": attacker/repository/.github/workflows/unsafe.yml@v1
""",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="mutable or malformed action"):
        _assert_reviewed_action_pins(tmp_path)


def test_action_policy_rejects_multiple_yaml_documents(tmp_path):
    (tmp_path / "documents.yml").write_text(
        """name: first
jobs: {}
---
name: hidden
jobs: {}
""",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="exactly one YAML document"):
        _assert_reviewed_action_pins(tmp_path)


def test_workflows_reject_privileged_pull_request_target_and_default_read_only():
    _assert_default_permissions_are_read_only()


def test_tests_job_cannot_override_default_permissions(tmp_path, monkeypatch):
    workflow = _workflow("tests.yml")
    mutated = workflow.replace(
        "  pytest:\n    runs-on:",
        "  pytest:\n    permissions:\n      contents: write\n    runs-on:",
    )
    assert mutated != workflow
    (tmp_path / "tests.yml").write_text(mutated, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "WORKFLOW_DIRECTORY", tmp_path)

    with pytest.raises(AssertionError):
        _assert_default_permissions_are_read_only()


@pytest.mark.parametrize("job_name", ["pip-audit", "dependency-review"])
def test_supply_chain_jobs_cannot_override_default_permissions(
    tmp_path,
    monkeypatch,
    job_name,
):
    workflow = _workflow("supply-chain.yml")
    mutated = workflow.replace(
        f"  {job_name}:\n",
        f"  {job_name}:\n    permissions:\n      contents: write\n",
    )
    assert mutated != workflow
    (tmp_path / "supply-chain.yml").write_text(mutated, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "WORKFLOW_DIRECTORY", tmp_path)

    with pytest.raises(AssertionError):
        _assert_default_permissions_are_read_only()


def test_every_job_has_an_explicit_timeout():
    expected_timeouts = {
        "tests.yml": {"pytest": 20, "cli-smoke": 15, "package": 20, "offline-eval": 15},
        "supply-chain.yml": {"pip-audit": 15, "dependency-review": 15},
        "codeql.yml": {"analysis": 20},
        "release.yml": {"build": 20, "publish": 10},
        "sandbox.yml": {"bwrap": 15, "docker": 15, "sandbox-exec": 15},
    }
    for workflow_name, expected_jobs in expected_timeouts.items():
        jobs = _job_blocks(_workflow(workflow_name))
        assert set(jobs) == set(expected_jobs)
        for job_name, timeout in expected_jobs.items():
            assert re.search(
                rf"(?m)^    timeout-minutes:\s*{timeout}\s*$",
                jobs[job_name],
            ), f"{workflow_name}:{job_name} needs timeout {timeout}"


def test_workflows_have_safe_top_level_concurrency():
    for workflow_name in EXPECTED_WORKFLOWS:
        prefix = _workflow_prefix(_workflow(workflow_name))
        concurrency = re.search(
            r"(?ms)^concurrency:\n(?P<body>(?:  [^\n]+\n?)*)",
            prefix,
        )
        assert concurrency, f"{workflow_name} needs top-level concurrency"
        body = concurrency.group("body")
        assert re.search(r"(?m)^  group: .*\$\{\{ github\.(?:workflow|ref)", body)
        expected_cancel = "false" if workflow_name == "release.yml" else "true"
        assert re.search(
            rf"(?m)^  cancel-in-progress:\s*{expected_cancel}\s*$",
            body,
        )


def test_regular_ci_is_main_scoped_and_caches_pip_dependencies():
    tests_workflow = _workflow("tests.yml")
    _assert_trigger_has_main_branch(tests_workflow, "push")
    assert re.search(r"(?m)^  pull_request:\s*$", _workflow_prefix(tests_workflow))

    for workflow_name in ("tests.yml", "release.yml", "supply-chain.yml"):
        workflow = _workflow(workflow_name)
        setup_blocks = re.findall(
            r"(?ms)^\s*- uses: actions/setup-python@[0-9a-f]{40}.*?(?=^\s*- (?:uses|name|run):|\Z)",
            workflow,
        )
        assert setup_blocks, f"{workflow_name} needs setup-python"
        for setup_block in setup_blocks:
            assert re.search(r"(?m)^\s+cache:\s*[\"']?pip[\"']?\s*$", setup_block)
            assert re.search(r"(?m)^\s+cache-dependency-path:\s*\|\s*$", setup_block)
            assert re.search(r"(?m)^\s+pyproject\.toml\s*$", setup_block)
            assert re.search(r"(?m)^\s+requirements-ci\.txt\s*$", setup_block)


def test_offline_eval_uploads_only_sanitized_json_for_seven_days():
    eval_job = _job_blocks(_workflow("tests.yml"))["offline-eval"]
    output = "artifacts/evals/v0.3.2.json"
    assert f"python -m evals.run_evals --json --output {output}" in eval_job
    assert eval_job.count("actions/upload-artifact@") == 1
    upload_step = eval_job[eval_job.index("actions/upload-artifact@") :]
    assert re.search(r"(?m)^        if:\s*always\(\)\s*$", upload_step)
    assert re.search(rf"(?m)^          path:\s*{re.escape(output)}\s*$", upload_step)
    assert re.search(r"(?m)^          retention-days:\s*7\s*$", upload_step)
    assert re.search(r"(?m)^          if-no-files-found:\s*error\s*$", upload_step)
    assert "trajectory" not in upload_step.lower()
    assert "workspace" not in upload_step.lower()


def test_sandbox_workflow_exercises_real_backends_without_sensitive_artifacts():
    workflow = _workflow("sandbox.yml")
    jobs = _job_blocks(workflow)

    assert set(jobs) == {"bwrap", "docker", "sandbox-exec"}
    assert "sudo apt-get install -y bubblewrap" in jobs["bwrap"]
    assert "docker pull python:3.11-slim" in jobs["docker"]
    assert "runs-on: macos-latest" in jobs["sandbox-exec"]

    for job_name, backend in (
        ("bwrap", "bwrap"),
        ("docker", "docker"),
        ("sandbox-exec", "sandbox-exec"),
    ):
        job = jobs[job_name]
        assert 'python -m pip install -e ".[dev]"' in job
        assert (
            f"MCA_SANDBOX_BACKEND={backend} python -m pytest "
            "tests/test_sandbox_integration.py -q"
        ) in job
        assert f"mca sandbox probe --sandbox {backend}" in job

    lowered = workflow.lower()
    assert "actions/upload-artifact@" not in lowered
    assert "${{ secrets." not in lowered
    assert "workspace" not in lowered
    assert "trajectory" not in lowered


def test_dependabot_groups_weekly_pip_and_actions_updates():
    dependabot = _required_file(".github/dependabot.yml").read_text(encoding="utf-8")
    assert re.search(r"(?m)^version:\s*2\s*$", dependabot)
    assert dependabot.count('package-ecosystem: "pip"') == 1
    assert dependabot.count('package-ecosystem: "github-actions"') == 1
    assert dependabot.count("interval: \"weekly\"") == 2
    assert dependabot.count("open-pull-requests-limit: 2") == 2
    assert dependabot.count("groups:") == 2
    assert dependabot.count('patterns: ["*"]') == 2
    assert dependabot.count('update-types: ["minor", "patch"]') == 2


def test_supply_chain_runs_strict_audit_and_pr_only_dependency_review():
    assert _required_file("requirements-ci.txt").read_text(encoding="utf-8").splitlines() == [
        "pip==26.1.2",
        "setuptools==83.0.0",
        "pip-audit==2.10.1",
    ]
    workflow = _workflow("supply-chain.yml")
    _assert_trigger_has_main_branch(workflow, "push")
    _assert_trigger_has_main_branch(workflow, "pull_request")
    jobs = _job_blocks(workflow)
    assert "python -m pip install -e . -r requirements-ci.txt" in jobs["pip-audit"]
    assert "python -m pip_audit --strict ." in jobs["pip-audit"]
    assert "python -m pip_audit --strict -r requirements-ci.txt" in jobs["pip-audit"]
    review = jobs["dependency-review"]
    assert re.search(r"(?m)^    if:\s*github\.event_name == 'pull_request'\s*$", review)
    assert "actions/dependency-review-action@" in review
    assert re.search(r"(?m)^          fail-on-severity:\s*high\s*$", review)
    assert re.search(r"(?m)^          comment-summary-in-pr:\s*never\s*$", review)


def test_codeql_is_python_only_main_and_monday_with_narrow_write_permission():
    workflow = _workflow("codeql.yml")
    _assert_trigger_has_main_branch(workflow, "push")
    _assert_trigger_has_main_branch(workflow, "pull_request")
    prefix = _workflow_prefix(workflow)
    cron = re.search(r"(?m)^    - cron:\s*['\"]([^'\"]+)['\"]\s*$", prefix)
    assert cron, "CodeQL needs a quoted weekly cron"
    assert cron.group(1).split()[4] == "1", "CodeQL schedule must run on Monday"

    analysis = _job_blocks(workflow)["analysis"]
    _assert_codeql_permissions_are_narrow(workflow)
    assert "languages: python" in analysis
    assert "build-mode: none" in analysis
    assert "github/codeql-action/init@" in analysis
    assert "github/codeql-action/analyze@" in analysis


def test_codeql_permission_policy_rejects_duplicate_write_scope():
    workflow = _workflow("codeql.yml")
    mutated = workflow.replace(
        "      contents: read\n      security-events: write",
        "      contents: read\n      security-events: write\n      contents: write",
    )
    assert mutated != workflow

    with pytest.raises(AssertionError):
        _assert_codeql_permissions_are_narrow(mutated)


def test_codeql_permission_policy_ignores_comment_indentation_boundaries():
    workflow = _workflow("codeql.yml")
    mutated = workflow.replace(
        "      security-events: write",
        "      security-events: write\n# parser boundary\n  # consecutive comment\n      contents: write",
    )
    assert mutated != workflow

    with pytest.raises(AssertionError):
        _assert_codeql_permissions_are_narrow(mutated)


def test_release_permissions_and_gates_remain_narrow():
    workflow = _workflow("release.yml")
    jobs = _job_blocks(workflow)
    build = jobs["build"]
    publish = jobs["publish"]
    checkout = f"actions/checkout@{APPROVED_ACTIONS['actions/checkout']}"
    ancestry_gate = "git merge-base --is-ancestor HEAD origin/main"
    package_build = "python -m build --sdist --wheel"

    assert "fetch-depth: 0" in build[build.index(checkout) :].split("- uses:", 1)[0]
    assert ancestry_gate in build
    assert "reachable from origin/main" in build
    assert build.index(ancestry_gate) < build.index(package_build)
    assert "source version" in build
    assert 'expected = f"v{version}"' in build
    assert "needs: build" in publish
    assert "actions/download-artifact@" in publish
    _assert_release_permissions_are_narrow(workflow)


def test_release_permission_policy_rejects_appended_write_scope():
    workflow = _workflow("release.yml")
    mutated = workflow.replace(
        "      id-token: write",
        "      id-token: write\n      contents: write",
    )
    assert mutated != workflow

    with pytest.raises(AssertionError):
        _assert_release_permissions_are_narrow(mutated)


def test_release_permission_policy_ignores_comment_indentation_boundaries():
    workflow = _workflow("release.yml")
    mutated = workflow.replace(
        "      id-token: write",
        "      id-token: write\n# parser boundary\n    # consecutive comment\n      contents: write",
    )
    assert mutated != workflow

    with pytest.raises(AssertionError):
        _assert_release_permissions_are_narrow(mutated)
