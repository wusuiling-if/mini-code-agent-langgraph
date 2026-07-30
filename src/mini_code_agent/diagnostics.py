"""Read-only environment diagnostics for Mini Code Agent."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal


DiagnosticStatus = Literal["pass", "warn", "fail"]
SandboxMode = Literal["auto", "sandbox-exec", "bwrap", "docker", "none"]
Provider = Literal["auto", "deepseek", "openai"]

_SANDBOX_BACKENDS = ("sandbox-exec", "bwrap", "docker")
_PROVIDER_KEYS = {
    "auto": ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "MCA_API_KEY"),
    "deepseek": ("DEEPSEEK_API_KEY", "MCA_API_KEY"),
    "openai": ("OPENAI_API_KEY", "MCA_API_KEY"),
}


@dataclass(frozen=True)
class DiagnosticCheck:
    """One stable, human-readable diagnostic result."""

    name: str
    status: DiagnosticStatus
    detail: str


def _python_check() -> DiagnosticCheck:
    version = tuple(sys.version_info[:3])
    rendered = ".".join(str(part) for part in version)
    if version >= (3, 10):
        return DiagnosticCheck("python", "pass", f"Python {rendered} is supported")
    return DiagnosticCheck("python", "fail", f"Python {rendered} is unsupported; requires >= 3.10")


def _package_check() -> DiagnosticCheck:
    try:
        installed = version("mini-code-agent-langgraph")
    except PackageNotFoundError:
        return DiagnosticCheck(
            "package",
            "warn",
            "package metadata is unavailable; install the project to report a version",
        )
    return DiagnosticCheck("package", "pass", f"mini-code-agent-langgraph {installed}")


def _cwd_check(cwd: str | os.PathLike[str]) -> DiagnosticCheck:
    try:
        path = Path(cwd).expanduser()
        metadata = path.stat()
        resolved = path.resolve()
    except (OSError, TypeError, ValueError) as exc:
        return DiagnosticCheck("cwd", "fail", f"workspace is unavailable: {exc}")
    if not stat.S_ISDIR(metadata.st_mode):
        return DiagnosticCheck("cwd", "fail", f"workspace is not a directory: {resolved}")
    return DiagnosticCheck("cwd", "pass", f"workspace directory exists: {resolved}")


def _git_check() -> DiagnosticCheck:
    try:
        executable = shutil.which("git")
    except OSError as exc:
        return DiagnosticCheck("git", "fail", f"could not inspect PATH for git: {exc}")
    if executable is None:
        return DiagnosticCheck("git", "fail", "git executable is missing from PATH")
    return DiagnosticCheck("git", "pass", f"git executable is available: {executable}")


def _default_state_dir() -> Path:
    override = os.getenv("MCA_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        root = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return root / "mini-code-agent"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "mini-code-agent" / "state"
    root = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "mini-code-agent"


def _default_config_dir() -> Path:
    override = os.getenv("MCA_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        root = Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming"))
        return root / "mini-code-agent"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "mini-code-agent"
    root = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "mini-code-agent"


def _nearest_existing_directory(path: Path) -> tuple[Path | None, str | None]:
    candidate = path
    while True:
        try:
            metadata = candidate.stat()
        except FileNotFoundError:
            parent = candidate.parent
            if parent == candidate:
                return None, f"no existing parent found for {path}"
            candidate = parent
            continue
        except OSError as exc:
            return None, f"cannot inspect {candidate}: {exc}"
        if not stat.S_ISDIR(metadata.st_mode):
            return None, f"nearest existing path is not a directory: {candidate}"
        return candidate, None


def _writable_location_check(name: str, path: Path) -> DiagnosticCheck:
    path = path.expanduser()
    if path.is_symlink():
        return DiagnosticCheck(name, "fail", f"location must not be a symlink: {path}")
    parent, error = _nearest_existing_directory(path)
    if parent is None:
        return DiagnosticCheck(name, "fail", error or f"location is unavailable: {path}")
    if not os.access(parent, os.W_OK | os.X_OK):
        return DiagnosticCheck(
            name,
            "fail",
            f"nearest existing directory is not writable: {parent} (target: {path})",
        )
    return DiagnosticCheck(
        name,
        "pass",
        f"nearest existing directory is writable: {parent} (target: {path})",
    )


def _env_file_check(path: Path, *, explicit: bool) -> DiagnosticCheck:
    path = path.expanduser()
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        status: DiagnosticStatus = "fail" if explicit else "warn"
        return DiagnosticCheck("env", status, f"env file is not present: {path}")
    except OSError as exc:
        return DiagnosticCheck("env", "fail", f"cannot inspect env file metadata: {exc}")

    if stat.S_ISLNK(metadata.st_mode):
        return DiagnosticCheck("env", "fail", f"env file must not be a symlink: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        return DiagnosticCheck("env", "fail", f"env file is not a regular file: {path}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        return DiagnosticCheck("env", "fail", f"env file is not owned by the current user: {path}")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        return DiagnosticCheck("env", "fail", f"env file permissions are broader than 0600: {path}")
    return DiagnosticCheck("env", "pass", f"private env file metadata is valid: {path}; contents not read")


def _provider_check(provider: str) -> DiagnosticCheck:
    keys = _PROVIDER_KEYS.get(provider)
    if keys is None:
        return DiagnosticCheck("provider", "fail", f"unsupported provider: {provider}")
    present = any(bool(os.getenv(name)) for name in keys)
    label = "a supported provider" if provider == "auto" else provider
    if present:
        return DiagnosticCheck("provider", "pass", f"{label} key is present in the environment")
    status: DiagnosticStatus = "warn" if provider == "auto" else "fail"
    return DiagnosticCheck("provider", status, f"{label} key is missing from the environment")


def _sandbox_check(sandbox: str) -> DiagnosticCheck:
    if sandbox == "none":
        return DiagnosticCheck("sandbox", "warn", "sandbox isolation is explicitly disabled")
    if sandbox == "auto":
        backends = ("docker",) if os.name == "nt" else _SANDBOX_BACKENDS
        for backend in backends:
            try:
                executable = shutil.which(backend)
            except OSError as exc:
                return DiagnosticCheck("sandbox", "fail", f"could not inspect PATH: {exc}")
            if executable is not None:
                return DiagnosticCheck(
                    "sandbox",
                    "pass",
                    f"auto sandbox backend is available: {backend} ({executable})",
                )
        return DiagnosticCheck(
            "sandbox",
            "fail",
            "no sandbox backend is available on PATH (checked "
            + ", ".join(backends)
            + ")",
        )
    if sandbox not in _SANDBOX_BACKENDS:
        return DiagnosticCheck("sandbox", "fail", f"unsupported sandbox mode: {sandbox}")
    if os.name == "nt" and sandbox != "docker":
        return DiagnosticCheck(
            "sandbox",
            "fail",
            f"{sandbox} is not supported on native Windows; use docker or none",
        )
    try:
        executable = shutil.which(sandbox)
    except OSError as exc:
        return DiagnosticCheck("sandbox", "fail", f"could not inspect PATH for {sandbox}: {exc}")
    if executable is None:
        return DiagnosticCheck("sandbox", "fail", f"{sandbox} executable is missing from PATH")
    return DiagnosticCheck("sandbox", "pass", f"{sandbox} executable is available: {executable}")


def run_diagnostics(
    cwd: str | os.PathLike[str],
    sandbox: SandboxMode,
    provider: Provider,
    state_dir: str | os.PathLike[str] | None = None,
    config_dir: str | os.PathLike[str] | None = None,
    env_file: str | os.PathLike[str] | None = None,
) -> list[DiagnosticCheck]:
    """Inspect runtime prerequisites without creating paths or reading secrets."""

    resolved_state_dir = Path(state_dir) if state_dir is not None else _default_state_dir()
    resolved_config_dir = Path(config_dir) if config_dir is not None else _default_config_dir()
    resolved_env_file = Path(env_file) if env_file is not None else resolved_config_dir / "env"

    return [
        _package_check(),
        _python_check(),
        _cwd_check(cwd),
        _git_check(),
        _writable_location_check("state", resolved_state_dir),
        _writable_location_check("config", resolved_config_dir),
        _env_file_check(resolved_env_file, explicit=env_file is not None),
        _provider_check(provider),
        _sandbox_check(sandbox),
    ]


__all__ = ["DiagnosticCheck", "run_diagnostics"]
