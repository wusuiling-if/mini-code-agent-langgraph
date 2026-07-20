from __future__ import annotations

import ast
import builtins
import io
import os
import sys
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import mini_code_agent.diagnostics as diagnostics
from mini_code_agent.diagnostics import DiagnosticCheck, run_diagnostics


PROVIDER_KEYS = ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "MCA_API_KEY")


@pytest.fixture(autouse=True)
def clear_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)


def check_named(checks: list[DiagnosticCheck], name: str) -> DiagnosticCheck:
    matches = [check for check in checks if check.name == name]
    assert len(matches) == 1
    return matches[0]


def test_diagnostic_check_is_a_frozen_structured_record() -> None:
    check = DiagnosticCheck(name="python", status="pass", detail="supported")

    assert (check.name, check.status, check.detail) == ("python", "pass", "supported")
    with pytest.raises(FrozenInstanceError):
        check.status = "fail"  # type: ignore[misc]


def test_diagnostics_module_uses_only_the_standard_library() -> None:
    source = Path(diagnostics.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )

    assert imported_roots <= sys.stdlib_module_names | {"__future__"}


def test_diagnostics_report_all_required_checks(tmp_path: Path) -> None:
    checks = run_diagnostics(tmp_path, sandbox="none", provider="auto")

    assert {
        "package",
        "python",
        "cwd",
        "git",
        "state",
        "config",
        "env",
        "provider",
        "sandbox",
    } <= {check.name for check in checks}
    assert check_named(checks, "python").status == "pass"
    assert check_named(checks, "cwd").status == "pass"


def test_package_version_is_reported_without_importing_the_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(diagnostics, "version", lambda _name: "9.8.7")

    checks = run_diagnostics(tmp_path, sandbox="none", provider="auto")

    package = check_named(checks, "package")
    assert package.status == "pass"
    assert package.detail == "mini-code-agent-langgraph 9.8.7"


def test_missing_package_metadata_is_only_a_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def missing(_name: str) -> str:
        raise diagnostics.PackageNotFoundError

    monkeypatch.setattr(diagnostics, "version", missing)

    checks = run_diagnostics(tmp_path, sandbox="none", provider="auto")

    assert check_named(checks, "package").status == "warn"


def test_missing_workspace_is_a_failure(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    checks = run_diagnostics(missing, sandbox="none", provider="auto")

    assert check_named(checks, "cwd").status == "fail"
    assert not missing.exists()


def test_git_availability_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: None)

    checks = run_diagnostics(tmp_path, sandbox="none", provider="auto")

    assert check_named(checks, "git").status == "fail"


def test_state_and_config_check_nearest_existing_parent_without_creating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_parent = tmp_path / "state-parent"
    config_parent = tmp_path / "config-parent"
    state_parent.mkdir()
    config_parent.mkdir()
    state_dir = state_parent / "missing" / "nested"
    config_dir = config_parent / "missing"
    checked_paths: list[Path] = []

    def record_access(path: os.PathLike[str] | str, mode: int) -> bool:
        checked_paths.append(Path(path))
        return True

    monkeypatch.setattr(diagnostics.os, "access", record_access)
    checks = run_diagnostics(
        tmp_path,
        sandbox="none",
        provider="auto",
        state_dir=state_dir,
        config_dir=config_dir,
        env_file=tmp_path / "missing.env",
    )

    assert check_named(checks, "state").status == "pass"
    assert check_named(checks, "config").status == "pass"
    assert state_parent in checked_paths
    assert config_parent in checked_paths
    assert not state_dir.exists()
    assert not config_dir.exists()


def test_unwritable_state_parent_is_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_parent = tmp_path / "state-parent"
    config_parent = tmp_path / "config-parent"
    state_parent.mkdir()
    config_parent.mkdir()

    def fake_access(path: os.PathLike[str] | str, mode: int) -> bool:
        return Path(path) != state_parent

    monkeypatch.setattr(diagnostics.os, "access", fake_access)
    checks = run_diagnostics(
        tmp_path,
        sandbox="none",
        provider="auto",
        state_dir=state_parent / "future",
        config_dir=config_parent / "future",
    )

    assert check_named(checks, "state").status == "fail"
    assert check_named(checks, "config").status == "pass"


def test_env_file_is_inspected_without_opening_or_exposing_contents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "env"
    secret = "sk-do-not-read-or-print"
    env_file.write_text(f"DEEPSEEK_API_KEY={secret}\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "present-in-process")

    def guard_open(original_open: Callable[..., object]) -> Callable[..., object]:
        def guarded_open(file: object, *args: object, **kwargs: object) -> object:
            try:
                opened_path = Path(file).resolve()  # type: ignore[arg-type]
            except (OSError, TypeError):
                opened_path = None
            if opened_path == env_file.resolve():
                raise AssertionError("diagnostics must not open the env file")
            return original_open(file, *args, **kwargs)

        return guarded_open

    monkeypatch.setattr(builtins, "open", guard_open(builtins.open))
    monkeypatch.setattr(io, "open", guard_open(io.open))
    checks = run_diagnostics(
        tmp_path,
        sandbox="none",
        provider="deepseek",
        env_file=env_file,
    )

    assert check_named(checks, "env").status == "pass"
    assert check_named(checks, "package").status == "pass"
    assert secret not in "\n".join(check.detail for check in checks)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not portable")
def test_env_file_with_broad_permissions_is_a_failure(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    env_file.write_text("DEEPSEEK_API_KEY=not-inspected\n", encoding="utf-8")
    env_file.chmod(0o644)

    checks = run_diagnostics(
        tmp_path,
        sandbox="none",
        provider="deepseek",
        env_file=env_file,
    )

    assert check_named(checks, "env").status == "fail"


def test_diagnostics_never_expose_provider_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = "sk-do-not-print"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)

    checks = run_diagnostics(tmp_path, sandbox="none", provider="deepseek")

    rendered = "\n".join(check.detail for check in checks)
    assert secret not in rendered
    assert check_named(checks, "provider").status == "pass"


@pytest.mark.parametrize(
    ("provider", "key_name"),
    [("deepseek", "DEEPSEEK_API_KEY"), ("openai", "OPENAI_API_KEY"), ("auto", "MCA_API_KEY")],
)
def test_provider_accepts_supported_environment_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider: str, key_name: str
) -> None:
    monkeypatch.setenv(key_name, "present")

    checks = run_diagnostics(tmp_path, sandbox="none", provider=provider)

    assert check_named(checks, "provider").status == "pass"


def test_missing_provider_key_is_a_failure(tmp_path: Path) -> None:
    checks = run_diagnostics(tmp_path, sandbox="none", provider="openai")

    provider = check_named(checks, "provider")
    assert provider.status == "fail"
    assert "missing" in provider.detail.lower()


def test_auto_provider_without_a_key_is_a_warning(tmp_path: Path) -> None:
    checks = run_diagnostics(tmp_path, sandbox="none", provider="auto")

    provider = check_named(checks, "provider")
    assert provider.status == "warn"
    assert "missing" in provider.detail.lower()


def test_none_sandbox_is_an_explicit_warning(tmp_path: Path) -> None:
    checks = run_diagnostics(tmp_path, sandbox="none", provider="auto")

    assert check_named(checks, "sandbox").status == "warn"


def test_selected_sandbox_executable_is_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        diagnostics.shutil,
        "which",
        lambda name: "/usr/bin/git" if name == "git" else None,
    )

    checks = run_diagnostics(tmp_path, sandbox="bwrap", provider="auto")

    assert check_named(checks, "sandbox").status == "fail"


def test_auto_sandbox_reports_an_available_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        diagnostics.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"git", "docker"} else None,
    )

    checks = run_diagnostics(tmp_path, sandbox="auto", provider="auto")

    sandbox = check_named(checks, "sandbox")
    assert sandbox.status == "pass"
    assert "docker" in sandbox.detail


def test_auto_sandbox_without_a_backend_is_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        diagnostics.shutil,
        "which",
        lambda name: "/usr/bin/git" if name == "git" else None,
    )

    checks = run_diagnostics(tmp_path, sandbox="auto", provider="auto")

    assert check_named(checks, "sandbox").status == "fail"
