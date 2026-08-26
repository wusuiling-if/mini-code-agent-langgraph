"""Host-neutral conservative secret detection for durable memory content."""

from __future__ import annotations

import math
import re
from collections import Counter


class SecretDetector:
    """Reject obvious credentials and high-entropy credential assignments."""

    _TOKEN = re.compile(r"[A-Za-z0-9_+/=-]{24,}")
    _ASSIGNMENT = re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)\b"
        r"\s*[:=]\s*['\"]?([^\s'\",;}]+)"
    )
    _SAFE_PREFIXES = ("os.environ", "getenv", "env.", "${", "<", "***")

    def contains_secret(self, text: str) -> bool:
        if "-----BEGIN " in text and "PRIVATE KEY-----" in text:
            return True
        patterns = (
            r"(?i)\bauthorization\s*:\s*bearer\s+\S+",
            r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b",
            r"\bAKIA[0-9A-Z]{16}\b",
            r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
            r"\b(?:xox[baprs]-)[A-Za-z0-9-]{20,}\b",
        )
        if any(re.search(pattern, text) for pattern in patterns):
            return True
        for match in self._ASSIGNMENT.finditer(text):
            value = match.group(1).casefold().strip()
            if value.startswith(self._SAFE_PREFIXES):
                continue
            if len(value) >= 8:
                return True
        for line in text.splitlines():
            if not re.search(
                r"(?i)(credential|token|secret|password|api[_-]?key)", line
            ):
                continue
            if any(self._entropy(token) >= 4.0 for token in self._TOKEN.findall(line)):
                return True
        return False

    @staticmethod
    def _entropy(value: str) -> float:
        counts = Counter(value)
        length = len(value)
        return -sum(
            (count / length) * math.log2(count / length) for count in counts.values()
        )
