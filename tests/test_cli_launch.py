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
    assert result.stdout.strip() == "mca 0.3.0"
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
