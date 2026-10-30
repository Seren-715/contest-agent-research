from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from core.solve import solve_task


def test_non_chat_result_is_direct_without_answer(settings) -> None:
    settings = dataclasses.replace(settings, mock_llm=True)
    result = solve_task(
        {
            "case_id": "B-01",
            "function": "Literature_Retrieval",
            "input": "城市绿地与地表温度",
            "materials": [],
            "constraints": [],
        },
        settings,
    )
    assert result["case_id"] == "B-01"
    assert "answer" not in result
    assert result["papers"] == []

    on_disk = json.loads((settings.team_dir / "B-01.json").read_text(encoding="utf-8"))
    assert on_disk == {"case_id": "B-01", "papers": []}


def test_d_failure_returns_honest_schema_valid_fallback(settings) -> None:
    case_dir = settings.d_materials_root / "D-01"
    case_dir.mkdir(parents=True)
    (case_dir / "protocol.json").write_text(
        json.dumps(
            {
                "case_id": "D-01",
                "metrics": [
                    {"name": "Macro-F1", "unit": "分数"},
                    {"name": "Accuracy", "unit": "%"},
                    {"name": "运行时间", "unit": "秒"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (case_dir / "method.md").write_text("method", encoding="utf-8")
    (case_dir / "sample.json").write_text('{"case_id":"D-01"}', encoding="utf-8")

    settings = dataclasses.replace(settings, mock_llm=True)
    result = solve_task(
        {
            "case_id": "D-01",
            "function": "Reproduction",
            "input": "复现实验",
            "materials": [],
            "constraints": [],
        },
        settings,
    )
    assert result["status"] == "failed"
    assert len(result["metrics"]) == 3
    assert all(item["observed"] is None for item in result["metrics"])
    assert result["figures"] == []
    assert "answer" not in result

    on_disk = json.loads((settings.team_dir / "D-01.json").read_text(encoding="utf-8"))
    assert set(on_disk) == {"case_id", "status", "metrics", "figures"}
