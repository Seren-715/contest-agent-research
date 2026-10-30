from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.schemas import validate_answer


def test_chat_schema() -> None:
    model = validate_answer(
        "Chat",
        {"case_id": "A-01", "answer": "回答", "clarifying_question": "问题？"},
    )
    assert model.model_dump()["case_id"] == "A-01"


def test_retrieval_schema() -> None:
    model = validate_answer(
        "Literature_Retrieval",
        {"case_id": "B-01", "papers": [{"filename": "a.pdf", "reason": "匹配"}]},
    )
    assert len(model.model_dump()["papers"]) == 1


def test_report_requires_length_and_references() -> None:
    markdown = "正文" * 500 + "\n\n## 参考文献\n[1] 论文.pdf\n"
    model = validate_answer(
        "Survey",
        {"case_id": "C-01", "path": "data/expert/C-01-综述-team.md", "markdown": markdown},
    )
    assert model.model_dump()["path"].endswith(".md")
    with pytest.raises(ValidationError):
        validate_answer("Survey", {"case_id": "C-01", "path": "data/expert/x.md", "markdown": "太短"})


def test_reproduction_requires_three_metrics() -> None:
    metrics = [
        {"name": "m1", "unit": "u", "reference": None, "observed": 1.0, "relative_error": None},
        {"name": "m2", "unit": "u", "reference": None, "observed": 2.0, "relative_error": None},
        {"name": "m3", "unit": "u", "reference": None, "observed": 3.0, "relative_error": None},
    ]
    model = validate_answer(
        "Reproduction",
        {"case_id": "D-01", "status": "completed", "metrics": metrics, "figures": []},
    )
    assert len(model.model_dump()["metrics"]) == 3
    with pytest.raises(ValidationError):
        validate_answer(
            "Reproduction",
            {"case_id": "D-01", "status": "completed", "metrics": metrics[:2], "figures": []},
        )
    with pytest.raises(ValidationError):
        validate_answer(
            "Reproduction",
            {"case_id": "D-01", "status": "failed", "metrics": metrics, "figures": []},
        )


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        validate_answer(
            "Chat",
            {"case_id": "A-01", "answer": "回答", "clarifying_question": "问题？", "extra": 1},
        )
