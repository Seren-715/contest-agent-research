from __future__ import annotations

from pathlib import Path

import pytest

from core.paperlib import EvidencePassage, MaterialRecord, index_materials
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


def test_matrix_recognizes_alternative_section_headings():
    texts = [
        "Implementation: An adaptive policy adjusts candidates.",
        "Evaluation: Samples come from corpus Falcon.",
        "Findings: On Falcon the score reaches 75.2 under protocol P.",
        "Caveats: No multilingual evaluation was performed.",
    ]
    record = _record([EvidencePassage(page, 0, len(text), text)
                      for page, text in enumerate(texts, 1)])
    matrix = survey_evidence_matrix([record], query="neural inference")
    for text in texts:
        assert text in matrix


def test_matrix_budget_compacts_without_creating_new_failure():
    records = [_record([EvidencePassage(1, 0, 40, "Method and dataset results need comparison.")], f"paper{i}.pdf")
               for i in range(12)]
    matrix = survey_evidence_matrix(records)
    assert "[12] 完整文件名：paper11.pdf" in matrix
    assert len(matrix) <= 36000
    compact = survey_evidence_matrix(records, max_total_chars=2000)
    assert "[12] 完整文件名：paper11.pdf" in compact
    assert "Method and dataset" in compact
    assert len(compact) <= 2000


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
    assert (settings.workspace_root / result["path"]).read_text(encoding="utf-8") == result["markdown"]


def _with_uncited_paragraph(markdown):
    # Survey citations cover their own paragraph: this claim stands alone.
    return markdown.replace("## 方法与技术脉络\n", "## 方法与技术脉络\n该方法准确率达到0.9。\n\n")


def test_survey_repair_receives_draft_and_preserves_source_context(paper_settings):
    settings, paths = paper_settings
    draft = _with_uncited_paragraph(_markdown([p.name for p in paths]))
    valid = _markdown([p.name for p in paths])
    client = FakeClient([draft, valid])
    result = SurveyHandler().handle({"case_id": "C-fix", "input": "比较论文",
                                    "materials": [str(p) for p in paths]}, settings, client)
    assert len(client.calls) == 2
    assert client.calls[1][2] == {"role": "assistant", "content": draft.strip() + "\n"}
    assert "必须修复的问题" in client.calls[1][3]["content"]
    assert client.calls[1][:2] == client.calls[0]  # same task and evidence
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
    draft = _with_uncited_paragraph(_markdown([p.name for p in paths]))
    monkeypatch.setattr(module, "Deadline", ShortDeadline)
    # Capture the existing draft at finalization; an invalid draft must still
    # face the real finalizer's gate, not trigger an unbudgeted second call.
    monkeypatch.setattr("handlers.reporting.finalize", lambda settings, function, case_id, markdown, paths, **kwargs: markdown)
    client = FakeClient([draft])
    result = SurveyHandler().handle({"case_id": "C-short", "input": "比较论文",
                                    "materials": [str(p) for p in paths]}, settings, client)
    assert len(client.calls) == 1
    assert result == draft.strip() + "\n"


def test_survey_keeps_draft_when_repair_is_worse(paper_settings, monkeypatch):
    settings, paths = paper_settings
    draft = _with_uncited_paragraph(_markdown([p.name for p in paths]))
    client = FakeClient([draft, "这是一份没有证据和格式的回答。"])
    monkeypatch.setattr("handlers.reporting.finalize", lambda settings, function, case_id, markdown, paths, **kwargs: markdown)
    result = SurveyHandler().handle({"case_id": "C-regression", "input": "比较论文",
                                    "materials": [str(p) for p in paths]}, settings, client)
    assert len(client.calls) == 3  # two complete revisions, both worse
    assert result == draft.strip() + "\n"


def test_json_survey_is_rendered_with_paragraph_citations_and_references(paper_settings):
    import json
    settings, paths = paper_settings
    paragraph = "该研究比较了指定条件下的绿地覆盖与地表温度，并说明了数据来源和适用边界。" * 4
    sections = [{"heading": heading, "paragraphs": [{"text": paragraph, "citations": list(range(1, len(paths) + 1))}]}
                for heading in ("引言与研究问题", "方法与技术脉络", "数据与评价指标比较", "结论、局限与冲突")]
    comparison = [{"paper": index, "problem": "地表温度", "method": "遥感", "data": "摘录未提供",
                   "metrics": "摘录未提供", "findings": "降温", "limitations": "摘录未提供"}
                  for index in range(1, len(paths) + 1)]
    reply = json.dumps({"sections": sections, "comparison": comparison}, ensure_ascii=False)
    client = FakeClient([reply])
    result = SurveyHandler().handle({"case_id": "C-json", "input": "比较论文",
                                    "materials": [str(p) for p in paths]}, settings, client)
    assert len(client.calls) == 1
    markdown = result["markdown"]
    assert "## 方法与技术脉络" in markdown and "## 逐篇证据比较" in markdown
    assert paragraph + " [" + ",".join(str(i) for i in range(1, len(paths) + 1)) + "]" in markdown
    for index, path in enumerate(paths, start=1):
        assert f"[{index}] {path.name}" in markdown.partition("## 参考文献")[2]
    assert "sections" in client.calls[0][1]["content"]


def test_json_with_bracket_citations_in_text_is_sent_back_for_revision(paper_settings, monkeypatch):
    import json
    settings, paths = paper_settings
    bad = json.dumps({"sections": [{"heading": "引言与研究问题",
                                    "paragraphs": [{"text": "正文里写了 [1] 编号。", "citations": [1]}]}]},
                     ensure_ascii=False)
    client = FakeClient([bad, _markdown([p.name for p in paths])])
    SurveyHandler().handle({"case_id": "C-struct", "input": "比较论文",
                            "materials": [str(p) for p in paths]}, settings, client)
    assert len(client.calls) == 2
    assert "Survey structure error" in client.calls[1][-1]["content"]
