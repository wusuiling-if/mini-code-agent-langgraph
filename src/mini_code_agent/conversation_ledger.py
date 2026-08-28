"""HMAC-chained private JSONL ledgers for conversation evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mini_code_agent.locking import exclusive_file_lock
from mini_code_agent.utils import MAX_STATE_FILE_BYTES, atomic_write_text

LOG_SCHEMA_VERSION = 2
CONVERSATION_KEY_NAME = "conversation.key"
CONVERSATION_LOCK_NAME = "conversation-events.lock"


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ConversationLedgerError(RuntimeError):
    """A private conversation ledger failed structural or HMAC validation."""


class AuthenticatedConversationLedger:
    """Append and verify per-file hash chains under one private local key."""

    def __init__(self, directory: Path, *, create: bool) -> None:
        self.directory = Path(os.path.abspath(Path(directory).expanduser()))
        self.key_path = self.directory / CONVERSATION_KEY_NAME
        self.lock_path = self.directory / CONVERSATION_LOCK_NAME
        if create:
            self._ensure_private_directory(self.directory)
            self.key = self._load_or_create_key()
        else:
            try:
                directory_metadata = self.directory.lstat()
            except FileNotFoundError:
                self.key = b""
            else:
                if stat.S_ISLNK(directory_metadata.st_mode) or not stat.S_ISDIR(
                    directory_metadata.st_mode
                ):
                    raise ConversationLedgerError(
                        "conversation memory path must be a real directory"
                    )
                try:
                    self.key = self._load_existing_key()
                except FileNotFoundError:
                    self.key = b""

    def append(self, path: Path, log_name: str, payload: dict[str, object]) -> None:
        with exclusive_file_lock(self.lock_path):
            self.append_unlocked(path, log_name, payload)

    def append_unlocked(
        self, path: Path, log_name: str, payload: dict[str, object]
    ) -> None:
        if not self.key:
            raise ConversationLedgerError("conversation authentication key is missing")
        last = self._read_last_raw_row(path)
        if last is None:
            sequence = 0
            previous_hmac = ""
        else:
            self._verify_envelope(last, log_name, expected_sequence=None)
            last_sequence = last["sequence"]
            if not isinstance(last_sequence, int):
                raise ConversationLedgerError("conversation ledger sequence is invalid")
            sequence = last_sequence + 1
            previous_hmac = str(last["hmac_sha256"])
        envelope = self._envelope(log_name, sequence, previous_hmac, payload)
        self._append_raw_row(path, envelope)

    def read(self, path: Path, log_name: str) -> list[dict[str, object]]:
        rows = self._read_raw_rows(path)
        previous_hmac = ""
        payloads: list[dict[str, object]] = []
        for sequence, row in enumerate(rows):
            self._verify_envelope(row, log_name, expected_sequence=sequence)
            if str(row["previous_hmac"]) != previous_hmac:
                raise ConversationLedgerError(
                    f"conversation ledger chain mismatch: {path.name}:{sequence}"
                )
            payload = row.get("payload")
            if not isinstance(payload, dict):
                raise ConversationLedgerError("conversation ledger payload is invalid")
            payloads.append(payload)
            previous_hmac = str(row["hmac_sha256"])
        return payloads

    def last_payload(self, path: Path, log_name: str) -> dict[str, object] | None:
        """Read and authenticate only the last envelope of a validated live log."""

        row = self._read_last_raw_row(path)
        if row is None:
            return None
        self._verify_envelope(row, log_name, expected_sequence=None)
        payload = row.get("payload")
        if not isinstance(payload, dict):
            raise ConversationLedgerError("conversation ledger payload is invalid")
        return payload

    def migrate_legacy(
        self,
        path: Path,
        log_name: str,
        validator: Callable[[dict[str, object]], None],
    ) -> bool:
        """Authenticate a self-validating v1 file once, under the write lock."""

        with exclusive_file_lock(self.lock_path):
            rows = self._read_raw_rows(path)
            if not rows:
                return False
            if rows[0].get("schema_version") == LOG_SCHEMA_VERSION:
                self.read(path, log_name)
                return False
            for row in rows:
                validator(row)
            previous_hmac = ""
            envelopes = []
            for sequence, payload in enumerate(rows):
                envelope = self._envelope(log_name, sequence, previous_hmac, payload)
                envelopes.append(envelope)
                previous_hmac = str(envelope["hmac_sha256"])
            text = "".join(f"{canonical_json(row)}\n" for row in envelopes)
            atomic_write_text(
                path,
                text,
                mode=0o600,
                max_bytes=MAX_STATE_FILE_BYTES,
            )
            return True

    def _envelope(
        self,
        log_name: str,
        sequence: int,
        previous_hmac: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        authenticated = {
            "schema_version": LOG_SCHEMA_VERSION,
            "log_name": log_name,
            "sequence": sequence,
            "previous_hmac": previous_hmac,
            "payload": payload,
        }
        return {
            **authenticated,
            "hmac_sha256": hmac.new(
                self.key,
                canonical_json(authenticated).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest(),
        }

    def _verify_envelope(
        self,
        row: dict[str, Any],
        log_name: str,
        *,
        expected_sequence: int | None,
    ) -> None:
        if int(row.get("schema_version", 0)) != LOG_SCHEMA_VERSION:
            raise ConversationLedgerError("unsupported conversation ledger schema")
        if row.get("log_name") != log_name:
            raise ConversationLedgerError("conversation ledger name binding mismatch")
        sequence = row.get("sequence")
        if not isinstance(sequence, int) or sequence < 0:
            raise ConversationLedgerError("conversation ledger sequence is invalid")
        if expected_sequence is not None and sequence != expected_sequence:
            raise ConversationLedgerError(
                "conversation ledger sequence is not contiguous"
            )
        previous_hmac = row.get("previous_hmac")
        payload = row.get("payload")
        recorded_hmac = row.get("hmac_sha256")
        if not isinstance(previous_hmac, str) or not isinstance(payload, dict):
            raise ConversationLedgerError("conversation ledger envelope is invalid")
        if not isinstance(recorded_hmac, str) or len(recorded_hmac) != 64:
            raise ConversationLedgerError("conversation ledger HMAC is invalid")
        authenticated = {
            "schema_version": LOG_SCHEMA_VERSION,
            "log_name": log_name,
            "sequence": sequence,
            "previous_hmac": previous_hmac,
            "payload": payload,
        }
        expected = hmac.new(
            self.key,
            canonical_json(authenticated).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(recorded_hmac, expected):
            raise ConversationLedgerError("conversation ledger authentication failed")

    def _load_or_create_key(self) -> bytes:
        try:
            return self._load_existing_key()
        except FileNotFoundError:
            pass
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        key = secrets.token_bytes(32)
        try:
            descriptor = os.open(self.key_path, flags, 0o600)
        except FileExistsError:
            return self._load_existing_key()
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            os.write(descriptor, key)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return key

    def _load_existing_key(self) -> bytes:
        metadata = self.key_path.lstat()
        self._validate_private_file(self.key_path, metadata)
        key = self.key_path.read_bytes()
        if len(key) != 32:
            raise ConversationLedgerError("conversation authentication key is invalid")
        return key

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ConversationLedgerError(
                "conversation memory path must be a real directory"
            )
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("conversation memory path is not owned by this user")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise PermissionError("conversation memory path permissions are too broad")

    @classmethod
    def _read_raw_rows(cls, path: Path) -> list[dict[str, object]]:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return []
        cls._validate_private_file(path, metadata)
        if metadata.st_size > MAX_STATE_FILE_BYTES:
            raise ConversationLedgerError(
                "conversation memory log exceeds the state size limit"
            )
        rows: list[dict[str, object]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ConversationLedgerError(
                        "conversation memory log contains invalid JSON"
                    ) from exc
                if not isinstance(value, dict):
                    raise ConversationLedgerError(
                        "conversation memory log rows must be objects"
                    )
                rows.append(value)
        return rows

    @classmethod
    def _read_last_raw_row(cls, path: Path) -> dict[str, object] | None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return None
        cls._validate_private_file(path, metadata)
        if metadata.st_size > MAX_STATE_FILE_BYTES:
            raise ConversationLedgerError(
                "conversation memory log exceeds the state size limit"
            )
        if metadata.st_size == 0:
            return None
        with path.open("rb") as handle:
            position = metadata.st_size
            tail = b""
            while position > 0 and b"\n" not in tail.rstrip(b"\n"):
                amount = min(8192, position)
                position -= amount
                handle.seek(position)
                tail = handle.read(amount) + tail
            lines = tail.splitlines()
        if not lines:
            return None
        try:
            value = json.loads(lines[-1])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConversationLedgerError(
                "conversation memory log contains invalid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise ConversationLedgerError(
                "conversation memory log rows must be objects"
            )
        return value

    @classmethod
    def _append_raw_row(cls, path: Path, row: dict[str, object]) -> None:
        payload = (canonical_json(row) + "\n").encode("utf-8")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ConversationLedgerError(
                    "conversation memory log must be a regular file"
                )
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise PermissionError(
                    "conversation memory log is not owned by this user"
                )
            if metadata.st_size + len(payload) > MAX_STATE_FILE_BYTES:
                raise ConversationLedgerError(
                    "conversation memory log exceeds the state size limit"
                )
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("could not append conversation memory log")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _validate_private_file(path: Path, metadata: os.stat_result) -> None:
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ConversationLedgerError(
                f"conversation memory file must be regular: {path.name}"
            )
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError("conversation memory file is not owned by this user")
        if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
            raise PermissionError("conversation memory file permissions are too broad")
