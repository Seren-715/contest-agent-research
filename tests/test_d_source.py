from __future__ import annotations

import pytest

from d_adapter.source import SourceError, extract_source, normalize


CODE = "def run_experiment(request):\n    return {'baseline': [], 'candidate': None, 'details': {}}\n"


def response_with(content: str, finish_reason: str = "stop") -> dict:
    return {
        "choices": [
            {"finish_reason": finish_reason, "message": {"role": "assistant", "content": content}}
        ]
    }


def test_extracts_single_code_block() -> None:
    source = extract_source(response_with("```python\n" + CODE + "```"))
    assert source.endswith("\n")
    assert "def run_experiment" in source


def test_extracts_plain_source() -> None:
    assert "run_experiment" in extract_source(response_with(CODE))


def test_rejects_truncated_response() -> None:
    with pytest.raises(SourceError):
        extract_source(response_with(CODE, finish_reason="length"))


def test_rejects_multiple_versions() -> None:
    second = CODE.replace("return", "return  # v2\n    return")
    with pytest.raises(SourceError):
        extract_source(response_with("```python\n" + CODE + "```\n```python\n" + second + "```"))


def test_normalize_strips_extra_newlines() -> None:
    assert normalize(CODE + "\n\n") == CODE
