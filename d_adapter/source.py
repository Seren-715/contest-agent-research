"""Verbatim model-source extraction.

Ported from the platform's public `model_source.py` (20261020-source-v1). The
platform checks that the executed source matches this session's model response;
do not edit, format, or augment the extracted text.
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path
from typing import Any


class SourceError(ValueError):
    pass


def normalize(source: str) -> str:
    if not isinstance(source, str) or not 0 < len(source.encode("utf-8")) <= 65536:
        raise SourceError("source must be 1..65536 UTF-8 bytes")
    source = source.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"
    tree = ast.parse(source, feature_version=(3, 9))
    if not any(isinstance(node, ast.FunctionDef) and node.name == "run_experiment" for node in tree.body):
        raise SourceError("missing top-level run_experiment(request)")
    return source


def extract_source(response: dict[str, Any]) -> str:
    if not isinstance(response, dict):
        raise SourceError("expected a complete chat/completions JSON response")
    found: set[str] = set()
    for choice in response.get("choices", []):
        if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
            continue
        message = choice.get("message", {})
        if message.get("role") != "assistant" or not isinstance(message.get("content"), str):
            continue
        content = message["content"]
        candidates = [content]
        candidates.extend(
            re.findall(r"^```(?:python|py)?[ \t]*\r?\n(.*?)^```[ \t]*$", content, re.MULTILINE | re.DOTALL)
        )
        for candidate in candidates:
            try:
                found.add(normalize(candidate))
            except (SyntaxError, SourceError, RecursionError):
                continue
    if len(found) != 1:
        raise SourceError(
            "expected exactly one complete Python implementation with finish_reason=stop; "
            "request a full new response"
        )
    return found.pop()


def save_source(response: dict[str, Any], destination: Path) -> str:
    source = extract_source(response)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.encode("utf-8"))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()
