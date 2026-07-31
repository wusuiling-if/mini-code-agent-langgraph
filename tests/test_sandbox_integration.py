import os

import pytest

from mini_code_agent.sandbox_probe import run_sandbox_probe


BACKEND = os.getenv("MCA_SANDBOX_BACKEND", "")


@pytest.mark.skipif(not BACKEND, reason="real sandbox integration is opt-in")
def test_real_sandbox_backend_passes_capability_probe():
    report = run_sandbox_probe(sandbox_mode=BACKEND, timeout_seconds=10)
    assert report.ok, [
        (check.name, check.detail) for check in report.checks if not check.passed
    ]
