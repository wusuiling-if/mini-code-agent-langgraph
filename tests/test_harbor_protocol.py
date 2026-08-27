from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.harbor.baseline_transport import (
    STREAMING_MODEL_MODULE,
    build_streaming_model_install_command,
)
from benchmarks.harbor.launch import (
    build_execution_environment,
    build_harbor_command,
    load_protocol,
    main,
    normalize_task_ref,
    resolve_harbor_executable,
    validate_package_spec,
)
from benchmarks.harbor.preflight import run_transport_preflight
from benchmarks.harbor.protocol import (
    HarborRunConfig,
    build_agent_install_command,
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
    assert '. "$HOME/.local/bin/env"' in command
    assert "--memory off" in command
    assert 'resume_count" -lt 3' in command
    assert "resume_backoff=$((resume_count * 10))" in command
    assert "recovery: attempt=%s backoff_seconds=%s" in command
    assert "recovery: attempt=%s status=%s duration_seconds=%s" in command
    assert 'mca tx resume "$transaction_id"' in command
    assert "eval " not in command
    assert "--sandbox none" in command
    assert "--allow-shell" not in command
    assert "git diff --check" in command
    assert "/tests/test.sh" not in command
    assert 'mca tx commit "$transaction_id"' in command
    assert "https://model.invalid/v1" in command
    assert command.count("--streaming") == 2
    assert command.count("--reasoning-effort low") == 2


def test_shell_is_an_explicit_benchmark_ablation() -> None:
    command = build_transaction_command(
        "Fix it",
        "deepseek/fixed-model",
        config=HarborRunConfig(allow_shell=True),
    )
    assert "--allow-shell" in command


def test_agent_install_reuses_pinned_image_uv_before_downloading() -> None:
    command = build_agent_install_command("mini-code-agent-langgraph==0.5.0")
    first_load = command.index('. "$HOME/.local/bin/env"')
    availability_check = command.index("command -v uv")

    assert first_load < availability_check
    assert "https://astral.sh/uv/0.7.13/install.sh" in command
    assert "mini-code-agent-langgraph==0.5.0" in command
    assert "for install_attempt in $(seq 1 3)" in command


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
        "model_attempts": 0,
        "model_failures": 0,
    }


def test_protocol_builds_paired_fixed_model_commands(tmp_path: Path) -> None:
    protocol = load_protocol()
    assert protocol["status"] == "transport-hardening-ready-for-rerun"
    assert protocol["comparison"]["model"] == "openai/gpt-5.6-sol"
    assert protocol["comparison"]["base_url"] == "https://api.dstopology.com/v1"
    assert "model_attempts" in protocol["metrics"]
    assert "model_failures" in protocol["metrics"]
    assert "transaction_resume_count" in protocol["metrics"]
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
        selected = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--include-task-name"
        ]
        assert all(name.startswith("swe-bench/") for name in selected)
        assert command[command.index("--dataset") + 1] == protocol["dataset"]["ref"]
        assert command[command.index("--model") + 1] == "openai/gpt-5.6-sol"
        assert command[command.index("--n-attempts") + 1] == "1"
        assert command[command.index("--max-retries") + 1] == "0"
        assert "BASH_ENV=/root/.local/bin/env" in command
        assert "UV_CONCURRENT_DOWNLOADS=1" in command
        assert "UV_HTTP_TIMEOUT=120" in command
        assert command.count("--agent-env") == 3
        assert command.count("--verifier-env") == 2
    assert "benchmarks.harbor.baseline_agent:StreamingMiniSweAgent" in baseline
    assert "version=2.1.0" in baseline
    assert "streaming=true" in baseline
    assert "reasoning_effort=low" in baseline
    assert "benchmarks.harbor.mca_agent:MiniCodeAgentHarborAdapter" in candidate
    assert "package_spec=mini-code-agent-langgraph==0.5.0" in candidate
    assert "allow_shell=true" in candidate
    assert "resume_attempts=3" in candidate
    assert "resume_backoff_seconds=10" in candidate
    assert "streaming=true" in candidate
    assert "reasoning_effort=low" in candidate


def test_baseline_streaming_shim_aggregates_chat_completion_chunks() -> None:
    command = build_streaming_model_install_command()

    assert "mini-swe-agent/bin/python" in command
    assert "mca_streaming_litellm" in command
    assert "stream_chunk_builder" in STREAMING_MODEL_MODULE
    assert 'options["stream"] = True' in STREAMING_MODEL_MODULE


def test_long_context_transport_preflight_requires_streamed_tool_call() -> None:
    observed: dict[str, object] = {}

    class Completions:
        def create(self, **kwargs):
            observed.update(kwargs)
            function = SimpleNamespace(name="list_files", arguments='{"path":"."}')
            tool_call = SimpleNamespace(function=function)
            delta = SimpleNamespace(tool_calls=[tool_call])
            first = SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=delta)])
            usage = SimpleNamespace(prompt_tokens=6100, completion_tokens=12)
            final = SimpleNamespace(usage=usage, choices=[])
            return iter((first, final))

    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=Completions())

    def client_factory(**kwargs):
        observed["client"] = kwargs
        return Client()

    times = iter((10.0, 10.5, 11.0))
    result = run_transport_preflight(
        load_protocol(),
        {"OPENAI_API_KEY": "secret"},
        client_factory=client_factory,
        clock=lambda: next(times),
    )

    assert result["status"] == "passed"
    assert result["tool"] == "list_files"
    assert result["first_chunk_seconds"] == 0.5
    assert result["total_seconds"] == 1.0
    assert result["input_tokens"] == 6100
    assert observed["stream"] is True
    assert observed["reasoning_effort"] == "low"
    assert observed["model"] == "gpt-5.6-sol"
    assert observed["client"] == {
        "api_key": "secret",
        "base_url": "https://api.dstopology.com/v1",
        "timeout": 60,
        "max_retries": 0,
    }


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
        assert f"swe-bench/{task_name}" in command


def test_task_reference_normalization_matches_harbor_filter_contract() -> None:
    assert normalize_task_ref("matplotlib__matplotlib-14623") == (
        "swe-bench/matplotlib__matplotlib-14623"
    )
    assert normalize_task_ref("swe-bench/matplotlib__matplotlib-14623") == (
        "swe-bench/matplotlib__matplotlib-14623"
    )
    with pytest.raises(ValueError, match="unexpected pilot task reference"):
        normalize_task_ref("other/task")


def test_harbor_executable_falls_back_to_active_python_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    python = tmp_path / "bin" / "python"
    harbor = python.with_name("harbor")
    harbor.parent.mkdir(parents=True)
    harbor.touch()
    monkeypatch.setattr("benchmarks.harbor.launch.shutil.which", lambda _name: None)
    monkeypatch.setattr("benchmarks.harbor.launch.sys.executable", str(python))

    assert resolve_harbor_executable() == str(harbor)


def test_selected_tasks_must_belong_to_pinned_pilot(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside the pinned pilot"):
        build_harbor_command(
            "baseline",
            "openai/gpt-5.6-sol",
            tmp_path / "baseline",
            task_names=["unknown__task-1"],
        )


def test_execution_environment_routes_both_arms_through_pinned_endpoint() -> None:
    env = build_execution_environment(
        load_protocol(),
        {"OPENAI_API_KEY": "secret", "PYTHONPATH": "/existing/modules"},
    )
    assert env["OPENAI_API_KEY"] == "secret"
    assert env["MCA_API_KEY"] == "secret"
    assert env["OPENAI_BASE_URL"] == "https://api.dstopology.com/v1"
    assert env["MCA_BASE_URL"] == "https://api.dstopology.com/v1"
    assert env["DOCKER_DEFAULT_PLATFORM"] == "linux/amd64"
    project_root, existing = env["PYTHONPATH"].split(os.pathsep, 1)
    assert Path(project_root) == Path(__file__).resolve().parents[1]
    assert existing == "/existing/modules"


def test_execution_environment_requires_a_provider_key() -> None:
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY or MCA_API_KEY"):
        build_execution_environment(load_protocol(), {})


def test_paid_execution_requires_an_explicit_candidate_package() -> None:
    with pytest.raises(RuntimeError, match="explicit immutable --package-spec"):
        main(["--model", "openai/gpt-5.6-sol", "--smoke", "--execute"])


def test_failed_transport_preflight_blocks_all_paid_harbor_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[object] = []
    monkeypatch.setenv("OPENAI_API_KEY", "secret")

    def fail_preflight(*_args, **_kwargs):
        raise RuntimeError("preflight failed")

    monkeypatch.setattr(
        "benchmarks.harbor.launch.run_transport_preflight",
        fail_preflight,
    )
    monkeypatch.setattr(
        "benchmarks.harbor.launch.subprocess.run",
        lambda *args, **_kwargs: launched.append(args),
    )

    with pytest.raises(RuntimeError, match="preflight failed"):
        main(
            [
                "--model",
                "openai/gpt-5.6-sol",
                "--smoke",
                "--package-spec",
                "mini-code-agent-langgraph==0.5.0",
                "--execute",
            ]
        )

    assert launched == []


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
