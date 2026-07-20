from __future__ import annotations

import re
import subprocess
from collections import Counter


Finding = tuple[str, str, str]

_DEEPSEEK_KEY = "DEEPSEEK" + "_API_KEY="
_OPENAI_KEY = "OPENAI" + "_API_KEY="
_MCA_KEY = "MCA" + "_API_KEY="
_SK_TEST = "sk-" + "testsecret123456"
_SK_NO_PRINT = "sk-" + "do-not-print"
_SK_NO_READ = "sk-" + "do-not-read-or-print"
_SK_PARENT = "sk-" + "parent-secret-123456"

APPROVED_LOCATIONS: Counter[Finding] = Counter(
    {
        (
            _DEEPSEEK_KEY + "...",
            "README.zh-CN.md",
            _DEEPSEEK_KEY + "...",
        ): 1,
        (
            _OPENAI_KEY + "...",
            "README.zh-CN.md",
            _OPENAI_KEY + "...",
        ): 1,
        (
            _MCA_KEY + "not-needed",
            "README.zh-CN.md",
            _MCA_KEY + "not-needed",
        ): 1,
        (
            _MCA_KEY + "not-needed\\n",
            "src/mini_code_agent/cli.py",
            '"# ' + _MCA_KEY + 'not-needed\\n"',
        ): 1,
        (
            _MCA_KEY + "not-needed",
            "src/mini_code_agent/model.py",
            '"For a keyless local compatible server, set '
            + _MCA_KEY
            + 'not-needed explicitly."',
        ): 1,
        (
            _SK_TEST,
            "tests/test_agent_cli.py",
            'secret = "' + _SK_TEST + '"',
        ): 1,
        (
            _SK_NO_READ,
            "tests/test_diagnostics.py",
            'secret = "' + _SK_NO_READ + '"',
        ): 1,
        (
            _DEEPSEEK_KEY + "{secret}\\n",
            "tests/test_diagnostics.py",
            'env_file.write_text(f"'
            + _DEEPSEEK_KEY
            + '{secret}\\n", encoding="utf-8")',
        ): 1,
        (
            _DEEPSEEK_KEY + "not-inspected\\n",
            "tests/test_diagnostics.py",
            'env_file.write_text("'
            + _DEEPSEEK_KEY
            + 'not-inspected\\n", encoding="utf-8")',
        ): 1,
        (
            _SK_NO_PRINT,
            "tests/test_diagnostics.py",
            'secret = "' + _SK_NO_PRINT + '"',
        ): 1,
        (
            _SK_PARENT,
            "tests/test_hardening.py",
            'monkeypatch.setenv("DEEPSEEK_API_KEY", "' + _SK_PARENT + '")',
        ): 2,
        (
            _DEEPSEEK_KEY + "default-private-key\\n",
            "tests/test_hardening.py",
            'env_file.write_text("'
            + _DEEPSEEK_KEY
            + 'default-private-key\\n", encoding="utf-8")',
        ): 1,
    }
)

_CANDIDATE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{12,}|(?:DEEPSEEK|OPENAI|MCA)_API_KEY=[^\s\"'`]+"),
    re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+(?:/[^\s\"'`<>]*)?"),
)


def scan_added_diff(diff: str) -> Counter[Finding]:
    findings: Counter[Finding] = Counter()
    path = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            continue
        if not path or not line.startswith("+") or line.startswith("+++"):
            continue
        context = line[1:].strip()
        for pattern in _CANDIDATE_PATTERNS:
            for match in pattern.finditer(line[1:]):
                findings[(match.group(0), path, context)] += 1
    return findings


def validate_added_diff(diff: str) -> Counter[Finding]:
    findings = scan_added_diff(diff)
    unexpected = findings - APPROVED_LOCATIONS
    if unexpected:
        details = "\n".join(
            f"{path}: {candidate!r} ({count} occurrence(s)) in {context!r}"
            for (candidate, path, context), count in sorted(unexpected.items())
        )
        raise ValueError(f"unapproved added-content candidate:\n{details}")
    return findings


def main() -> int:
    diff = subprocess.check_output(
        ["git", "diff", "--unified=0", "origin/main...HEAD", "--", "."],
        text=True,
    )
    try:
        findings = validate_added_diff(diff)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    for (candidate, path, _context), count in sorted(findings.items()):
        print(f"approved synthetic location: {path}: {candidate!r} ({count})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
