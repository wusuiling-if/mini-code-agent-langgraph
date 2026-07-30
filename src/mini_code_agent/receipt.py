from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from pathlib import Path
from typing import Any

from mini_code_agent.utils import MAX_STATE_FILE_BYTES, write_json


RECEIPT_VERSION = 1


class ReceiptError(RuntimeError):
    """A transaction receipt is missing, malformed, or unauthentic."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def issue_receipt(
    state_root: Path,
    transaction_directory: Path,
    manifest: dict[str, Any],
    trajectory: dict[str, Any],
) -> dict[str, Any]:
    checks = _verification_checks(trajectory)
    payload = {
        "version": RECEIPT_VERSION,
        "kind": "mini-code-agent/transaction-receipt",
        "transaction_id": manifest["id"],
        "issued_at_ns": time.time_ns(),
        "state": "prepared",
        "baseline": {
            "commit": manifest["baseline_commit"],
            "workspace_fingerprint": manifest["baseline_fingerprint"],
        },
        "prepared": {
            "workspace_fingerprint": manifest["prepared_fingerprint"],
            "projected_source_fingerprint": manifest[
                "prepared_source_fingerprint"
            ],
            "patch_sha256": manifest["prepared_patch_sha256"],
        },
        "verification": {
            "status": trajectory.get("verification_status", ""),
            "fingerprint": trajectory.get("verified_fingerprint", ""),
            "checks": checks,
        },
        "provenance": {
            "trajectory_sha256": digest_json(trajectory),
            "access_log_sha256": digest_json(manifest["access_log"]),
            "read_set": list(manifest["read_set"]),
            "write_set": list(manifest["write_set"]),
            "broad_read": bool(manifest["broad_read"]),
            "broad_write": bool(manifest["broad_write"]),
        },
    }
    receipt_id = digest_json(payload)
    key = _load_or_create_key(state_root)
    envelope = {
        "receipt_id": receipt_id,
        "payload": payload,
        "hmac_sha256": hmac.new(
            key, canonical_json(payload), hashlib.sha256
        ).hexdigest(),
    }
    write_json(transaction_directory / "receipt.json", envelope)
    return envelope


def load_receipt(
    state_root: Path,
    transaction_directory: Path,
    transaction_id: str,
) -> dict[str, Any]:
    envelope = json.loads(
        _read_private_bytes(
            transaction_directory / "receipt.json", "transaction receipt"
        ).decode("utf-8")
    )
    if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
        raise ReceiptError("transaction receipt is malformed")
    payload = envelope["payload"]
    if (
        payload.get("version") != RECEIPT_VERSION
        or payload.get("kind") != "mini-code-agent/transaction-receipt"
        or payload.get("transaction_id") != transaction_id
        or payload.get("state") != "prepared"
    ):
        raise ReceiptError("transaction receipt identity is invalid")
    receipt_id = digest_json(payload)
    if not hmac.compare_digest(str(envelope.get("receipt_id", "")), receipt_id):
        raise ReceiptError("transaction receipt digest is invalid")
    expected = hmac.new(
        _load_existing_key(state_root), canonical_json(payload), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(str(envelope.get("hmac_sha256", "")), expected):
        raise ReceiptError("transaction receipt authentication failed")
    return envelope


def validate_receipt(
    envelope: dict[str, Any],
    manifest: dict[str, Any],
    trajectory: dict[str, Any],
) -> None:
    payload = envelope["payload"]
    baseline = payload.get("baseline", {})
    prepared = payload.get("prepared", {})
    verification = payload.get("verification", {})
    provenance = payload.get("provenance", {})
    expected = {
        "transaction_id": manifest["id"],
        "baseline_commit": manifest["baseline_commit"],
        "baseline_fingerprint": manifest["baseline_fingerprint"],
        "prepared_fingerprint": manifest["prepared_fingerprint"],
        "prepared_source_fingerprint": manifest["prepared_source_fingerprint"],
        "prepared_patch_sha256": manifest["prepared_patch_sha256"],
        "verification_status": trajectory.get("verification_status", ""),
        "verification_fingerprint": trajectory.get("verified_fingerprint", ""),
        "trajectory_sha256": digest_json(trajectory),
        "access_log_sha256": digest_json(manifest["access_log"]),
        "read_set": list(manifest["read_set"]),
        "write_set": list(manifest["write_set"]),
        "broad_read": bool(manifest["broad_read"]),
        "broad_write": bool(manifest["broad_write"]),
    }
    actual = {
        "transaction_id": payload.get("transaction_id"),
        "baseline_commit": baseline.get("commit"),
        "baseline_fingerprint": baseline.get("workspace_fingerprint"),
        "prepared_fingerprint": prepared.get("workspace_fingerprint"),
        "prepared_source_fingerprint": prepared.get(
            "projected_source_fingerprint"
        ),
        "prepared_patch_sha256": prepared.get("patch_sha256"),
        "verification_status": verification.get("status"),
        "verification_fingerprint": verification.get("fingerprint"),
        "trajectory_sha256": provenance.get("trajectory_sha256"),
        "access_log_sha256": provenance.get("access_log_sha256"),
        "read_set": provenance.get("read_set"),
        "write_set": provenance.get("write_set"),
        "broad_read": provenance.get("broad_read"),
        "broad_write": provenance.get("broad_write"),
    }
    mismatches = [key for key in expected if actual.get(key) != expected[key]]
    if mismatches:
        raise ReceiptError(
            "transaction receipt does not match durable state: "
            + ", ".join(mismatches)
        )


def _verification_checks(trajectory: dict[str, Any]) -> list[dict[str, Any]]:
    successful = [
        event
        for event in trajectory.get("events", [])
        if isinstance(event, dict)
        and event.get("type") == "tool"
        and event.get("tool") == "run_tests"
        and event.get("returncode") == 0
    ]
    if not successful:
        raise ReceiptError("passing verification evidence is missing")
    raw_checks = successful[-1].get("verification_checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        event = successful[-1]
        legacy: dict[str, Any] = {
            "name": "tests",
            "returncode": 0,
            "duration_ms": int(event.get("duration_ms", 0)),
            "blocked": bool(event.get("blocked", False)),
            "approved": bool(event.get("approved", True)),
        }
        if event.get("tests_run") is not None:
            legacy["tests_run"] = int(event["tests_run"])
        return [legacy]
    checks: list[dict[str, Any]] = []
    for raw in raw_checks:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            raise ReceiptError("verification check evidence is malformed")
        checks.append(
            {
                key: raw[key]
                for key in (
                    "name",
                    "returncode",
                    "duration_ms",
                    "tests_run",
                    "exception_info",
                    "blocked",
                    "approved",
                )
                if key in raw
            }
        )
    return checks


def _key_path(state_root: Path) -> Path:
    return state_root / "receipt.key"


def _load_or_create_key(state_root: Path) -> bytes:
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path = _key_path(state_root)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(key_path, flags, 0o600)
    except FileExistsError:
        return _load_existing_key(state_root)
    key = secrets.token_bytes(32)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        os.write(descriptor, key)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return key


def _load_existing_key(state_root: Path) -> bytes:
    for _ in range(100):
        key = _read_private_bytes(_key_path(state_root), "transaction receipt key")
        if len(key) == 32:
            return key
        if key:
            break
        time.sleep(0.01)
    raise ReceiptError("transaction receipt key is invalid")


def _read_private_bytes(path: Path, label: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ReceiptError(f"could not open {label}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ReceiptError(f"{label} must be a regular file")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise PermissionError(f"{label} is not owned by this user")
        if metadata.st_size > MAX_STATE_FILE_BYTES:
            raise ReceiptError(f"{label} exceeds the state size limit")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(MAX_STATE_FILE_BYTES + 1)
        if len(payload) > MAX_STATE_FILE_BYTES:
            raise ReceiptError(f"{label} exceeds the state size limit")
        return payload
    finally:
        if descriptor >= 0:
            os.close(descriptor)
