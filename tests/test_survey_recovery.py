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


def _crowded_paper(path, index):
    """Abstract, contributions and conclusion compete with many dense pages."""
    from fpdf import FPDF
    pdf = FPDF()
    pdf.set_font("Helvetica", size=9)
    pdf.add_page()
    pdf.multi_cell(0, 4, f"Method{index}: Goodput Oriented Serving\nAbstract\nWe present Method{index}. "
                   f"HEADLINE{index}: latency drops by up to {index + 2}.2x versus plain decoding.\n"
                   "1 Introduction\n" + "Serving is costly. " * 30
                   + f"Our contributions are: CONTRIB{index} adaptive proposal length per batch.")
    for page in range(8):
        pdf.add_page()
        pdf.multi_cell(0, 4, ("\nResults show speculative decoding latency throughput with the proposed method "
                              "on the ShareGPT dataset improves 12.5% accuracy over the baseline.\nMethod details: "
                              "we propose a draft model verification framework; limitation: batch size.\n") * 12)
    pdf.add_page()
    pdf.multi_cell(0, 4, f"7 Conclusion\nCONCL{index}: goodput improves across workloads. " + "Future work. " * 5)
    pdf.output(str(path))
    return path


def test_survey_prompt_always_carries_abstract_contributions_and_conclusion(paper_settings, monkeypatch):
    import handlers.c_survey as module
    settings, paths = paper_settings
    papers = [_crowded_paper(paths[0].parent / f"crowded{i}.pdf", i) for i in range(5)]
    monkeypatch.delenv("CONTEST_SURVEY_EVIDENCE_MATRIX", raising=False)
    monkeypatch.setattr(module, "finalize", lambda settings, function, case_id, markdown, paths, **kwargs: markdown)
    client = FakeClient(["draft"])
    SurveyHandler().handle({"case_id": "C-core", "input": "比较 speculative decoding 的 latency 与 throughput",
                            "materials": [str(path) for path in papers]}, settings, client)
    prompt = client.calls[0][1]["content"]
    for index in range(5):
        assert f"HEADLINE{index}" in prompt
        assert f"CONTRIB{index}" in prompt
        assert f"CONCL{index}" in prompt
    assert "（结论，PDF页10）" in prompt
    assert "核心事实" in prompt


def test_core_passages_report_what_they_cannot_locate():
    from handlers.reporting import core_evidence, core_passages
    record = MaterialRecord(path=Path("x.pdf"), filename="x.pdf", sha256="", bytes=0, pages=2,
                            title="X", abstract="", text="",
                            passages=(EvidencePassage(1, 0, 40, "Plain first page without headings here."),
                                      EvidencePassage(2, 0, 30, "More text on the second page.")))
    assert [label for label, _, _ in core_passages(record)] == ["首页"]
    block = core_evidence([record])
    assert "未定位：贡献陈述、结论" in block and "[1] 完整文件名：x.pdf" in block


def test_core_passages_use_discussion_when_no_conclusion_and_stop_at_references():
    from handlers.reporting import core_passages
    pages = [
        (1, "Title\nAbstract\nWe introduce Foo, which is 2.0x faster.\n1 Introduction\nBackground text."),
        (2, "Equal contribution. In summary, the paper makes the following contributions: a goodput metric."),
        (3, "Body."), (4, "Body."),
        (5, "6. Discussion\nWe presented Foo for serving.\nReferences\n[1] Someone. A paper. 2020."),
    ]
    record = MaterialRecord(path=Path("f.pdf"), filename="f.pdf", sha256="", bytes=0, pages=5, title="Foo",
                            abstract="", text="",
                            passages=tuple(EvidencePassage(page, 0, len(text), text) for page, text in pages))
    parts = {label: (page, quote) for label, page, quote in core_passages(record)}
    assert parts["摘要"][1].startswith("We introduce Foo, which is 2.0x faster.")
    assert parts["摘要"][1] == "We introduce Foo, which is 2.0x faster."
    assert parts["贡献陈述"][0] == (2, 3)  # the window continues onto page 3
    assert "[PDF页3]" in parts["贡献陈述"][1]
    assert parts["贡献陈述"][1].startswith("In summary, the paper makes the following contributions: a goodput metric.")
    assert parts["结论"][0] == (5,)
    assert "We presented Foo" in parts["结论"][1] and "Someone" not in parts["结论"][1]


def test_core_window_never_joins_a_non_adjacent_page_and_labels_every_page():
    from handlers.reporting import core_evidence, core_passages
    # Page 2 was not indexed (time limit): page 3 must not appear as page 1.
    pages = [(1, "Abstract\nShort abstract of Foo."), (3, "PAGE-THREE results 9.9x."), (4, "More."),
             (5, "Body."), (6, "7 Conclusion\nFoo works."), (7, "Next page text.")]
    record = MaterialRecord(path=Path("g.pdf"), filename="g.pdf", sha256="", bytes=0, pages=7, title="Foo",
                            abstract="", text="",
                            passages=tuple(EvidencePassage(page, 0, len(text), text) for page, text in pages))
    parts = {label: (pages_, quote) for label, pages_, quote in core_passages(record)}
    assert parts["摘要"] == ((1,), "Short abstract of Foo.")
    assert parts["结论"][0] == (6, 7) and "[PDF页7] Next page text." in parts["结论"][1]
    block = core_evidence([record])
    assert "（摘要，PDF页1）Short abstract of Foo." in block
    assert "（结论，PDF页6-7）" in block
    assert "PAGE-THREE" not in block


def test_core_window_does_not_continue_into_references_on_the_next_page():
    from handlers.reporting import core_passages
    pages = [(1, "Abstract\nFoo."), (2, "B."), (3, "C."), (4, "8 Conclusion\nFoo wins 3.2x."),
             (5, "References\n[1] Someone. 2020.")]
    record = MaterialRecord(path=Path("h.pdf"), filename="h.pdf", sha256="", bytes=0, pages=5, title="Foo",
                            abstract="", text="",
                            passages=tuple(EvidencePassage(page, 0, len(text), text) for page, text in pages))
    parts = {label: (used, quote) for label, used, quote in core_passages(record)}
    assert parts["结论"] == ((4,), "8 Conclusion Foo wins 3.2x.")
