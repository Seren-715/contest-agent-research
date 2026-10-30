"""Output schemas for the five contest task types.

Keep these strict: the platform rejects extra top-level fields and wrong types.
"""

from __future__ import annotations

import math
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


REFERENCE_HEADING = re.compile(r"^[ \t]{0,3}##[ \t]+参考文献[ \t]*\r?$", re.MULTILINE)
REPORT_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+[^\r\n]*(?:\r?\n|$)", re.MULTILINE)
NUMERIC_CITATION_GROUP = re.compile(r"\[\s*\d+(?:\s*[,，]\s*\d+)*(?:\s*,\s*p\.?\s*\d+)?\s*\]")


def report_body(markdown: str) -> str:
    """ce-quality-v3 body: exclude references and Markdown heading lines."""
    body = REFERENCE_HEADING.split(markdown, maxsplit=1)[0]
    return REPORT_HEADING.sub("", body)


def report_limit_errors(markdown: str, function: str, min_chars: int = 800) -> list[str]:
    """One counting rule for generation audits, schemas and final saves."""
    errors: list[str] = []
    if len(REFERENCE_HEADING.findall(markdown)) != 1:
        errors.append("report must contain exactly one '## 参考文献' section")
    body = report_body(markdown)
    if len(body) < min_chars:
        errors.append(f"report body has {len(body)} characters; at least {min_chars} required excluding references/headings")
    maximum = 5000 if function == "Survey" else 4500
    if len(body) > maximum:
        errors.append(f"report body has {len(body)} characters; {function} maximum is {maximum}")
    # The supplied I/O specification counts Markdown before references, while
    # a scoring supplement excludes headings. Stay inside both upper bounds.
    inclusive_body = REFERENCE_HEADING.split(markdown, maxsplit=1)[0]
    if len(inclusive_body) > maximum and len(body) <= maximum:
        errors.append(f"report body including Markdown headings has {len(inclusive_body)} characters; {function} maximum is {maximum}")
    citations = len(NUMERIC_CITATION_GROUP.findall(body))
    if function == "Survey" and citations > 40:
        errors.append(f"Survey body has {citations} citation groups; maximum is 40")
    return errors


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatAnswer(StrictModel):
    case_id: str
    answer: str
    clarifying_question: str

    @field_validator("answer", "clarifying_question")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value


class PaperHit(StrictModel):
    filename: str
    reason: str

    @field_validator("filename", "reason")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string")
        return value


class RetrievalAnswer(StrictModel):
    case_id: str
    papers: list[PaperHit]


class ReportAnswer(StrictModel):
    case_id: str
    path: str
    markdown: str

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.replace("\\", "/").split("/"):
            raise ValueError("path must be a relative path without '..'")
        return value

    @model_validator(mode="after")
    def long_enough(self) -> "ReportAnswer":
        function = "Survey" if self.case_id.startswith("C-") else "Idea"
        if not self.path.startswith("data/expert/") or "/" in self.path[len("data/expert/"):]:
            raise ValueError("report path must be a file directly inside data/expert")
        filename = self.path[len("data/expert/"):]
        if function == "Survey":
            pattern = re.escape(self.case_id) + r"-综述-.+\.md"
        else:
            pattern = re.escape(self.case_id) + r"-.*Idea报告-.+\.md"
        if not re.fullmatch(pattern, filename):
            raise ValueError("report filename must match the current case and report naming rule")
        errors = report_limit_errors(self.markdown, function)
        if errors:
            raise ValueError("; ".join(errors))
        return self


class Metric(StrictModel):
    name: str
    unit: str
    reference: Optional[float] = None
    observed: Optional[float] = None
    relative_error: Optional[float] = None


class Figure(StrictModel):
    path: str
    caption: str
    x_label: str
    y_label: str
    legend: str


class ReproductionAnswer(StrictModel):
    case_id: str
    status: Literal["completed", "failed", "materials_missing"]
    metrics: list[Metric]
    figures: list[Figure]

    @field_validator("metrics")
    @classmethod
    def exactly_three(cls, value: list[Metric]) -> list[Metric]:
        if len(value) != 3:
            raise ValueError("metrics must contain exactly three objects")
        return value

    @field_validator("figures")
    @classmethod
    def figure_fields(cls, value: list[Figure]) -> list[Figure]:
        for figure in value:
            if not figure.path.startswith("data/figure/"):
                raise ValueError("figure path must start with data/figure/")
            if ".." in figure.path.replace("\\", "/").split("/"):
                raise ValueError("figure path must not contain '..'")
        return value

    @model_validator(mode="after")
    def observed_consistency(self) -> "ReproductionAnswer":
        values = [metric.observed for metric in self.metrics]
        if self.status == "completed":
            if any(value is None for value in values):
                raise ValueError("completed requires three finite observed values")
            if any(not math.isfinite(float(value)) for value in values if value is not None):
                raise ValueError("completed observed values must be finite")
        else:
            if any(value is not None for value in values):
                raise ValueError("failed/materials_missing requires observed=null for all metrics")
        return self


SCHEMAS: dict[str, type[BaseModel]] = {
    "Chat": ChatAnswer,
    "Literature_Retrieval": RetrievalAnswer,
    "Survey": ReportAnswer,
    "Reproduction": ReproductionAnswer,
    "Idea": ReportAnswer,
}


def validate_answer(function: str, payload: dict[str, Any]) -> BaseModel:
    schema = SCHEMAS.get(function)
    if schema is None:
        raise ValueError(f"unknown function: {function}")
    prefix = {"Chat": "A", "Literature_Retrieval": "B", "Survey": "C", "Reproduction": "D", "Idea": "E"}[function]
    if not isinstance(payload.get("case_id"), str) or not re.fullmatch(prefix + r"-\d{2,}", payload["case_id"]):
        raise ValueError("case_id must agree with the requested output type")
    return schema.model_validate(payload)
