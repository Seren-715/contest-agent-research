from __future__ import annotations

import json
import re

import pytest

from core.paperlib import EvidencePassage, MaterialRecord
from core.schemas import validate_answer
from handlers.b_retrieval import RetrievalHandler
from handlers.base import HandlerError
from scripts.evaluate_retrieval import evaluate, evaluate_case
from tests.conftest import FakeClient
from pathlib import Path


def _corpus(monkeypatch, tmp_path, count=26, body="Adaptive decoding for language inference throughput"):
    paths = [tmp_path / ("paper_%02d.pdf" % index) for index in range(count)]
    for path in paths:
        path.write_bytes(b"%PDF-")
    records = [MaterialRecord(path=p, filename=p.name, sha256="test", bytes=6, pages=1,
                              title="Language inference", abstract=body, text=body) for p in paths]
    monkeypatch.setattr("handlers.b_retrieval.resolve_materials", lambda *a, **kw: paths)
    monkeypatch.setattr("handlers.b_retrieval.index_materials",
                        lambda actual, **kwargs: [r for r in records if r.path in set(actual)])
    return paths


class SelectEachBatch:
    def __init__(self):
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append(messages)
        names = re.findall(r"\d+\. (paper_\d+\.pdf)（", messages[-1]["content"])
        return json.dumps({"papers": [{"filename": name, "reason": "正文研究自适应解码与语言推理吞吐量。"} for name in names]}, ensure_ascii=False)


def test_all_files_after_twentieth_and_more_than_five_results(settings, tmp_path, monkeypatch):
    paths = _corpus(monkeypatch, tmp_path)
    client = SelectEachBatch()
    result = RetrievalHandler().handle({"case_id": "B-01", "input": "adaptive decoding language inference"}, settings, client)
    assert {item["filename"] for item in result["papers"]} == {p.name for p in paths}
    assert len(client.calls) == 1  # one selection call judges every candidate together
    validate_answer("Literature_Retrieval", result)


def test_valid_empty_model_choice_never_gets_keyword_fallback(settings, tmp_path, monkeypatch):
    _corpus(monkeypatch, tmp_path, count=2)
    result = RetrievalHandler().handle({"case_id": "B-01", "input": "adaptive decoding language inference"}, settings, FakeClient(['{"papers":[]}']))
    assert result["papers"] == []


def test_same_topic_but_excluded_task_stays_empty(settings, tmp_path, monkeypatch):
    _corpus(monkeypatch, tmp_path, count=2, body="Speculative decoding uses draft model language inference")
    client = FakeClient(['{"papers":[]}'])
    result = RetrievalHandler().handle({"case_id": "B-01", "input": "speculative decoding without draft model", "constraints": ["Exclude methods requiring a separate draft model"]}, settings, client)
    assert result["papers"] == []
    assert "Exclude methods requiring" in client.calls[0][-1]["content"]


def test_semantic_selection_does_not_require_token_overlap(settings, tmp_path, monkeypatch):
    paths = _corpus(monkeypatch, tmp_path, count=1, body="Unrelated lexical representation")
    client = FakeClient([json.dumps({"papers": [{"filename": paths[0].name, "reason": "跨语言语义匹配确认其覆盖当前检索任务。"}]}, ensure_ascii=False)])
    result = RetrievalHandler().handle({"case_id": "B-01", "input": "speculative decoding"}, settings, client)
    assert result["papers"][0]["filename"] == paths[0].name


def test_short_specific_reason_is_not_silently_discarded(settings, tmp_path, monkeypatch):
    paths = _corpus(monkeypatch, tmp_path, count=1)
    client = FakeClient([json.dumps({"papers": [{"filename": paths[0].name, "reason": "研究推理解码加速"}]}, ensure_ascii=False)])
    result = RetrievalHandler().handle({"case_id": "B-01", "input": "decoding inference"}, settings, client)
    assert result["papers"] == [{"filename": paths[0].name, "reason": "研究推理解码加速"}]
    validate_answer("Literature_Retrieval", result)


def test_failed_model_with_exclusion_never_guesses_from_overlap(settings, tmp_path, monkeypatch):
    _corpus(monkeypatch, tmp_path, count=2)
    with pytest.raises(HandlerError, match="before any valid"):
        RetrievalHandler().handle({"case_id": "B-01", "input": "adaptive decoding excluding language inference"}, settings, FakeClient(["bad JSON"]))


def test_real_pdf_after_twentieth_with_unique_middle_page_evidence(settings):
    from fpdf import FPDF

    library = settings.data_dir / "papers"
    library.mkdir(parents=True)
    for i in range(21):
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", size=10)
        pdf.multi_cell(0, 5, "Botany observations of tropical forests.")
        pdf.output(str(library / ("a_%02d.pdf" % i)))
    pdf = FPDF()
    for i in range(24):
        pdf.add_page()
        pdf.set_font("Helvetica", size=10)
        text = "QuarrySpec adaptive decoding increases language inference throughput." if i == 9 else "Introduction and general background."
        pdf.multi_cell(0, 5, text)
    pdf.output(str(library / "z_relevant.pdf"))

    class EvidenceChoice:
        def complete(self, messages, **kwargs):
            prompt = messages[-1]["content"]
            if "z_relevant.pdf" in prompt:
                assert "QuarrySpec" in prompt  # the middle page reached the prompt
                return json.dumps({"papers": [{"filename": "z_relevant.pdf", "reason": "中间页原文描述自适应解码及推理吞吐量。"}]}, ensure_ascii=False)
            return '{"papers":[]}'

    result = RetrievalHandler().handle({"case_id": "B-01", "input": "QuarrySpec adaptive decoding throughput"}, settings, EvidenceChoice())
    assert [item["filename"] for item in result["papers"]] == ["z_relevant.pdf"]


def test_readable_pdf_with_leading_bytes_is_not_discarded(settings):
    from fpdf import FPDF

    library = settings.data_dir / "papers"
    library.mkdir(parents=True)
    path = library / "leading.pdf"
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)
    pdf.multi_cell(0, 5, "Adaptive decoding improves language inference throughput.")
    pdf.output(str(path))
    path.write_bytes(b"leading bytes\n" + path.read_bytes())
    client = FakeClient(['{"papers":[{"filename":"leading.pdf","reason":"原文研究自适应解码和语言推理吞吐量。"}]}'])
    result = RetrievalHandler().handle({"case_id": "B-01", "input": "adaptive decoding throughput"}, settings, client)
    assert [item["filename"] for item in result["papers"]] == ["leading.pdf"]


def test_invalid_pdf_headers_report_failure_instead_of_empty_results(settings):
    library = settings.data_dir / "papers"
    library.mkdir(parents=True)
    (library / "broken.pdf").write_text("not a PDF", encoding="utf-8")
    with pytest.raises(HandlerError, match="no recognizable PDFs"):
        RetrievalHandler().handle({"case_id": "B-01", "input": "adaptive decoding"}, settings, FakeClient([]))


def test_official_formula_penalizes_false_hits_and_deduplicates_case():
    result = evaluate_case(["a.pdf", "b.pdf", "c.pdf"], ["A.PDF", "a.pdf", "wrong.pdf"])
    assert result["H"] == 1 and result["N"] == 3 and result["U"] == 1
    assert result["returned"] == 2
    assert result["official_score"] == 4.25
    assert result["paper_recall"] == pytest.approx(1 / 3)


def test_missing_case_is_zero_not_dropped_from_average():
    result = evaluate([{"case_id": "B-01", "relevant": ["a.pdf"]}, {"case_id": "B-02", "relevant": ["b.pdf"]}],
                      [{"case_id": "B-01", "papers": [{"filename": "a.pdf", "reason": "test"}]}])
    assert result["macro_official_score"] == 7.5
    assert result["missing_predictions"] == ["B-02"]


def test_invalid_empty_gold_not_treated_as_perfect_recall():
    with pytest.raises(ValueError):
        evaluate_case([], [])


def test_filename_match_without_readable_evidence_is_not_returned(settings, monkeypatch):
    library = settings.data_dir / "papers"
    library.mkdir(parents=True)
    (library / "QuarrySpec.pdf").write_bytes(b"%PDF-" + b"x" * 100)
    monkeypatch.setenv("CONTEST_PDF_MAX_BYTES", "20")
    class MustNotCall:
        def complete(self, *args, **kwargs):
            pytest.fail("unread PDF must not become an evidence candidate")
    with pytest.raises(HandlerError, match="no extractable evidence"):
        RetrievalHandler().handle({"case_id": "B-01", "input": "QuarrySpec"}, settings, MustNotCall())


def test_real_model_client_accepts_and_forwards_retrieval_timeouts(settings, tmp_path, monkeypatch):
    from core.llm import ModelClient
    _corpus(monkeypatch, tmp_path, count=2)
    timeouts = []
    def raw(self, messages, *, timeout=None, **kwargs):
        timeouts.append(timeout)
        if len(timeouts) == 1:
            content = '{"keywords":["adaptive","decoding","inference"]}'
        else:
            content = '{"papers":[{"filename":"paper_00.pdf","reason":"原文提出自适应解码并比较语言推理吞吐量。"}]}'
        return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
    monkeypatch.setattr(ModelClient, "complete_raw", raw)
    result = RetrievalHandler().handle({"case_id": "B-01", "input": "自适应解码推理吞吐量"},
                                       settings, ModelClient(None, None, None))
    assert len(result["papers"]) == 1
    assert len(timeouts) == 2
    assert all(0 < timeout <= settings.request_timeout for timeout in timeouts)


def test_query_expansion_retries_once_then_gives_up_with_warning(caplog):
    from core.budget import Deadline
    from handlers.b_retrieval import _expand_query

    class Flaky:
        def __init__(self, replies):
            self.replies = list(replies)
            self.calls = 0

        def complete(self, *args, **kwargs):
            self.calls += 1
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

    recovered = Flaky([RuntimeError("upstream"), '{"keywords":["adaptive","decoding"]}'])
    assert _expand_query(recovered, "自适应解码", Deadline(300), 30) == ["adaptive", "decoding"]
    assert recovered.calls == 2

    failing = Flaky(["not json", "still not json"])
    with caplog.at_level("WARNING"):
        assert _expand_query(failing, "自适应解码", Deadline(300), 30) == []
    assert failing.calls == 2
    assert "B query expansion produced no keywords" in caplog.text


def test_all_model_failures_do_not_return_unverified_lexical_matches(settings, tmp_path, monkeypatch):
    _corpus(monkeypatch, tmp_path, count=2)
    with pytest.raises(HandlerError, match="before any valid"):
        RetrievalHandler().handle({"case_id": "B-01", "input": "adaptive decoding language inference"},
                                  settings, FakeClient(["bad JSON"]))


def test_returned_name_is_matched_case_insensitively_and_emitted_canonically(settings, tmp_path, monkeypatch):
    _corpus(monkeypatch, tmp_path, count=2)
    reply = '{"papers":[{"filename":"PAPER_00.PDF","reason":"正文研究自适应解码与语言推理吞吐量。"}]}'
    result = RetrievalHandler().handle({"case_id": "B-01", "input": "adaptive decoding language inference"},
                                       settings, FakeClient([reply]))
    assert [item["filename"] for item in result["papers"]] == ["paper_00.pdf"]


@pytest.mark.parametrize("query", [
    "adaptive decoding language inference published after 2022",
    "adaptive decoding language inference, only on English benchmarks",
    "自适应解码 语言推理 2022年以后的论文",
    "自适应解码 语言推理，仅限使用公开数据集",
    "adaptive decoding language inference at least 7B parameters",
    "adaptive decoding language inference during the last five years",
])
def test_lexical_fallback_is_refused_when_the_query_states_a_condition(settings, tmp_path, monkeypatch, query):
    _corpus(monkeypatch, tmp_path, count=2)
    with pytest.raises(HandlerError, match="before any valid"):
        RetrievalHandler().handle({"case_id": "B-01", "input": query}, settings, FakeClient(["bad JSON"]))


def test_shortlist_size_is_configurable_and_visible(settings, tmp_path, monkeypatch, caplog):
    _corpus(monkeypatch, tmp_path, count=5)
    monkeypatch.setenv("CONTEST_B_SHORTLIST", "3")
    client = SelectEachBatch()
    result = RetrievalHandler().handle({"case_id": "B-01", "input": "adaptive decoding"}, settings, client)
    assert len(result["papers"]) == 3
    assert "deep-reading 3 of 5 scanned papers" in caplog.text


def test_every_paper_is_scanned_before_the_shortlist(settings, tmp_path, monkeypatch):
    paths = _corpus(monkeypatch, tmp_path, count=130)
    seen = []
    original = __import__("handlers.b_retrieval", fromlist=["x"]).index_materials

    def spy(actual, **kwargs):
        seen.append(list(actual))
        return original(actual, **kwargs)

    monkeypatch.setattr("handlers.b_retrieval.index_materials", spy)
    RetrievalHandler().handle({"case_id": "B-01", "input": "adaptive decoding"}, settings, SelectEachBatch())
    assert seen[0] == paths            # first pages of all 130 papers
    assert len(seen[1]) == 60          # default shortlist read deeply


def test_facet_keyword_brings_a_globally_weak_paper_into_the_shortlist():
    from handlers.b_retrieval import _candidate_papers
    from core.paperlib import Paper
    papers = [Paper(Path(f"g{i:02d}.pdf"), f"g{i:02d}.pdf", "speculative decoding latency serving " * 3)
              for i in range(10)]
    papers.append(Paper(Path("tree.pdf"), "tree.pdf", "token tree verification"))
    shortlist = _candidate_papers("speculative decoding latency serving tree verification",
                                  ["tree verification"], papers, size=4)
    assert "tree.pdf" in {paper.filename for paper in shortlist}
    assert len(shortlist) == 4


def test_selection_is_retried_once_after_a_failed_call(settings, tmp_path, monkeypatch):
    paths = _corpus(monkeypatch, tmp_path, count=2)
    reply = json.dumps({"papers": [{"filename": paths[0].name, "reason": "正文研究自适应解码。"}]}, ensure_ascii=False)
    client = FakeClient(["truncated {", reply])
    result = RetrievalHandler().handle({"case_id": "B-01", "input": "adaptive decoding"}, settings, client)
    assert [item["filename"] for item in result["papers"]] == [paths[0].name]
    assert len(client.calls) == 2
