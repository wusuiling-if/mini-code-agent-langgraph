from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.harbor.launch import (
    build_execution_environment,
    build_harbor_command,
    load_protocol,
    main,
    validate_package_spec,
)
from benchmarks.harbor.protocol import (
    HarborRunConfig,
    build_transaction_command,
    load_latest_run,
    split_harbor_model,
    usage_from_trajectory,
)


def test_fixed_model_must_use_a_supported_explicit_provider() -> None:
    assert split_harbor_model("openai/fixed-model") == ("openai", "fixed-model")
    assert split_harbor_model("deepseek/fixed-model") == (
        "deepseek",
        "fixed-model",
    )
    with pytest.raises(ValueError, match="provider/model"):
        split_harbor_model("floating-alias")
    with pytest.raises(ValueError, match="unsupported MCA provider"):
        split_harbor_model("anthropic/fixed-model")


def test_transaction_command_keeps_memory_and_shell_disabled() -> None:
    command = build_transaction_command(
        "Fix the issue; do not read /tests.",
        "openai/fixed-model",
        base_url="https://model.invalid/v1",
    )

    assert "mca tx run" in command
    assert "--memory off" in command
    assert "--sandbox none" in command
    assert "--allow-shell" not in command
    assert "git diff --check" in command
    assert "/tests/test.sh" not in command
    assert 'mca tx commit "$transaction_id"' in command
    assert "https://model.invalid/v1" in command


def test_shell_is_an_explicit_benchmark_ablation() -> None:
    command = build_transaction_command(
        "Fix it",
        "deepseek/fixed-model",
        config=HarborRunConfig(allow_shell=True),
    )
    assert "--allow-shell" in command


def test_usage_and_latest_transaction_are_sanitized_aggregates(tmp_path: Path) -> None:
    transaction = tmp_path / "state" / "transactions" / "tx-1"
    transaction.mkdir(parents=True)
    trajectory = {
        "task": "private issue text",
        "steps": 4,
        "model_usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "cached_input_tokens": 10,
            "reasoning_tokens": 3,
            "model_calls": 4,
        },
    }
    manifest = {"status": "committed"}
    (transaction / "trajectory.json").write_text(json.dumps(trajectory))
    (transaction / "manifest.json").write_text(json.dumps(manifest))

    loaded_trajectory, loaded_manifest = load_latest_run(tmp_path)
    assert loaded_trajectory == trajectory
    assert loaded_manifest == manifest
    assert usage_from_trajectory(loaded_trajectory) == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "cached_input_tokens": 10,
        "reasoning_tokens": 3,
        "model_calls": 4,
    }


def test_protocol_builds_paired_fixed_model_commands(tmp_path: Path) -> None:
    protocol = load_protocol()
    assert protocol["status"] == "adapter-ready-no-score"
    assert protocol["comparison"]["model"] == "openai/gpt-5.6-sol"
    assert protocol["comparison"]["base_url"] == "https://api.dstopology.com/v1"
    assert protocol["dataset"]["ref"].endswith(
        "@sha256:b934b0cc3dc800fe945eaf9f1623329db97ee3133c706d20644524c7759fb341"
    )

    baseline = build_harbor_command(
        "baseline", "openai/gpt-5.6-sol", tmp_path / "baseline"
    )
    candidate = build_harbor_command(
        "candidate", "openai/gpt-5.6-sol", tmp_path / "candidate"
    )

    for command in (baseline, candidate):
        assert command.count("--include-task-name") == 25
        assert command[command.index("--dataset") + 1] == protocol["dataset"]["ref"]
        assert command[command.index("--model") + 1] == "openai/gpt-5.6-sol"
        assert command[command.index("--n-attempts") + 1] == "1"
        assert command[command.index("--max-retries") + 1] == "0"
    assert "mini-swe-agent" in baseline
    assert "version=2.1.0" in baseline
    assert "benchmarks.harbor.mca_agent:MiniCodeAgentHarborAdapter" in candidate
    assert "package_spec=mini-code-agent-langgraph==0.5.0" in candidate
    assert "allow_shell=true" in candidate


def test_protocol_rejects_model_drift(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not match pinned model"):
        build_harbor_command("baseline", "openai/another-model", tmp_path / "baseline")


def test_single_task_smoke_is_paired_and_pinned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    task_name = "matplotlib__matplotlib-14623"
    assert (
        main(
            [
                "--model",
                "openai/gpt-5.6-sol",
                "--jobs-dir",
                str(tmp_path),
                "--smoke",
                "--task-name",
                task_name,
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    commands = [line for line in output.splitlines() if line.startswith("harbor run")]
    assert len(commands) == 2
    for command in commands:
        assert command.count("--include-task-name") == 1
        assert task_name in command


def test_selected_tasks_must_belong_to_pinned_pilot(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside the pinned pilot"):
        build_harbor_command(
            "baseline",
            "openai/gpt-5.6-sol",
            tmp_path / "baseline",
            task_names=["unknown__task-1"],
        )


def test_execution_environment_routes_both_arms_through_pinned_endpoint() -> None:
    env = build_execution_environment(load_protocol(), {"OPENAI_API_KEY": "secret"})
    assert env["OPENAI_API_KEY"] == "secret"
    assert env["MCA_API_KEY"] == "secret"
    assert env["OPENAI_BASE_URL"] == "https://api.dstopology.com/v1"
    assert env["MCA_BASE_URL"] == "https://api.dstopology.com/v1"


def test_execution_environment_requires_a_provider_key() -> None:
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY or MCA_API_KEY"):
        build_execution_environment(load_protocol(), {})


def test_paid_execution_requires_an_explicit_candidate_package() -> None:
    with pytest.raises(RuntimeError, match="explicit immutable --package-spec"):
        main(["--model", "openai/gpt-5.6-sol", "--smoke", "--execute"])


@pytest.mark.parametrize(
    "package_spec",
    [
        "mini-code-agent-langgraph==0.5.0",
        "git+https://example.invalid/repo.git@0123456789abcdef0123456789abcdef01234567",
        "https://example.invalid/mca.whl#sha256=" + "a" * 64,
    ],
)
def test_immutable_candidate_package_specs_are_accepted(package_spec: str) -> None:
    assert validate_package_spec(package_spec) == package_spec


@pytest.mark.parametrize(
    "package_spec",
    [
        "mini-code-agent-langgraph>=0.5.0",
        "git+https://example.invalid/repo.git@main",
        "https://example.invalid/mca.whl",
        "/tmp/mca.whl",
    ],
)
def test_mutable_candidate_package_specs_are_rejected(package_spec: str) -> None:
    with pytest.raises(ValueError, match="must be an exact name==version"):
        validate_package_spec(package_spec)
