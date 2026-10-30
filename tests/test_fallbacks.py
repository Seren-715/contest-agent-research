from __future__ import annotations

import json
from pathlib import Path

import pytest

import core.solve as solve_module
from core.llm import ModelError
from core.execution import ExecutionCancelled
from core.schemas import report_body, validate_answer


class FailingClient:
    def complete(self, *args, **kwargs):
        raise ModelError("gateway unavailable")

    def complete_raw(self, *args, **kwargs):
        raise ModelError("gateway unavailable")


def _make_pdf(path: Path, text: str) -> None:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 6, text)
    pdf.output(str(path))


@pytest.fixture
def paper_paths(tmp_path) -> list[Path]:
    library = tmp_path / "data" / "论文库"
    library.mkdir(parents=True)
    paths = []
    for index in range(3):
        path = library / f"paper_{index + 1}.pdf"
        _make_pdf(path, (f"Paper {index + 1}: urban green space and surface temperature. "
                         "The study compares vegetation coverage across districts using remote sensing. "
                         "Cooling effects depend on season and measurement conditions. ") * 4)
        paths.append(path)
    return paths


def test_chat_fallback_is_schema_valid(settings, monkeypatch) -> None:
    settings = settings
    monkeypatch.setattr(solve_module, "model_client", lambda _settings: FailingClient())
    result = solve_module.solve_task(
        {"case_id": "A-01", "function": "Chat", "input": "解释光照与生长。", "constraints": []},
        settings,
    )
    assert result["case_id"] == "A-01"
    assert result["answer"]
    assert result["clarifying_question"]
    assert (settings.team_dir / "A-01.json").exists()


def test_survey_fallback_cites_all_materials(settings, monkeypatch, paper_paths) -> None:
    monkeypatch.setattr(solve_module, "model_client", lambda _settings: FailingClient())
    result = solve_module.solve_task(
        {
            "case_id": "C-01",
            "function": "Survey",
            "input": "比较城市绿地与地表温度研究。",
            "materials": [str(path) for path in paper_paths],
            "constraints": [],
        },
        settings,
    )
    assert "## 参考文献" in result["markdown"]
    assert 800 <= len(report_body(result["markdown"])) <= 5000
    for index, path in enumerate(paper_paths, start=1):
        assert f"[{index}" in result["markdown"]
        assert path.name in result["markdown"]
    saved = settings.workspace_root / result["path"]
    assert saved.read_text(encoding="utf-8") == result["markdown"]
    validate_answer("Survey", {key: value for key, value in result.items() if key != "answer"})


def test_idea_generation_failure_does_not_publish_template(settings, monkeypatch, paper_paths) -> None:
    monkeypatch.setattr(solve_module, "model_client", lambda _settings: FailingClient())
    with pytest.raises(solve_module.SolveError, match="gateway unavailable"):
        solve_module.solve_task(
            {
                "case_id": "E-01",
                "function": "Idea",
                "input": "提出一个降低步行热暴露的路线规划方案。",
                "materials": [str(path) for path in paper_paths],
                "constraints": [],
            },
            settings,
        )
    assert not (settings.team_dir / "E-01.json").exists()
    assert not list(settings.expert_dir.glob("E-01*.md"))


def test_short_survey_excerpts_are_not_padded(settings, monkeypatch, paper_paths) -> None:
    for path in paper_paths:
        _make_pdf(path, "A short source passage.")
    monkeypatch.setattr(solve_module, "model_client", lambda _settings: FailingClient())
    with pytest.raises(solve_module.SolveError, match="gateway unavailable"):
        solve_module.solve_task(
            {"case_id": "C-01", "function": "Survey", "input": "综述", "materials": [str(path) for path in paper_paths]},
            settings,
        )
    assert not (settings.team_dir / "C-01.json").exists()
    assert not list(settings.expert_dir.glob("C-01*.md"))


def test_cancelled_generation_does_not_enter_report_fallback(settings, monkeypatch, paper_paths) -> None:
    class CancelledClient:
        def complete(self, *args, **kwargs):
            raise ExecutionCancelled("case execution was cancelled")

    monkeypatch.setattr(solve_module, "model_client", lambda _settings: CancelledClient())
    monkeypatch.setattr(solve_module, "_fallback_report", lambda *args: pytest.fail("cancelled task entered fallback"))
    with pytest.raises(ExecutionCancelled):
        solve_module.solve_task(
            {"case_id": "C-01", "function": "Survey", "input": "综述", "materials": [str(path) for path in paper_paths]},
            settings,
        )
    assert not (settings.team_dir / "C-01.json").exists()
