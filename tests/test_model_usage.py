from __future__ import annotations

from langchain_core.messages import AIMessage

from mini_code_agent.agent import _model_usage


def test_model_usage_prefers_normalized_langchain_metadata() -> None:
    message = AIMessage(
        content="done",
        usage_metadata={
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
            "input_token_details": {"cache_read": 20},
            "output_token_details": {"reasoning": 8},
        },
    )

    assert _model_usage(message) == {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "cached_input_tokens": 20,
        "reasoning_tokens": 8,
    }


def test_model_usage_falls_back_to_openai_response_metadata() -> None:
    message = AIMessage(
        content="done",
        response_metadata={
            "token_usage": {
                "prompt_tokens": 90,
                "completion_tokens": 10,
                "total_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 12},
                "completion_tokens_details": {"reasoning_tokens": 4},
            }
        },
    )

    assert _model_usage(message) == {
        "input_tokens": 90,
        "output_tokens": 10,
        "total_tokens": 100,
        "cached_input_tokens": 12,
        "reasoning_tokens": 4,
    }
