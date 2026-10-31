"""Minimal OpenAI-compatible model client.

The contest Gateway exposes a chat/completions endpoint. Prefer the OpenAI SDK
for AgentHack telemetry, with an HTTP fallback for local environments.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from core.execution import check_active, current_execution

try:
    from openai import OpenAI as _OpenAI
except ImportError:  # local venv may not have the SDK; httpx fallback still works
    _OpenAI = None


_TOKEN_LIMIT_HINTS = ("max_tokens", "maximum", "context length", "too large", "out of range")


def output_budget() -> int:
    """Output-token cap for B/C/E calls (D keeps its own setting).

    The contest model is a reasoning model: hidden thinking and the visible
    answer share max_tokens. Small caps (400-6000) truncate the thinking and
    leave no answer; docs/PLATFORM.md recorded the same failure for D at 16k.
    A cap is an upper bound, not a target length.
    """
    value = int(os.environ.get("CONTEST_MAX_TOKENS", "32000"))
    if value <= 0:
        raise ValueError("CONTEST_MAX_TOKENS must be positive")
    return value


class ModelError(RuntimeError):
    pass


@dataclass
class ModelClient:
    base_url: Optional[str]
    api_key: Optional[str]
    model: Optional[str]
    timeout: float = 60.0
    mock: bool = False

    def _check(self) -> None:
        if self.mock:
            return
        if not self.base_url or not self.api_key or not self.model:
            raise ModelError(
                "model gateway is not configured: set PLATFORM_LLM_URL, "
                "PLATFORM_API_KEY and PLATFORM_LLM_MODEL"
            )

    def _endpoint(self) -> str:
        assert self.base_url
        url = self.base_url.rstrip("/")
        return url if url.endswith("/chat/completions") else url + "/chat/completions"

    def complete_raw(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        check_active()
        self._check()
        request_timeout = self.timeout if timeout is None else min(self.timeout, timeout)
        control = current_execution()
        if control is not None:
            request_timeout = min(request_timeout, control.remaining())
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise TimeoutError("model request has no remaining time")
        if self.mock:
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "answer": "mock answer",
                                    "clarifying_question": "你希望把研究范围限定在哪个具体场景？",
                                },
                                ensure_ascii=False,
                            ),
                        },
                    }
                ]
            }
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        # Prefer the OpenAI SDK: the AgentHack SDK intercepts
        # Completions.create and records model usage/tokens for review.
        if _OpenAI is not None:
            try:
                client = _OpenAI(
                    base_url=self.base_url,
                    api_key=self.api_key,
                    timeout=request_timeout,
                    max_retries=0,
                )
                response = client.chat.completions.create(**payload)
                body = response.model_dump()
            except Exception as exc:
                raise ModelError(f"model request failed: {exc}") from exc
            check_active()
            if not isinstance(body, dict):
                raise ModelError("model response is not a JSON object")
            return body

        try:
            response = httpx.post(
                self._endpoint(),
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=request_timeout,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise ModelError(f"model request failed: {exc}") from exc
        except ValueError as exc:
            raise ModelError(f"model response is not JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise ModelError("model response is not a JSON object")
        check_active()
        return body

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        try:
            body = self.complete_raw(messages, temperature=temperature, max_tokens=max_tokens, timeout=timeout)
        except ModelError as exc:
            # A gateway may cap max_tokens; retry once at the cap D already uses.
            if not (max_tokens and max_tokens > 16000 and any(h in str(exc).lower() for h in _TOKEN_LIMIT_HINTS)):
                raise
            check_active()
            body = self.complete_raw(messages, temperature=temperature, max_tokens=16000, timeout=timeout)
        choices = body.get("choices") or []
        if not choices:
            raise ModelError(f"model response has no choices: {body}")
        finish_reason = choices[0].get("finish_reason")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            if finish_reason == "length":
                # Thinking consumed the whole budget. The reasoning trace is not
                # an answer; using it would publish thoughts or break JSON.
                raise ModelError("model output truncated (finish_reason=length) before any answer; "
                                 "raise CONTEST_MAX_TOKENS")
            # Some reasoning models put the visible answer in reasoning_content
            # when it completed normally; use it rather than returning empty.
            reasoning = message.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning.strip():
                content = reasoning
        elif finish_reason == "length":
            logging.getLogger(__name__).warning("model answer truncated (finish_reason=length, max_tokens=%s)", max_tokens)
        if not isinstance(content, str):
            raise ModelError("model response content is not a string")
        return content
