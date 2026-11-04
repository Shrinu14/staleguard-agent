"""
The single LLM client. Every model call in the project goes through here,
and through Bifrost — never directly to a provider.

How it works:
  - Bifrost speaks the OpenAI API, so we use the official `openai` SDK with
    base_url pointed at Bifrost. The SDK never sees a provider key: it
    authenticates with a Bifrost *virtual key* (x-bf-vk header).
  - Fallbacks are sent in the request body ("fallbacks": [...]); Bifrost
    tries them in order if the primary model fails.
  - We read the raw HTTP response so we can see Bifrost's routing headers.
    They say which provider/model really answered and whether a fallback
    served the request — that is how the CLI can tell the user
    "Switched to X — primary unavailable".
  - Token counts come from the response's `usage` field (real numbers from
    the provider), which later pieces use for budgets and benchmarks.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import openai
from openai.types.chat import ChatCompletion, ChatCompletionMessage

from agent import lookups
from agent.config import Settings, get_settings


class LLMError(RuntimeError):
    """A model call failed in a way the user should see (with a hint)."""


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0  # prompt tokens served from the provider's cache


@dataclass(frozen=True, slots=True)
class LLMResult:
    message: ChatCompletionMessage
    requested_model: str  # what we asked for, e.g. "gemini/gemini-2.5-flash"
    served_model: str  # what actually answered, e.g. "ollama/qwen2.5-coder:7b"
    is_fallback: bool
    usage: Usage

    @property
    def content(self) -> str:
        return self.message.content or ""

    @property
    def switch_notice(self) -> str | None:
        """User-facing notice when a different model took over, else None."""
        if not self.is_fallback:
            return None
        return f"Switched to {self.served_model} — {self.requested_model} unavailable."


def make_client(
    settings: Settings | None = None,
    *,
    local_only: bool = False,
    http_client: httpx.Client | None = None,
) -> openai.OpenAI:
    """Build an OpenAI SDK client that talks to Bifrost with a virtual key."""
    s = settings or get_settings()
    vk = s.virtual_key(local_only=local_only)
    if vk is None:
        name = "BIFROST_VK_LOCAL" if local_only else "BIFROST_VK_DEFAULT"
        raise LLMError(f"{name} is not set in .env — Bifrost requires a virtual key.")
    secret = vk.get_secret_value()
    return openai.OpenAI(
        base_url=s.bifrost_base_url,
        api_key=secret,  # SDK requires one; Bifrost also accepts sk-bf-* here
        default_headers={lookups.BIFROST_VK_HEADER: secret},
        timeout=lookups.LLM_TIMEOUT_SECONDS,
        max_retries=lookups.LLM_CLIENT_MAX_RETRIES,
        http_client=http_client,
    )


class LLM:
    """Thin wrapper: one chat() call in, one LLMResult out."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        local_only: bool = False,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.local_only = local_only
        self._client = make_client(
            self.settings, local_only=local_only, http_client=http_client
        )

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str,
        fallbacks: Iterable[str] | None = None,
        **params: Any,
    ) -> LLMResult:
        """Send one chat completion through Bifrost.

        `fallbacks=None` uses MODEL_FALLBACKS from .env; pass () for none.
        Extra params (tools, temperature, max_tokens, ...) go to the API as-is.
        """
        chain = list(self.settings.fallbacks if fallbacks is None else fallbacks)
        extra_body = {lookups.BIFROST_FALLBACKS_FIELD: chain} if chain else None
        try:
            raw = self._client.chat.completions.with_raw_response.create(
                model=model, messages=list(messages), extra_body=extra_body, **params
            )
        except openai.APIConnectionError as exc:
            raise LLMError(
                f"Cannot reach Bifrost at {self.settings.bifrost_base_url}. "
                "Is it running? Start it with: docker compose up -d"
            ) from exc
        except openai.APIStatusError as exc:
            raise LLMError(_describe_status_error(exc)) from exc

        completion: ChatCompletion = raw.parse()
        return _to_result(completion, raw.headers, requested_model=model)


def _to_result(
    completion: ChatCompletion, headers: httpx.Headers, *, requested_model: str
) -> LLMResult:
    provider = headers.get(lookups.BIFROST_HDR_PROVIDER)
    model = headers.get(lookups.BIFROST_HDR_MODEL) or completion.model
    served = f"{provider}/{model}" if provider else model
    flagged = headers.get(lookups.BIFROST_HDR_IS_FALLBACK, "").lower() == "true"
    # Header first; the model comparison is a safety net if headers are stripped.
    is_fallback = flagged or (bool(provider) and served != requested_model)
    return LLMResult(
        message=completion.choices[0].message,
        requested_model=requested_model,
        served_model=served,
        is_fallback=is_fallback,
        usage=_usage(completion),
    )


def _usage(completion: ChatCompletion) -> Usage:
    u = completion.usage
    if u is None:
        return Usage()
    details = u.prompt_tokens_details
    return Usage(
        prompt_tokens=u.prompt_tokens,
        completion_tokens=u.completion_tokens,
        total_tokens=u.total_tokens,
        cached_tokens=(details.cached_tokens or 0) if details else 0,
    )


def _describe_status_error(exc: openai.APIStatusError) -> str:
    """Turn a Bifrost error response into one readable line with a hint."""
    # The SDK has already unwrapped {"error": {...}}; Bifrost's error object has
    # "message" (summary) and sometimes "error" (low-level cause) strings.
    body = exc.body if isinstance(exc.body, dict) else {}
    if isinstance(body.get("error"), dict):  # not unwrapped (defensive)
        body = body["error"]
    summary = body.get("message") or exc.message
    cause = body.get("error") if isinstance(body.get("error"), str) else None
    msg = f"Bifrost returned {exc.status_code}: {summary}"
    if cause and cause != summary:
        msg += f" — {cause}"
    if exc.status_code == 401:
        msg += " (check BIFROST_VK_* in .env matches bifrost/config.json)"
    elif exc.status_code == 403:
        msg += " (this virtual key is not allowed to use that provider/model)"
    return msg
