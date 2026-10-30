from __future__ import annotations

import json
from pathlib import Path

from scripts.validate_local import validate_submission


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _report(root: Path, case_id: str, function: str, team: str = "team") -> dict:
    name = f"{case_id}-综述-{team}.md" if function == "Survey" else f"{case_id}-灵感Idea报告-{team}.md"
    path = root / "data" / "expert" / name
    markdown = "# 报告\n\n" + ("正文" * 500) + "\n\n## 参考文献\n[1] paper.pdf\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return {"case_id": case_id, "path": f"data/expert/{name}", "markdown": markdown}


def test_validate_local_accepts_complete_submission(tmp_path: Path) -> None:
    team = "team"
    team_dir = tmp_path / "data" / team
    _write_json(team_dir / "A-01.json", {"case_id": "A-01", "answer": "答案", "clarifying_question": "问题？"})
    for case_id in ("B-01", "B-02", "B-03"):
        _write_json(team_dir / f"{case_id}.json", {"case_id": case_id, "papers": []})
    for case_id in ("C-01", "C-03"):
        _write_json(team_dir / f"{case_id}.json", _report(tmp_path, case_id, "Survey"))
    for case_id in ("D-01", "D-02", "D-05"):
        _write_json(
            team_dir / f"{case_id}.json",
            {
                "case_id": case_id,
                "status": "failed",
                "metrics": [
                    {"name": "m1", "unit": "u", "reference": None, "observed": None, "relative_error": None},
                    {"name": "m2", "unit": "u", "reference": None, "observed": None, "relative_error": None},
                    {"name": "m3", "unit": "u", "reference": None, "observed": None, "relative_error": None},
                ],
                "figures": [],
            },
        )
    for case_id in ("E-01", "E-02"):
        _write_json(team_dir / f"{case_id}.json", _report(tmp_path, case_id, "Idea"))

    assert validate_submission(tmp_path, team) == []


def test_validate_local_reports_missing_case(tmp_path: Path) -> None:
    team_dir = tmp_path / "data" / "team"
    team_dir.mkdir(parents=True)
    _write_json(team_dir / "A-01.json", {"case_id": "A-01", "answer": "答案", "clarifying_question": "问题？"})
    errors = validate_submission(tmp_path, "team")
    assert any("B-01" in error for error in errors)
    assert any("C-03" in error for error in errors)
