from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path

import pytest

import mini_code_agent.cli as cli_module


HEAVY_MODULES = {
    "langchain_core",
    "langchain_deepseek",
    "langchain_openai",
    "langgraph",
}
DEEPSEEK_KEY_NAME = "DEEPSEEK" + "_API_KEY"
OPENAI_KEY_NAME = "OPENAI" + "_API_KEY"


def _imported_top_level_modules(code: str) -> set[str]:
    return {name.partition(".")[0] for name in _imported_modules(code)}


def _imported_modules(code: str) -> set[str]:
    result = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return set(result.stdout.strip().splitlines()[-1].split(","))


def test_build_parser_does_not_import_heavy_runtime():
    modules = _imported_top_level_modules(
        "import sys; "
        "import mini_code_agent.cli as cli; "
        "cli.build_parser(); "
        "print(','.join(sorted({name.partition('.')[0] for name in sys.modules})))"
    )

    assert modules.isdisjoint(HEAVY_MODULES)


def test_parser_accepts_nested_sandbox_probe():
    args = cli_module.build_parser().parse_args(
        ["sandbox", "probe", "--sandbox", "docker"]
    )

    assert args.command == "sandbox"
    assert args.sandbox_command == "probe"
    assert args.sandbox == "docker"


@pytest.mark.parametrize("command", ["status", "receipt", "commit", "abort"])
def test_parser_accepts_transaction_state_commands(command: str):
    args = cli_module.build_parser().parse_args(["tx", command, "0" * 24])

    assert args.command == "tx"
    assert args.transaction_command == command
    assert args.transaction_id == "0" * 24


def test_parser_accepts_transaction_run_and_resume():
    parser = cli_module.build_parser()
    run = parser.parse_args(
        [
            "tx",
            "run",
            "fix it",
            "--model",
            "deepseek",
            "--test-command",
            "pytest -q",
        ]
    )
    resume = parser.parse_args(
        [
            "tx",
            "resume",
            "0" * 24,
            "--model",
            "deepseek",
            "--test-command",
            "pytest -q",
        ]
    )

    assert run.transaction_command == "run"
    assert run.task == "fix it"
    assert resume.transaction_command == "resume"
    assert resume.transaction_id == "0" * 24


def test_parser_accepts_transaction_demo():
    args = cli_module.build_parser().parse_args(["tx", "demo"])

    assert args.command == "tx"
    assert args.transaction_command == "demo"


def test_parser_accepts_login_and_logout_with_optional_provider():
    parser = cli_module.build_parser()

    assert parser.parse_args(["login", "deepseek"]).provider == "deepseek"
    assert parser.parse_args(["logout", "openai"]).provider == "openai"
    assert parser.parse_args(["login"]).provider is None


def test_login_prompts_without_echo_and_saves_private_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    class TtyInput:
        @staticmethod
        def isatty() -> bool:
            return True

    config = tmp_path / "config"
    secret = "test-secret-value"
    monkeypatch.setenv("MCA_CONFIG_DIR", str(config))
    monkeypatch.setattr(cli_module.sys, "stdin", TtyInput())
    monkeypatch.setattr(cli_module.getpass, "getpass", lambda _prompt: secret)

    result = cli_module.login_command(
        cli_module.build_parser().parse_args(["login", "deepseek"])
    )

    env_file = config / "env"
    assert result == 0
    assert env_file.read_text(encoding="utf-8") == f"{DEEPSEEK_KEY_NAME}={secret}\n"
    if os.name != "nt":
        assert env_file.stat().st_mode & 0o777 == 0o600
    assert secret not in capsys.readouterr().out


def test_login_without_provider_selects_one_in_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class TtyInput:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setenv("MCA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(cli_module.sys, "stdin", TtyInput())
    monkeypatch.setattr("builtins.input", lambda _prompt: "2")
    monkeypatch.setattr(cli_module.getpass, "getpass", lambda _prompt: "openai-secret")

    result = cli_module.login_command(cli_module.build_parser().parse_args(["login"]))

    assert result == 0
    content = (tmp_path / "config" / "env").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" in content


def test_logout_without_saved_credentials_does_not_create_a_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = tmp_path / "config"
    monkeypatch.setenv("MCA_CONFIG_DIR", str(config))

    result = cli_module.logout_command(
        cli_module.build_parser().parse_args(["logout", "openai"])
    )

    assert result == 0
    assert not config.exists()


def test_login_updates_existing_provider_and_logout_removes_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config = tmp_path / "config"
    config.mkdir()
    env_file = config / "env"
    env_file.write_text(
        f"{DEEPSEEK_KEY_NAME}=old-secret\n"
        f"{DEEPSEEK_KEY_NAME}=duplicate-secret\n"
        f"# {OPENAI_KEY_NAME}=\nMCA_BASE_URL=http://localhost/v1\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.setenv("MCA_CONFIG_DIR", str(config))

    cli_module._write_config_value("DEEPSEEK_API_KEY", "new-secret")
    cli_module._write_config_value("DEEPSEEK_API_KEY", None)

    content = env_file.read_text(encoding="utf-8")
    assert "old-secret" not in content
    assert "duplicate-secret" not in content
    assert "new-secret" not in content
    assert content.count(DEEPSEEK_KEY_NAME) == 1
    assert f"# {DEEPSEEK_KEY_NAME}=" in content
    assert "MCA_BASE_URL=http://localhost/v1" in content


def test_model_startup_prompts_for_missing_provider_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class TtyInput:
        @staticmethod
        def isatty() -> bool:
            return True

    captured: dict[str, object] = {}

    def fake_create_model(model: str, **kwargs):
        captured.update(model=model, **kwargs)
        return object()

    monkeypatch.setenv("MCA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("MCA_API_KEY", raising=False)
    monkeypatch.setattr(cli_module.sys, "stdin", TtyInput())
    monkeypatch.setattr(cli_module.getpass, "getpass", lambda _prompt: "entered-key")
    monkeypatch.setattr(cli_module, "_load_create_model", lambda: fake_create_model)
    args = cli_module.build_parser().parse_args(["chat", "--model", "deepseek"])

    cli_module._model_from_args(args)

    assert captured["api_key"] == "entered-key"
    assert os.getenv("DEEPSEEK_API_KEY") is None
    assert (tmp_path / "config" / "env").exists()


def test_transaction_demo_proves_commit_and_conflict_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    from mini_code_agent import transaction_cli

    root = tmp_path / "transaction-demo"
    root.mkdir()
    monkeypatch.setattr(cli_module, "_create_demo_workspace", lambda: root)

    result = transaction_cli.demo_command(
        cli_module.build_parser().parse_args(["tx", "demo"])
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "success.source_unchanged_before_commit: true" in output
    assert "success.commit: committed" in output
    assert "conflict.commit: refused" in output
    assert "conflict.user_change_preserved: true" in output


def test_parser_rejects_none_for_sandbox_probe():
    with pytest.raises(SystemExit):
        cli_module.build_parser().parse_args(
            ["sandbox", "probe", "--sandbox", "none"]
        )


def test_sandbox_probe_command_renders_checks_and_failure_exit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    import mini_code_agent.sandbox_probe as probe_module
    from mini_code_agent.sandbox_probe import SandboxCheck, SandboxProbeReport

    monkeypatch.setattr(
        probe_module,
        "run_sandbox_probe",
        lambda **_kwargs: SandboxProbeReport(
            backend="fake",
            checks=(
                SandboxCheck("workspace_write", True, "allowed"),
                SandboxCheck("network", False, "reachable"),
            ),
        ),
    )
    args = cli_module.build_parser().parse_args(["sandbox", "probe"])

    result = cli_module.sandbox_probe_command(args)

    assert result == 1
    assert capsys.readouterr().out.splitlines() == [
        "[PASS] workspace_write: allowed",
        "[FAIL] network: reachable",
    ]


def test_help_does_not_import_agent_runtime():
    modules = _imported_modules(
        "import sys\n"
        "import mini_code_agent.cli as cli\n"
        "sys.argv = ['mca', '--help']\n"
        "try:\n"
        "    cli.main()\n"
        "except SystemExit as exc:\n"
        "    if exc.code != 0:\n"
        "        raise\n"
        "print(','.join(sorted(sys.modules)))"
    )

    assert modules.isdisjoint(
        HEAVY_MODULES | {"mini_code_agent.agent", "mini_code_agent.workspace"}
    )


def test_mock_model_does_not_import_provider_adapters():
    modules = _imported_top_level_modules(
        "import sys; "
        "from mini_code_agent.model import create_model; "
        "create_model('mock'); "
        "print(','.join(sorted({name.partition('.')[0] for name in sys.modules})))"
    )

    assert modules.isdisjoint({"langchain_openai", "langchain_deepseek"})


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


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["run", "task", "--model", "deepseek"], []),
        (["chat"], []),
        (
            [
                "chat",
                "--check",
                "tests",
                "pytest -q",
                "--check",
                "lint",
                "ruff check .",
            ],
            [("tests", "pytest -q"), ("lint", "ruff check .")],
        ),
    ],
)
def test_cli_omitted_and_repeated_checks_keep_the_same_configuration_contract(
    argv: list[str], expected: list[tuple[str, str]]
):
    args = cli_module.build_parser().parse_args(argv)

    combined, explicit = cli_module._configured_verification_checks(
        args, required=False
    )

    assert [(item.name, item.command) for item in combined] == expected
    assert [(item.name, item.command) for item in explicit] == expected


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


def test_run_rejects_scripted_mock_before_runtime_setup():
    args = cli_module.build_parser().parse_args(
        ["run", "task", "--model", "mock", "--test-command", "pytest -q"]
    )

    with pytest.raises(RuntimeError, match="mca demo"):
        cli_module.run_agent(args)


def test_trajectory_helpers_do_not_import_model_message_runtime():
    modules = _imported_top_level_modules(
        "import sys; "
        "import mini_code_agent.trajectory; "
        "print(','.join(sorted({name.partition('.')[0] for name in sys.modules})))"
    )

    assert "langchain_core" not in modules


@pytest.mark.parametrize(
    "command_setup",
    [
        "sys.argv = ['mca', 'doctor', '--cwd', tempfile.gettempdir(), '--sandbox', 'none']",
        (
            "path = pathlib.Path(tempfile.mkstemp(suffix='.traj.json')[1]); "
            "path.write_text('{\"events\": []}', encoding='utf-8'); "
            "sys.argv = ['mca', 'trace', str(path)]"
        ),
    ],
)
def test_informational_command_dispatch_stays_free_of_llm_runtime(command_setup: str):
    modules = _imported_top_level_modules(
        "import pathlib, sys, tempfile\n"
        "import mini_code_agent.cli as cli\n"
        f"{command_setup}\n"
        "try:\n"
        "    cli.main()\n"
        "except SystemExit:\n"
        "    pass\n"
        "print(','.join(sorted({name.partition('.')[0] for name in sys.modules})))"
    )

    assert modules.isdisjoint(HEAVY_MODULES)


def test_cli_reports_package_version_without_a_subcommand():
    result = subprocess.run(
        [sys.executable, "-m", "mini_code_agent", "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "mca 0.4.0"
    assert result.stderr == ""


def test_demo_fixes_a_temporary_fixture_without_dirtying_the_tracked_example(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    demo_root = tmp_path / "mca-demo"
    tracked_example = Path(__file__).parents[1] / "examples" / "calculator_bug" / "calculator.py"
    tracked_before = tracked_example.read_text(encoding="utf-8")
    monkeypatch.setattr(cli_module, "_create_demo_workspace", lambda: demo_root)

    args = cli_module.build_parser().parse_args(["demo"])
    result = cli_module.demo_command(args)

    assert result == 0
    assert "return a + b" in (demo_root / "calculator.py").read_text(encoding="utf-8")
    assert tracked_example.read_text(encoding="utf-8") == tracked_before
    output = capsys.readouterr().out
    assert f"demo_workspace: {demo_root}" in output
    assert "exit_status: Submitted" in output
    assert "tests: passed" in output
    assert "next: mca trace" in output


def test_demo_rejects_native_windows_with_actionable_guidance(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(cli_module, "_platform_supports_demo", lambda: False)

    with pytest.raises(RuntimeError, match="WSL2 or Linux"):
        cli_module.demo_command(cli_module.build_parser().parse_args(["demo"]))


def test_doctor_runs_without_a_key_when_only_optional_capabilities_warn(tmp_path: Path):
    env = os.environ.copy()
    for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "MCA_API_KEY"):
        env.pop(name, None)
    env["MCA_STATE_DIR"] = str(tmp_path / "state")
    env["MCA_CONFIG_DIR"] = str(tmp_path / "config")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mini_code_agent",
            "doctor",
            "--cwd",
            str(tmp_path),
            "--sandbox",
            "none",
        ],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 0
    assert "[PASS] python:" in result.stdout
    assert "[WARN] provider:" in result.stdout
    assert "[WARN] sandbox:" in result.stdout
    assert "summary:" in result.stdout
    assert result.stderr == ""


def test_demo_ignores_a_tmpdir_inside_the_current_repository(tmp_path: Path):
    if not cli_module.shutil.which("git"):
        pytest.skip("git is required for the dirty-worktree assertion")
    repo = tmp_path / "repo"
    repo.mkdir()
    in_repo_tmp = repo / "tmp"
    in_repo_tmp.mkdir()
    workdir = repo / "nested"
    workdir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    env = os.environ.copy()
    env["TMPDIR"] = str(in_repo_tmp)

    result = subprocess.run(
        [sys.executable, "-m", "mini_code_agent", "demo"],
        cwd=workdir,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )

    assert result.returncode == 0
    assert status.stdout == ""
