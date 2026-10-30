"""Handler contract and small parsing helpers."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from core.config import Settings
from core.llm import ModelClient


class HandlerError(RuntimeError):
    pass


class Handler(Protocol):
    def handle(self, question: dict[str, Any], settings: Settings, client: ModelClient) -> dict[str, Any]:
        ...


FENCE_RE = re.compile(r"```(?:json|markdown|md)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    for match in FENCE_RE.finditer(text):
        candidate = match.group(1).strip()
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    raise HandlerError(f"model did not return a JSON object: {text[:300]!r}")


def strip_fences(text: str) -> str:
    text = (text or "").strip()
    match = FENCE_RE.fullmatch(text)
    if match:
        return match.group(1).strip()
    return text


def question_text(question: dict[str, Any]) -> str:
    value = question.get("input")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return str(question.get("title", "")).strip()


def constraints_text(question: dict[str, Any]) -> str:
    value = question.get("constraints")
    if isinstance(value, list):
        return "\n".join(f"- {item}" for item in value if isinstance(item, str))
    return ""
