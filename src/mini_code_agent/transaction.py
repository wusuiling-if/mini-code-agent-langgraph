from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from mini_code_agent.locking import exclusive_file_lock
from mini_code_agent.receipt import (
    issue_receipt,
    load_receipt,
    validate_receipt,
)
from mini_code_agent.utils import MAX_STATE_FILE_BYTES, atomic_write_text, write_json
from mini_code_agent.workspace import WorkspaceSnapshot


TRANSACTION_VERSION = 1
TRANSACTION_ID = re.compile(r"^[0-9a-f]{24}$")
class TransactionError(RuntimeError):
    """The transaction cannot safely make the requested state transition."""


def _private_directory(path: Path) -> Path:
    path = path.expanduser()
    if path.is_symlink():
        raise TransactionError(f"transaction state path must not be a symlink: {path}")
    path = path.resolve()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.stat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise TransactionError(f"transaction state path is not a directory: {path}")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise PermissionError(f"transaction state path is not owned by this user: {path}")
    if os.name != "nt":
        path.chmod(0o700)
    return path


def _git_executable(source: Path) -> str:
    candidate = shutil.which("git")
    if not candidate:
        raise TransactionError("git is required for transactional runs")
    resolved = str(Path(candidate).resolve())
    try:
        Path(resolved).relative_to(source)
    except ValueError:
        return resolved
    raise TransactionError("refusing to execute git from inside the source workspace")


def _git_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "LANG": "C",
    }


def _is_windows_platform() -> bool:
    return os.name == "nt"


def _git(
    source: Path,
    *args: str,
    check: bool = True,
    input_data: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    command = [
        _git_executable(source),
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.pager=cat",
        *(["-c", "core.autocrlf=true"] if _is_windows_platform() else []),
        *args,
    ]
    result = subprocess.run(
        command,
        cwd=source,
        env=_git_env(),
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise TransactionError(detail or f"git command failed: {' '.join(args)}")
    return result


def _output(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stdout.decode("utf-8", errors="strict").strip()


def _content_snapshot(root: Path) -> WorkspaceSnapshot:
    """Fingerprint user-visible content without worktree control metadata."""

    return WorkspaceSnapshot.capture(root, ignore_paths={root / ".git"})


def _read_private_bytes(path: Path, label: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise TransactionError(f"could not open {label}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise TransactionError(f"{label} must be a regular file")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError(f"{label} is not owned by this user")
        if metadata.st_size > MAX_STATE_FILE_BYTES:
            raise TransactionError(f"{label} exceeds the state size limit")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(MAX_STATE_FILE_BYTES + 1)
        if len(payload) > MAX_STATE_FILE_BYTES:
            raise TransactionError(f"{label} exceeds the state size limit")
        return payload
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class TransactionStore:
    """Persistent transaction metadata and isolated Git worktrees."""

    def __init__(self, state_root: Path):
        self.root = state_root.expanduser().resolve() / "transactions"

    def create(self, source: Path, *, task: str) -> dict[str, Any]:
        source = source.expanduser().resolve()
        if not source.is_dir():
            raise FileNotFoundError(f"source workspace is not a directory: {source}")
        if self.root.is_relative_to(source):
            raise TransactionError(
                "transaction state directory must be outside the source workspace"
            )
        _private_directory(self.root)
        top_level = _output(_git(source, "rev-parse", "--show-toplevel"))
        if Path(top_level).resolve() != source:
            raise TransactionError(
                "transaction --cwd must be the root of its Git worktree"
            )
        if _git(source, "status", "--porcelain", check=True).stdout:
            raise TransactionError(
                "transaction source worktree must be clean at begin"
            )
        baseline_commit = _output(_git(source, "rev-parse", "HEAD"))
        baseline = WorkspaceSnapshot.capture(source)
        baseline_content = _content_snapshot(source)
        transaction_id = secrets.token_hex(12)
        directory = self.root / transaction_id
        directory.mkdir(mode=0o700)
        workspace = directory / "workspace"
        try:
            _git(source, "worktree", "add", "--detach", str(workspace), baseline_commit)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        manifest: dict[str, Any] = {
            "version": TRANSACTION_VERSION,
            "id": transaction_id,
            "status": "open",
            "source": str(source),
            "baseline_commit": baseline_commit,
            "baseline_fingerprint": baseline.fingerprint,
            "baseline_content_fingerprint": baseline_content.fingerprint,
            "prepared_fingerprint": "",
            "prepared_content_fingerprint": "",
            "prepared_source_fingerprint": "",
            "prepared_patch_sha256": "",
            "receipt_id": "",
            "task": task,
            "created_at_ns": time.time_ns(),
            "updated_at_ns": time.time_ns(),
            "trajectory": str(directory / "trajectory.json"),
            "read_set": [],
            "write_set": [],
            "broad_read": False,
            "broad_write": False,
            "access_log": [],
            "failure": "",
        }
        try:
            source_still_clean = not _git(
                source, "status", "--porcelain", check=True
            ).stdout
            source_unchanged = (
                WorkspaceSnapshot.capture(source).fingerprint == baseline.fingerprint
            )
            head_unchanged = (
                _output(_git(source, "rev-parse", "HEAD")) == baseline_commit
            )
            if not (source_still_clean and source_unchanged and head_unchanged):
                raise TransactionError(
                    "source workspace changed while transaction begin was taking its snapshot"
                )
            overlay_baseline = _content_snapshot(workspace)
            write_json(
                directory / "baseline.json",
                {
                    "source_files": baseline_content.files,
                    "workspace_files": overlay_baseline.files,
                },
            )
            self.save(manifest)
        except BaseException:
            _git(source, "worktree", "remove", "--force", str(workspace), check=False)
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return manifest

    def load(self, transaction_id: str) -> dict[str, Any]:
        directory = self._directory(transaction_id)
        path = directory / "manifest.json"
        data = json.loads(_read_private_bytes(path, "transaction manifest").decode("utf-8"))
        if data.get("version") != TRANSACTION_VERSION or data.get("id") != transaction_id:
            raise TransactionError("transaction manifest identity is invalid")
        return data

    def save(self, manifest: dict[str, Any]) -> None:
        transaction_id = str(manifest.get("id", ""))
        directory = self._directory(transaction_id)
        manifest["updated_at_ns"] = time.time_ns()
        write_json(directory / "manifest.json", manifest)

    def workspace(self, transaction_id: str) -> Path:
        workspace = self._directory(transaction_id) / "workspace"
        if workspace.is_symlink() or not workspace.is_dir():
            raise TransactionError("transaction workspace is missing or invalid")
        return workspace.resolve()

    def trajectory(self, transaction_id: str) -> Path:
        return self._directory(transaction_id) / "trajectory.json"

    @contextmanager
    def locked(self, transaction_id: str) -> Iterator[dict[str, Any]]:
        directory = self._directory(transaction_id)
        lock_path = directory / "lock"
        with exclusive_file_lock(lock_path):
            yield self.load(transaction_id)

    def prepare(self, transaction_id: str, trajectory: dict[str, Any]) -> dict[str, Any]:
        with self.locked(transaction_id) as manifest:
            if manifest["status"] != "open":
                raise TransactionError(
                    f"cannot prepare transaction in {manifest['status']} state"
                )
            workspace = self.workspace(transaction_id)
            snapshot = WorkspaceSnapshot.capture(workspace)
            if trajectory.get("exit_status") != "Submitted":
                manifest["failure"] = "agent did not submit"
                self.save(manifest)
                return manifest
            if (
                trajectory.get("verification_status") != "passed"
                or trajectory.get("verified_fingerprint") != snapshot.fingerprint
            ):
                manifest["failure"] = "submission is not bound to the prepared workspace"
                self.save(manifest)
                return manifest

            untracked = _git(workspace, "ls-files", "--others", "--exclude-standard", "-z").stdout
            paths = [item for item in untracked.split(b"\0") if item]
            if paths:
                decoded = [item.decode("utf-8", errors="strict") for item in paths]
                _git(workspace, "add", "-N", "--", *decoded)
            content_snapshot = _content_snapshot(workspace)
            baseline = self._load_baseline(transaction_id)
            workspace_baseline = WorkspaceSnapshot(
                root=workspace,
                files=dict(baseline["workspace_files"]),
            )
            changed = workspace_baseline.diff(content_snapshot)
            expected_paths = {
                path
                for category in ("created", "deleted", "modified")
                for path in changed[category]
            }
            represented = {
                item.decode("utf-8", errors="strict")
                for item in _git(
                    workspace, "diff", "--name-only", "-z", "HEAD"
                ).stdout.split(b"\0")
                if item
            }
            if represented != expected_paths:
                missing = ", ".join(sorted(expected_paths - represented)) or "unknown"
                manifest["failure"] = (
                    "workspace contains changes the Git patch cannot represent: " + missing
                )
                self.save(manifest)
                return manifest
            patch = _git(workspace, "diff", "--binary", "--no-ext-diff", "HEAD").stdout
            patch_path = self._directory(transaction_id) / "prepared.patch"
            atomic_write_text(
                patch_path,
                patch.decode("utf-8", errors="strict"),
                mode=0o600,
                max_bytes=MAX_STATE_FILE_BYTES,
            )
            manifest["prepared_fingerprint"] = snapshot.fingerprint
            manifest["prepared_content_fingerprint"] = content_snapshot.fingerprint
            expected_source_files = dict(baseline["source_files"])
            for path in changed["deleted"]:
                expected_source_files.pop(path, None)
            for path in changed["created"] + changed["modified"]:
                expected_source_files[path] = content_snapshot.files[path]
            manifest["prepared_source_fingerprint"] = WorkspaceSnapshot(
                root=Path(manifest["source"]), files=expected_source_files
            ).fingerprint
            manifest["prepared_patch_sha256"] = hashlib.sha256(patch).hexdigest()
            manifest["status"] = "prepared"
            manifest["failure"] = ""
            write_json(self.trajectory(transaction_id), trajectory)
            receipt = issue_receipt(
                self.root,
                self._directory(transaction_id),
                manifest,
                trajectory,
            )
            manifest["receipt_id"] = receipt["receipt_id"]
            self.save(manifest)
            return manifest

    def commit(self, transaction_id: str) -> dict[str, Any]:
        with self.locked(transaction_id) as manifest:
            if manifest["status"] != "prepared":
                raise TransactionError(
                    f"cannot commit transaction in {manifest['status']} state"
                )
            source = Path(manifest["source"]).resolve()
            workspace = self.workspace(transaction_id)
            if _output(_git(source, "rev-parse", "HEAD")) != manifest["baseline_commit"]:
                raise TransactionError("source HEAD changed since transaction begin; aborting")
            current_source = WorkspaceSnapshot.capture(source)
            if current_source.fingerprint != manifest["baseline_fingerprint"]:
                raise TransactionError("source workspace changed since transaction begin; aborting")
            if WorkspaceSnapshot.capture(workspace).fingerprint != manifest["prepared_fingerprint"]:
                raise TransactionError("prepared workspace changed after verification; aborting")

            trajectory = json.loads(
                _read_private_bytes(
                    self.trajectory(transaction_id), "transaction trajectory"
                ).decode("utf-8")
            )
            receipt = load_receipt(
                self.root, self._directory(transaction_id), transaction_id
            )
            if receipt["receipt_id"] != manifest["receipt_id"]:
                raise TransactionError("transaction receipt id does not match manifest")
            validate_receipt(receipt, manifest, trajectory)

            patch_path = self._directory(transaction_id) / "prepared.patch"
            patch = _read_private_bytes(patch_path, "prepared patch")
            if hashlib.sha256(patch).hexdigest() != manifest["prepared_patch_sha256"]:
                raise TransactionError("prepared patch integrity check failed")
            if patch:
                checked = _git(
                    source,
                    "apply",
                    "--check",
                    "--whitespace=nowarn",
                    "-",
                    input_data=patch,
                    check=False,
                )
                if checked.returncode != 0:
                    detail = checked.stderr.decode("utf-8", errors="replace").strip()
                    raise TransactionError(detail or "prepared patch no longer applies")
                _git(source, "apply", "--whitespace=nowarn", "-", input_data=patch)
            committed = _content_snapshot(source)
            if committed.fingerprint != manifest["prepared_source_fingerprint"]:
                raise TransactionError("committed workspace does not match prepared state")
            manifest["status"] = "committed"
            manifest["committed_at_ns"] = time.time_ns()
            self.save(manifest)
        self._remove_workspace(transaction_id, source)
        return self.load(transaction_id)

    def abort(self, transaction_id: str) -> dict[str, Any]:
        with self.locked(transaction_id) as manifest:
            if manifest["status"] == "committed":
                raise TransactionError("a committed transaction cannot be aborted")
            if manifest["status"] != "aborted":
                manifest["status"] = "aborted"
                manifest["aborted_at_ns"] = time.time_ns()
                self.save(manifest)
            source = Path(manifest["source"]).resolve()
        self._remove_workspace(transaction_id, source)
        return self.load(transaction_id)

    def receipt(self, transaction_id: str) -> dict[str, Any]:
        manifest = self.load(transaction_id)
        if not manifest.get("receipt_id"):
            raise TransactionError("transaction has no prepared receipt")
        receipt = load_receipt(
            self.root, self._directory(transaction_id), transaction_id
        )
        if receipt["receipt_id"] != manifest.get("receipt_id"):
            raise TransactionError("transaction receipt id does not match manifest")
        return receipt

    def _remove_workspace(self, transaction_id: str, source: Path) -> None:
        workspace = self._directory(transaction_id) / "workspace"
        if workspace.exists():
            result = _git(source, "worktree", "remove", "--force", str(workspace), check=False)
            if result.returncode != 0:
                raise TransactionError(
                    result.stderr.decode("utf-8", errors="replace").strip()
                    or "could not remove transaction worktree"
                )

    def _load_baseline(self, transaction_id: str) -> dict[str, dict[str, str]]:
        path = self._directory(transaction_id) / "baseline.json"
        data = json.loads(_read_private_bytes(path, "transaction baseline").decode("utf-8"))
        if not isinstance(data, dict) or not all(
            isinstance(data.get(key), dict)
            and all(
                isinstance(path, str) and isinstance(digest, str)
                for path, digest in data[key].items()
            )
            for key in ("source_files", "workspace_files")
        ):
            raise TransactionError("transaction baseline is malformed")
        return data

    def _directory(self, transaction_id: str) -> Path:
        if not TRANSACTION_ID.fullmatch(transaction_id):
            raise TransactionError("transaction id must be 24 lowercase hex characters")
        directory = self.root / transaction_id
        if directory.is_symlink():
            raise TransactionError("transaction directory must not be a symlink")
        if not directory.exists():
            raise FileNotFoundError(f"transaction does not exist: {transaction_id}")
        if not directory.is_dir():
            raise TransactionError("transaction path is not a directory")
        return directory
