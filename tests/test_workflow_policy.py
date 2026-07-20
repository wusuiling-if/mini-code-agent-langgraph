from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIRECTORY = ROOT / ".github" / "workflows"
EXPECTED_WORKFLOWS = {
    "codeql.yml",
    "release.yml",
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


def _mapping_block(
    text: str,
    name: str,
    *,
    header_indent: int,
    entry_indent: int,
) -> dict[str, str]:
    lines = text.splitlines()
    header = f"{' ' * header_indent}{name}:"
    header_indexes = [index for index, line in enumerate(lines) if line == header]
    assert len(header_indexes) == 1, f"expected exactly one {name} block"

    values: dict[str, str] = {}
    for line in lines[header_indexes[0] + 1 :]:
        if not line.strip():
            continue
        indentation = len(line) - len(line.lstrip(" "))
        if indentation <= header_indent:
            break
        assert indentation == entry_indent, f"invalid indentation in {name} block"
        key, separator, raw_value = line[entry_indent:].partition(":")
        assert separator and key and raw_value.strip(), f"invalid entry in {name} block"
        assert key not in values, f"duplicate {name} key: {key}"
        values[key] = raw_value.strip()

    assert values, f"{name} block must not be empty"
    return values


def _assert_trigger_has_main_branch(workflow: str, event: str) -> None:
    prefix = _workflow_prefix(workflow)
    event_match = re.search(
        rf"(?ms)^  {re.escape(event)}:\n(?P<body>.*?)(?=^  [a-z_]+:|^permissions:)",
        prefix,
    )
    assert event_match, f"missing {event} trigger"
    assert re.search(r"(?m)^    branches:\s*\[main\]\s*$", event_match.group("body"))


def _assert_reviewed_action_pins(directory: Path | None = None) -> None:
    for path in _workflow_paths(directory):
        workflow = path.read_text(encoding="utf-8")
        uses_values = re.findall(r"(?m)^\s*(?:-\s*)?uses:\s*([^\s#]+)", workflow)
        for uses_value in uses_values:
            match = re.fullmatch(r"([^@]+)@([0-9a-f]{40})", uses_value)
            assert match, f"{path.name} has a mutable or malformed action: {uses_value}"
            action, sha = match.groups()
            assert action in APPROVED_ACTIONS, f"{path.name} uses unapproved action: {action}"
            assert sha == APPROVED_ACTIONS[action], f"{path.name} uses unreviewed SHA for {action}"


def _assert_default_permissions_are_read_only() -> None:
    for path in _workflow_paths():
        workflow = path.read_text(encoding="utf-8")
        assert "pull_request_target" not in workflow
        prefix = _workflow_prefix(workflow)
        assert _mapping_block(
            prefix,
            "permissions",
            header_indent=0,
            entry_indent=2,
        ) == {"contents": "read"}, f"{path.name} needs read-only default permissions"


def _assert_codeql_permissions_are_narrow(workflow: str) -> None:
    analysis = _job_blocks(workflow)["analysis"]
    assert _mapping_block(
        analysis,
        "permissions",
        header_indent=4,
        entry_indent=6,
    ) == {"contents": "read", "security-events": "write"}
    assert workflow.count("security-events: write") == 1


def _assert_release_permissions_are_narrow(workflow: str) -> None:
    jobs = _job_blocks(workflow)
    assert _mapping_block(
        jobs["publish"],
        "permissions",
        header_indent=4,
        entry_indent=6,
    ) == {"id-token": "write"}
    assert "id-token: write" not in jobs["build"]
    assert workflow.count("id-token: write") == 1


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


def test_workflows_reject_privileged_pull_request_target_and_default_read_only():
    _assert_default_permissions_are_read_only()


def test_every_job_has_an_explicit_timeout():
    expected_timeouts = {
        "tests.yml": {"pytest": 20, "cli-smoke": 15, "package": 20, "offline-eval": 15},
        "supply-chain.yml": {"pip-audit": 15, "dependency-review": 15},
        "codeql.yml": {"analysis": 20},
        "release.yml": {"build": 20, "publish": 10},
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
    assert "python -m pip_audit --strict" in jobs["pip-audit"]
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
