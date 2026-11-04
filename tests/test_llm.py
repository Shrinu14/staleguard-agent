"""
Unit tests for agent/llm.py. No network: httpx.MockTransport plays Bifrost,
so these run in CI without Docker or Ollama.
"""

import json

import httpx
import pytest

from agent import lookups
from agent.config import Settings
from agent.llm import LLM, LLMError

VK = "sk-bf-test-default"
VK_LOCAL = "sk-bf-test-local"
PRIMARY = "gemini/gemini-test"
LOCAL = "ollama/qwen2.5-coder:7b"


def make_settings(**overrides: str) -> Settings:
    values = {
        "bifrost_vk_default": VK,
        "bifrost_vk_local": VK_LOCAL,
        "model_fallbacks": "",
        **overrides,
    }
    return Settings(_env_file=None, **values)


def completion(model: str, content: str = "pong") -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": content},
        }],
        "usage": {
            "prompt_tokens": 34,
            "completion_tokens": 2,
            "total_tokens": 36,
            "prompt_tokens_details": {"cached_tokens": 10},
        },
    }


def fake_bifrost(responder, seen: list[httpx.Request]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responder(request)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_primary_answers_no_fallback() -> None:
    seen: list[httpx.Request] = []
    client = fake_bifrost(
        lambda _: httpx.Response(
            200,
            json=completion("qwen2.5-coder:7b"),
            headers={
                lookups.BIFROST_HDR_PROVIDER: "ollama",
                lookups.BIFROST_HDR_MODEL: "qwen2.5-coder:7b",
            },
        ),
        seen,
    )
    result = LLM(make_settings(), http_client=client).chat(
        [{"role": "user", "content": "hi"}], model=LOCAL
    )

    assert result.content == "pong"
    assert result.served_model == LOCAL
    assert result.is_fallback is False
    assert result.switch_notice is None
    assert (result.usage.prompt_tokens, result.usage.cached_tokens) == (34, 10)
    # Authenticated with the virtual key; no fallbacks field when chain is empty.
    assert seen[0].headers[lookups.BIFROST_VK_HEADER] == VK
    assert lookups.BIFROST_FALLBACKS_FIELD not in json.loads(seen[0].content)


def test_fallback_is_detected_and_announced() -> None:
    seen: list[httpx.Request] = []
    client = fake_bifrost(
        lambda _: httpx.Response(
            200,
            json=completion("qwen2.5-coder:7b"),
            headers={
                lookups.BIFROST_HDR_PROVIDER: "ollama",
                lookups.BIFROST_HDR_MODEL: "qwen2.5-coder:7b",
                lookups.BIFROST_HDR_IS_FALLBACK: "true",
                lookups.BIFROST_HDR_PRIMARY_MODEL: "gemini-test",
            },
        ),
        seen,
    )
    result = LLM(make_settings(), http_client=client).chat(
        [{"role": "user", "content": "hi"}], model=PRIMARY, fallbacks=[LOCAL]
    )

    assert result.is_fallback is True
    assert result.served_model == LOCAL
    assert result.switch_notice == f"Switched to {LOCAL} — {PRIMARY} unavailable."
    assert json.loads(seen[0].content)[lookups.BIFROST_FALLBACKS_FIELD] == [LOCAL]


def test_model_mismatch_counts_as_fallback_without_header() -> None:
    client = fake_bifrost(
        lambda _: httpx.Response(
            200,
            json=completion("qwen2.5-coder:7b"),
            headers={
                lookups.BIFROST_HDR_PROVIDER: "ollama",
                lookups.BIFROST_HDR_MODEL: "qwen2.5-coder:7b",
            },
        ),
        [],
    )
    result = LLM(make_settings(), http_client=client).chat(
        [{"role": "user", "content": "hi"}], model=PRIMARY, fallbacks=()
    )
    assert result.is_fallback is True


def test_fallbacks_default_to_env_chain() -> None:
    seen: list[httpx.Request] = []
    client = fake_bifrost(lambda _: httpx.Response(200, json=completion("x")), seen)
    settings = make_settings(model_fallbacks=f" groq/a , {LOCAL} ,")
    LLM(settings, http_client=client).chat([{"role": "user", "content": "hi"}], model=PRIMARY)

    assert settings.fallbacks == ("groq/a", LOCAL)
    assert json.loads(seen[0].content)[lookups.BIFROST_FALLBACKS_FIELD] == ["groq/a", LOCAL]


def test_local_only_uses_local_virtual_key() -> None:
    seen: list[httpx.Request] = []
    client = fake_bifrost(lambda _: httpx.Response(200, json=completion("x")), seen)
    LLM(make_settings(), local_only=True, http_client=client).chat(
        [{"role": "user", "content": "hi"}], model=LOCAL
    )
    assert seen[0].headers[lookups.BIFROST_VK_HEADER] == VK_LOCAL


def test_missing_virtual_key_fails_early() -> None:
    settings = Settings(_env_file=None)
    with pytest.raises(LLMError, match="BIFROST_VK_DEFAULT"):
        LLM(settings)


def test_gateway_down_gives_hint() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = fake_bifrost(refuse, [])
    with pytest.raises(LLMError, match="docker compose up -d"):
        LLM(make_settings(), http_client=client).chat(
            [{"role": "user", "content": "hi"}], model=LOCAL
        )


def test_status_error_is_readable() -> None:
    body = {
        "error": {
            "message": "Model 'x' is not allowed for virtual key 'staleguard-local-only'",
            "type": "model_not_allowed",
        }
    }
    client = fake_bifrost(lambda _: httpx.Response(403, json=body), [])
    with pytest.raises(LLMError, match=r"403: Model 'x' is not allowed.*not allowed to use"):
        LLM(make_settings(), http_client=client).chat(
            [{"role": "user", "content": "hi"}], model="groq/x"
        )
