from __future__ import annotations

from evals.run_memory_comparison import BenchmarkCase
from evals.run_memory_model_comparison import response_is_correct


def _case(*, abstain: bool = False) -> BenchmarkCase:
    return BenchmarkCase(
        "case",
        "generic",
        "query",
        (),
        expected_abstain=abstain,
        expected_markers=() if abstain else ("电子邮件", "账单"),
    )


def test_model_response_scoring_uses_preregistered_markers():
    assert response_is_correct(_case(), "请通过电子邮件接收账单。") is True
    assert response_is_correct(_case(), "请接收电子账单。") is False


def test_model_response_scoring_requires_strict_abstention_token():
    assert response_is_correct(_case(abstain=True), "NO_MEMORY") is True
    assert response_is_correct(_case(abstain=True), "可能是 NO_MEMORY") is False
