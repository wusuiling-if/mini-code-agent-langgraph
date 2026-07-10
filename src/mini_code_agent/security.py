from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


class SecurityError(Exception):
    """Raised when a tool request violates the workspace safety policy."""


class SafeWorkspace:
    def __init__(self, cwd: Path):
        self.cwd = cwd.resolve()

    def resolve(self, path: str | Path) -> Path:
        raw_path = Path(path or ".")
        resolved = (raw_path if raw_path.is_absolute() else self.cwd / raw_path).resolve()
        try:
            resolved.relative_to(self.cwd)
        except ValueError as exc:
            raise SecurityError(f"Path escapes workspace: {path}") from exc
        return resolved


class SecretRedactor:
    SECRET_ENV_NAMES = {
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "MCA_API_KEY",
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
    }
    SECRET_PATTERNS = [
        re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_\-]{8,}"),
        re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
        re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
        re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
        re.compile(r"xox[baprs]-[A-Za-z0-9\-]{20,}"),
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"Bearer\s+[A-Za-z0-9._\-]{12,}", re.IGNORECASE),
    ]

    def __init__(self, extra_secrets: list[str] | None = None):
        env_secrets = [secret for secret in os.getenv("MCA_REDACT", "").split(",") if secret]
        self.secrets = [
            value
            for value in [os.getenv(name) for name in self.SECRET_ENV_NAMES] + env_secrets + (extra_secrets or [])
            if value and len(value) >= 8
        ]

    def redact_text(self, text: str) -> str:
        redacted = text
        for secret in self.secrets:
            redacted = redacted.replace(secret, "[REDACTED_SECRET]")
        for pattern in self.SECRET_PATTERNS:
            redacted = pattern.sub("[REDACTED_SECRET]", redacted)
        return redacted

    def redact_data(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, list):
            return [self.redact_data(item) for item in value]
        if isinstance(value, dict):
            return {key: self.redact_data(item) for key, item in value.items()}
        return value


def load_env_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"env file does not exist: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def is_probably_text_file(path: Path, *, sample_size: int = 2048) -> bool:
    try:
        data = path.read_bytes()[:sample_size]
    except OSError:
        return False
    return b"\x00" not in data
