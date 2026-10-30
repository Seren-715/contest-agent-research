from __future__ import annotations

from pathlib import Path

import pytest

from core.paperlib import EvidencePassage, MaterialRecord, index_materials
from handlers.base import HandlerError
from handlers.c_survey import SurveyHandler
from handlers.reporting import survey_evidence_matrix
from tests.conftest import FakeClient
from tests.test_handlers_papers import _make_pdf, _markdown, paper_settings


def _record(passages, name="paper.pdf"):
    return MaterialRecord(
        Path(name), name, "a" * 64, 100, 20, "Test paper", "An abstract " * 500,
        "\n".join(p.text for p in passages), passages=tuple(passages),
    )


def test_matrix_preserves_late_results_with_exact_page_offsets():
    text = "The experiment on Dataset-X reports accuracy 81.2 percent under condition Z."
    record = _record([
        EvidencePassage(1, 0, 1100, "An abstract " * 100),
        EvidencePassage(10, 73, 73 + len(text), text),
        EvidencePassage(18, 0, 56, "Limitations: evaluation excludes multilingual deployment."),
    ])
    matrix = survey_evidence_matrix([record], query="experiment accuracy")
    assert text in matrix
    assert "PDF页10，原文字符[73:" in matrix
    assert "81.2" in matrix and "Dataset-X" in matrix and "condition Z" in matrix
    assert "multilingual deployment" in matrix
    assert record.sha256 in matrix
    assert "候选维度=" in matrix


def test_matrix_budget_covers_last_paper_or_fails_explicitly():
    records = [_record([EvidencePassage(1, 0, 40, "Method and dataset results need comparison.")], f"paper{i}.pdf")
               for i in range(12)]
    matrix = survey_evidence_matrix(records)
    assert "[12] 完整文件名：paper11.pdf" in matrix
    assert len(matrix) <= 36000
    with pytest.raises(HandlerError, match="cannot cover all"):
        survey_evidence_matrix(records, max_total_chars=2000)


def test_full_pdf_index_to_matrix_preserves_middle_results_and_late_limits(tmp_path):
    from fpdf import FPDF

    path = tmp_path / "long-experiment.pdf"
    pdf = FPDF()
    for page in range(1, 21):
        pdf.add_page()
        pdf.set_font("Helvetica", size=11)
        if page == 1:
            text = "Abstract\nThis method discusses dataset accuracy and results. " * 12
        elif page == 10:
            text = "Results\nOn Dataset-Z, Method-Q reaches accuracy 83.7 percent using split S and a fixed seed."
        elif page == 20:
            text = "Limitations\nEvaluation excludes multilingual deployment and low-bandwidth networks."
        else:
            text = f"Introduction and background context for experiment page {page}."
        pdf.multi_cell(0, 6, text)
    pdf.output(str(path))
    records = index_materials([path])
    matrix = survey_evidence_matrix(records, query="method dataset accuracy results")
    assert "83.7 percent" in matrix and "Dataset-Z" in matrix and "split S" in matrix
    assert "PDF页10" in matrix
    assert "PDF页20" in matrix and "low-bandwidth networks" in matrix


def test_matrix_quote_window_keeps_keyword_far_into_passage():
    raw = "neutral context " * 55 + "Results: accuracy 93.4 percent on Dataset-Y with no augmentation."
    record = _record([EvidencePassage(10, 50, 50 + len(raw), raw)])
    matrix = survey_evidence_matrix([record], max_total_chars=1300)
    assert "93.4 percent" in matrix
    assert "原文字符[50:" not in matrix


def test_survey_includes_seventh_specified_pdf_and_saves_it(paper_settings):
    settings, paths = paper_settings
    for index in range(2):
        path = paths[0].parent / f"additional{index}.pdf"
        _make_pdf(path, f"Additional cooling experiment {index}",
                  "The method evaluates surface temperature using remote sensing data.")
        paths.append(path)
    client = FakeClient([_markdown([p.name for p in paths])])
    result = SurveyHandler().handle({"case_id": "C-07", "input": "比较研究方法和条件",
                                    "materials": [str(p) for p in paths]}, settings, client)
    assert paths[-1].name in result["markdown"]
    assert paths[-1].name in client.calls[0][1]["content"]
    assert "候选证据矩阵" in client.calls[0][1]["content"]
    assert (settings.workspace_root / result["path"]).read_text(encoding="utf-8") == result["markdown"]


def test_survey_repair_receives_draft_and_preserves_source_context(paper_settings):
    settings, paths = paper_settings
    draft = _markdown([p.name for p in paths]).replace("## 方法与技术脉络\n", "## 方法与技术脉络\n该方法准确率达到0.9。\n")
    valid = _markdown([p.name for p in paths])
    client = FakeClient([draft, valid])
    result = SurveyHandler().handle({"case_id": "C-fix", "input": "比较论文",
                                    "materials": [str(p) for p in paths]}, settings, client)
    assert len(client.calls) == 2
    assert client.calls[1][2] == {"role": "assistant", "content": draft}
    assert "仅修复指出的问题" in client.calls[1][3]["content"]
    assert result["markdown"].strip() == valid.strip()


def test_survey_does_not_start_repair_without_remaining_budget(paper_settings, monkeypatch):
    import handlers.c_survey as module

    class ShortDeadline:
        def __init__(self, seconds):
            pass

        def timeout(self, maximum, **kwargs):
            return 20

        def remaining(self):
            return 20

    settings, paths = paper_settings
    draft = _markdown([p.name for p in paths]).replace("## 方法与技术脉络\n", "## 方法与技术脉络\n该方法准确率达到0.9。\n")
    monkeypatch.setattr(module, "Deadline", ShortDeadline)
    # Capture the existing draft at finalization; an invalid draft must still
    # face the real finalizer's gate, not trigger an unbudgeted second call.
    monkeypatch.setattr(module, "finalize", lambda settings, function, case_id, markdown, paths, **kwargs: markdown)
    client = FakeClient([draft])
    result = SurveyHandler().handle({"case_id": "C-short", "input": "比较论文",
                                    "materials": [str(p) for p in paths]}, settings, client)
    assert len(client.calls) == 1
    assert result == draft


def test_survey_keeps_draft_when_repair_is_worse(paper_settings, monkeypatch):
    import handlers.c_survey as module

    settings, paths = paper_settings
    draft = _markdown([p.name for p in paths]).replace("## 方法与技术脉络\n", "## 方法与技术脉络\n该方法准确率达到0.9。\n")
    client = FakeClient([draft, "这是一份没有证据和格式的回答。"])
    monkeypatch.setattr(module, "finalize", lambda settings, function, case_id, markdown, paths, **kwargs: markdown)
    result = SurveyHandler().handle({"case_id": "C-regression", "input": "比较论文",
                                    "materials": [str(p) for p in paths]}, settings, client)
    assert len(client.calls) == 2
    assert result == draft
