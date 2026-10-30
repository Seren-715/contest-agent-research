"""dispatch.json parsing and the 11-case contract."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXPECTED_PER_CATEGORY = {"A": 1, "B": 3, "C": 2, "D": 3, "E": 2}
EXPECTED_TOTAL = sum(EXPECTED_PER_CATEGORY.values())

FUNCTION_BY_PREFIX = {
    "A": "Chat",
    "B": "Literature_Retrieval",
    "C": "Survey",
    "D": "Reproduction",
    "E": "Idea",
}


class DispatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class CaseRef:
    case_id: str
    function: str
    title: str
    question_file: str
    difficulty: str | None = None


@dataclass(frozen=True)
class Dispatch:
    dispatch_id: str
    team: str
    case_ids: list[str]
    cases: list[CaseRef]
    raw: dict[str, Any]


def load_dispatch(path: Path) -> Dispatch:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DispatchError(f"dispatch file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DispatchError(f"dispatch file is not valid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise DispatchError("dispatch must be a JSON object")

    case_ids = raw.get("case_ids")
    cases_raw = raw.get("cases")
    if not isinstance(case_ids, list) or not all(isinstance(item, str) for item in case_ids):
        raise DispatchError("dispatch.case_ids must be a list of strings")
    if not isinstance(cases_raw, list):
        raise DispatchError("dispatch.cases must be a list")

    cases: list[CaseRef] = []
    for item in cases_raw:
        if not isinstance(item, dict):
            raise DispatchError("each dispatch.cases entry must be an object")
        for key in ("case_id", "function", "question_file"):
            if not isinstance(item.get(key), str):
                raise DispatchError(f"dispatch.cases entry missing string field {key}")
        cases.append(
            CaseRef(
                case_id=item["case_id"],
                function=item["function"],
                title=str(item.get("title", "")),
                question_file=item["question_file"],
                difficulty=item.get("difficulty"),
            )
        )

    dispatch = Dispatch(
        dispatch_id=str(raw.get("dispatch_id", "")),
        team=str(raw.get("team", "")),
        case_ids=list(case_ids),
        cases=cases,
        raw=raw,
    )
    validate_dispatch(dispatch)
    return dispatch


def validate_dispatch(dispatch: Dispatch) -> None:
    if len(dispatch.case_ids) != EXPECTED_TOTAL:
        raise DispatchError(f"case_ids must contain {EXPECTED_TOTAL} entries")
    if len(set(dispatch.case_ids)) != len(dispatch.case_ids):
        raise DispatchError("case_ids contains duplicates")
    if {case.case_id for case in dispatch.cases} != set(dispatch.case_ids):
        raise DispatchError("cases entries do not match case_ids")

    counts = Counter(case.case_id.split("-", 1)[0] for case in dispatch.cases)
    if dict(counts) != EXPECTED_PER_CATEGORY:
        raise DispatchError(f"category counts must be {EXPECTED_PER_CATEGORY}, got {dict(counts)}")

    for case in dispatch.cases:
        prefix = case.case_id.split("-", 1)[0]
        expected = FUNCTION_BY_PREFIX.get(prefix)
        if expected and case.function != expected:
            raise DispatchError(f"{case.case_id} function {case.function!r} != {expected!r}")


def find_case(dispatch: Dispatch, case_id: str) -> CaseRef:
    for case in dispatch.cases:
        if case.case_id == case_id:
            return case
    raise DispatchError(f"case {case_id!r} is not in dispatch")


def resolve_question_file(dispatch: Dispatch, case_id: str, workspace_root: Path) -> Path:
    case = find_case(dispatch, case_id)
    path = Path(case.question_file)
    if not path.is_absolute():
        path = workspace_root / path
    if not path.exists():
        raise DispatchError(f"question file not found for {case_id}: {path}")
    return path


def load_question(path: Path, expected_case_id: str | None = None) -> dict[str, Any]:
    try:
        question = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DispatchError(f"question file is not valid JSON: {path}") from exc
    if not isinstance(question, dict):
        raise DispatchError(f"question must be a JSON object: {path}")
    case_id = question.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise DispatchError(f"question is missing case_id: {path}")
    if expected_case_id and case_id != expected_case_id:
        raise DispatchError(f"question case_id {case_id!r} != expected {expected_case_id!r}")
    if not isinstance(question.get("function"), str):
        raise DispatchError(f"question is missing function: {path}")
    return question
