"""Stable Git-local project identity for workspace-scoped memory."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import uuid
from pathlib import Path

IDENTITY_CONFIG_KEY = "mca.memoryIdentity"


class GitProjectIdentityProvider:
    """Keep identity in local Git config so moving the checkout preserves memory."""

    def identity_sha256(self, project: Path, *, create: bool) -> str:
        root = Path(project).expanduser().resolve()
        git = shutil.which("git")
        if not git:
            raise RuntimeError("git is required for stable project identity")
        read = subprocess.run(
            [git, "config", "--local", "--get", IDENTITY_CONFIG_KEY],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        identity = read.stdout.strip()
        if read.returncode not in {0, 1}:
            raise RuntimeError(read.stderr.strip() or "could not read project identity")
        if not identity:
            if not create:
                return hashlib.sha256(str(root).encode()).hexdigest()
            identity = uuid.uuid4().hex
            written = subprocess.run(
                [git, "config", "--local", IDENTITY_CONFIG_KEY, identity],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if written.returncode != 0:
                raise RuntimeError(
                    written.stderr.strip() or "could not persist project identity"
                )
        if not re.fullmatch(r"[0-9a-f]{32}", identity):
            raise RuntimeError("project memory identity in Git config is invalid")
        return hashlib.sha256(f"mca-project:{identity}".encode()).hexdigest()
