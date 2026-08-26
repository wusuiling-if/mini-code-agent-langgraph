"""Translate authenticated MCA transactions into host-neutral experience."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from memory_core.contracts import (
    EvidenceReference,
    VerifiedCheck,
    VerifiedExperience,
)
from mini_code_agent.transaction import TransactionStore


class TransactionEvidenceAdapter:
    def __init__(self, state_root: Path) -> None:
        self.transactions = TransactionStore(Path(state_root).expanduser().resolve())

    def resolve(self, reference: str) -> VerifiedExperience:
        receipt = self.transactions.validated_receipt(reference)
        payload = receipt["payload"]
        verification = payload.get("verification", {})
        raw_checks = verification.get("checks")
        if verification.get("status") != "passed" or not isinstance(raw_checks, list):
            raise ValueError("transaction has no passing verification evidence")
        checks = []
        for check in raw_checks:
            if (
                not isinstance(check, dict)
                or check.get("returncode") != 0
                or bool(check.get("blocked", False))
                or not bool(check.get("approved", True))
            ):
                raise ValueError(
                    "transaction contains an ineligible verification check"
                )
            checks.append(
                VerifiedCheck(
                    name=str(check.get("name", "")),
                    command_sha256=str(check.get("command_sha256", "")),
                )
            )
        workspace_identity = str(
            payload.get("workspace", {}).get("identity_sha256", "")
        ).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", workspace_identity):
            raise ValueError("transaction has no valid project identity")
        issued_at_ns = payload.get("issued_at_ns")
        if not isinstance(issued_at_ns, int) or issued_at_ns < 0:
            raise ValueError("transaction issue time is invalid")
        valid_from = datetime.fromtimestamp(
            issued_at_ns / 1_000_000_000, tz=timezone.utc
        ).isoformat()
        manifest = self.transactions.load(reference)
        task = str(manifest.get("task", "")).strip()
        if not task:
            raise ValueError("transaction task is missing")
        patch = self.transactions.validated_patch(reference)
        binary = b"GIT binary patch" in patch
        try:
            artifact_text = None if binary else patch.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            artifact_text = None
            binary = True
        receipt_id = str(receipt["receipt_id"])
        return VerifiedExperience(
            evidence=EvidenceReference(
                source_type="transaction_receipt",
                source_ref=f"state:transaction-receipt:{reference}:{receipt_id}",
                source_sha256=receipt_id,
                origin="trusted_tool",
            ),
            scope="workspace",
            scope_key=f"sha256:{workspace_identity}",
            valid_from=valid_from,
            task=task,
            checks=tuple(checks),
            artifact_text=artifact_text,
            artifact_size_bytes=len(patch),
            artifact_binary=binary,
        )
