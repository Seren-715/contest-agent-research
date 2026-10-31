from pathlib import Path

import pytest

from core.paperlib import EvidencePassage, MaterialRecord
from handlers.c_survey import SurveyHandler
from handlers.reporting import materials_bundle, survey_evidence_matrix
from tests.conftest import FakeClient
from tests.test_handlers_papers import _markdown, paper_settings


def test_default_survey_uses_exact_materials_without_selection_call(paper_settings, monkeypatch):
    import handlers.c_survey as module
    settings, paths = paper_settings
    monkeypatch.delenv("CONTEST_SURVEY_EVIDENCE_MATRIX", raising=False)
    monkeypatch.setattr(module, "survey_materials_bundle", lambda *args, **kwargs: pytest.fail("matrix is opt-in"))
    client = FakeClient([_markdown([path.name for path in paths])])
    result = SurveyHandler().handle({"case_id": "C-exact", "input": "比较指定论文",
                                    "materials": [str(path) for path in paths]}, settings, client)
    assert len(client.calls) == 1
    assert "候选证据矩阵" not in client.calls[0][1]["content"]
    assert all(path.name in client.calls[0][1]["content"] for path in paths)
    assert (settings.workspace_root / result["path"]).read_text(encoding="utf-8") == result["markdown"]


def test_shared_excerpt_bundle_indexes_middle_page_with_page_locator(paper_settings):
    from fpdf import FPDF
    settings, paths = paper_settings
    path = paths[0].parent / "middle.pdf"
    pdf = FPDF()
    for number in range(24):
        pdf.add_page()
        pdf.set_font("Helvetica", size=10)
        text = "Results: QuarrySpec accuracy 81.2 percent on ZebraSet." if number == 9 else "General background."
        pdf.multi_cell(0, 5, text)
    pdf.output(str(path))
    block, records = materials_bundle([path], query="QuarrySpec ZebraSet accuracy", max_chars=1800)
    assert records[0].indexed_pages == tuple(range(1, 25))
    assert "p.10" in block and "81.2" in block


def test_default_survey_combines_ranked_excerpt_with_results_and_limitations(paper_settings, monkeypatch):
    from fpdf import FPDF
    import handlers.c_survey as module

    settings, paths = paper_settings
    path = paths[0].parent / "coverage.pdf"
    pdf = FPDF()
    for text in (
        "Decoding throughput background. " * 35,
        "Method uses adaptive candidates. " * 35,
        "Dataset contains workload Zebra. " * 35,
        "Results: accuracy is 83.7 percent under condition Q. " * 22,
        "Limitations: no multilingual workloads were evaluated. " * 22,
    ):
        pdf.add_page()
        pdf.set_font("Helvetica", size=10)
        pdf.multi_cell(0, 5, text)
    pdf.output(str(path))
    monkeypatch.delenv("CONTEST_SURVEY_EVIDENCE_MATRIX", raising=False)
    monkeypatch.setattr(module, "finalize", lambda settings, function, case_id, markdown, paths, **kwargs: markdown)
    client = FakeClient(["draft"])
    SurveyHandler().handle({"case_id": "C-coverage", "input": "decoding throughput",
                            "materials": [str(path)]}, settings, client)
    prompt = client.calls[0][1]["content"]
    assert "adaptive candidates" in prompt
    assert "83.7 percent" in prompt
    assert "multilingual workloads" in prompt
    assert "PDF页4" in prompt and "PDF页5" in prompt


def test_low_matrix_budget_reduces_windows_instead_of_rejecting_report():
    records = []
    for number in range(14):
        text = "Method: dataset measurements compare accuracy results under fixed conditions."
        passages = tuple(EvidencePassage(page, 0, len(text), text) for page in range(1, 15))
        records.append(MaterialRecord(Path(f"paper{number}.pdf"), f"paper{number}.pdf", "a" * 64,
                                      100, 14, "Study", text, text, passages=passages))
    result = survey_evidence_matrix(records, query="accuracy", max_total_chars=3000)
    assert len(result) <= 3000
    for number in range(14):
        assert f"paper{number}.pdf" in result
    assert "accuracy" in result


def test_impossibly_small_layout_budget_never_returns_only_source_names():
    text = "Accuracy is 81.2 percent under the stated experimental condition."
    records = [MaterialRecord(Path(f"p{i}.pdf"), f"p{i}.pdf", "a" * 64, 100, 1, "Study", text, text,
                              passages=(EvidencePassage(1, 0, len(text), text),)) for i in range(20)]
    matrix = survey_evidence_matrix(records, max_total_chars=1000)
    assert matrix.count("81.2") == 20


def _survey_request(paths):
    return {"case_id": "C-fact", "input": "比较指定论文", "materials": [str(path) for path in paths]}


def test_fact_check_pass_replaces_draft_when_gate_is_not_worse(paper_settings, monkeypatch):
    settings, paths = paper_settings
    monkeypatch.setenv("CONTEST_SURVEY_FACT_CHECK", "1")
    draft = _markdown([path.name for path in paths])
    revised = draft.replace("在遥感数据上比较了绿地覆盖率与地表温度", "在遥感数据上讨论了地表温度")
    client = FakeClient([draft, revised])
    result = SurveyHandler().handle(_survey_request(paths), settings, client)
    assert len(client.calls) == 2
    assert "逐句事实核对" in client.calls[1][-1]["content"]
    assert client.calls[1][-2] == {"role": "assistant", "content": draft}
    assert "讨论了地表温度" in result["markdown"]


def test_fact_check_pass_is_discarded_when_it_breaks_the_structural_gate(paper_settings, monkeypatch):
    settings, paths = paper_settings
    monkeypatch.setenv("CONTEST_SURVEY_FACT_CHECK", "1")
    draft = _markdown([path.name for path in paths])
    client = FakeClient([draft, "太短的修订"])
    result = SurveyHandler().handle(_survey_request(paths), settings, client)
    assert len(client.calls) == 2
    assert result["markdown"].strip() == draft.strip()


def test_fact_check_pass_is_skipped_without_enough_time_and_when_disabled(paper_settings, monkeypatch):
    settings, paths = paper_settings
    draft = _markdown([path.name for path in paths])
    client = FakeClient([draft])
    SurveyHandler().handle(_survey_request(paths), settings, client)  # disabled by default in tests
    assert len(client.calls) == 1
    monkeypatch.setenv("CONTEST_SURVEY_FACT_CHECK", "1")
    monkeypatch.setattr("handlers.c_survey.FACT_CHECK_MIN_SECONDS", 10_000)
    client = FakeClient([draft])
    SurveyHandler().handle(_survey_request(paths), settings, client)
    assert len(client.calls) == 1
