from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.dispatch import DispatchError, load_dispatch, validate_dispatch


def make_dispatch() -> dict:
    functions = {
        "A": "Chat",
        "B": "Literature_Retrieval",
        "C": "Survey",
        "D": "Reproduction",
        "E": "Idea",
    }
    counts = {"A": 1, "B": 3, "C": 2, "D": 3, "E": 2}
    cases = []
    ids = []
    for prefix, count in counts.items():
        for index in range(1, count + 1):
            case_id = f"{prefix}-{index:02d}"
            ids.append(case_id)
            cases.append(
                {
                    "case_id": case_id,
                    "function": functions[prefix],
                    "title": case_id,
                    "question_file": f"data/team/assigned/{case_id}.json",
                    "question_sha256": "0" * 64,
                }
            )
    return {"dispatch_id": "d-test", "team": "team", "case_ids": ids, "cases": cases}


def test_load_and_validate(tmp_path: Path) -> None:
    path = tmp_path / "dispatch.json"
    path.write_text(json.dumps(make_dispatch(), ensure_ascii=False), encoding="utf-8")
    dispatch = load_dispatch(path)
    assert len(dispatch.case_ids) == 11
    validate_dispatch(dispatch)


def test_missing_case_is_rejected(tmp_path: Path) -> None:
    raw = make_dispatch()
    raw["case_ids"] = raw["case_ids"][:-1]
    path = tmp_path / "dispatch.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(DispatchError):
        load_dispatch(path)


def test_wrong_category_count_is_rejected(tmp_path: Path) -> None:
    raw = make_dispatch()
    raw["cases"][0]["function"] = "Survey"
    path = tmp_path / "dispatch.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(DispatchError):
        load_dispatch(path)
