from __future__ import annotations

import json
import re

import pytest

from core.paperlib import EvidencePassage, MaterialRecord
from core.schemas import validate_answer
from handlers.b_retrieval import RetrievalHandler, _rank_records
from handlers.base import HandlerError
from scripts.evaluate_retrieval import evaluate, evaluate_case
from tests.conftest import FakeClient


def _corpus(monkeypatch, tmp_path, count=26, body="Adaptive decoding for language inference throughput"):
    paths = [tmp_path / ("paper_%02d.pdf" % index) for index in range(count)]
    for path in paths:
        path.write_bytes(b"%PDF-")
    records = [MaterialRecord(path=p, filename=p.name, sha256="test", bytes=6, pages=1,
                              title="Language inference", abstract=body, text=body) for p in paths]
    monkeypatch.setattr("handlers.b_retrieval.resolve_materials", lambda *a, **kw: paths)
    monkeypatch.setattr("handlers.b_retrieval.index_materials", lambda actual, **kwargs: records)
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
    assert len(client.calls) == 2
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


def test_failed_model_with_exclusion_never_guesses_from_overlap(settings, tmp_path, monkeypatch):
    _corpus(monkeypatch, tmp_path, count=2)
    with pytest.raises(HandlerError, match="before any valid"):
        RetrievalHandler().handle({"case_id": "B-01", "input": "adaptive decoding excluding language inference"}, settings, FakeClient(["bad JSON"]))


def test_candidate_budget_is_configurable_and_visible(settings, tmp_path, monkeypatch, caplog):
    _corpus(monkeypatch, tmp_path, count=5)
    monkeypatch.setenv("CONTEST_B_CANDIDATES", "3")
    result = RetrievalHandler().handle({"case_id": "B-01", "input": "adaptive decoding"}, settings, SelectEachBatch())
    assert len(result["papers"]) == 3
    assert "evaluating 3 of 5 papers" in caplog.text


def test_many_chunks_of_same_paper_do_not_displace_other_papers(tmp_path):
    def record(name, count):
        text = "adaptive decoding language inference"
        return MaterialRecord(path=tmp_path / name, filename=name, sha256="test", bytes=5,
                              pages=count, title=name, abstract=text, text=text,
                              passages=tuple(EvidencePassage(i + 1, 0, len(text), text) for i in range(count)))
    records = [record("a.pdf", 50), record("b.pdf", 1), record("c.pdf", 1)]
    ranked = _rank_records("adaptive decoding", records)
    assert len(ranked) == 3
    assert len({item.filename for item, _ in ranked}) == 3
    assert all(score > 0 for _, score in ranked)


def test_failed_later_batch_keeps_already_selected_results(settings, tmp_path, monkeypatch):
    _corpus(monkeypatch, tmp_path, count=3)
    monkeypatch.setenv("CONTEST_B_BATCH_SIZE", "2")

    class FailsLater(SelectEachBatch):
        def complete(self, messages, **kwargs):
            if self.calls:
                raise RuntimeError("upstream unavailable")
            return super().complete(messages, **kwargs)

    result = RetrievalHandler().handle({"case_id": "B-01", "input": "adaptive decoding", "constraints": ["必须符合检索条件"]}, settings, FailsLater())
    assert len(result["papers"]) == 2


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
                assert "QuarrySpec" in prompt
                assert "p.10" in prompt
                return json.dumps({"papers": [{"filename": "z_relevant.pdf", "reason": "中间页原文描述自适应解码及推理吞吐量。"}]}, ensure_ascii=False)
            return '{"papers":[]}'

    result = RetrievalHandler().handle({"case_id": "B-01", "input": "QuarrySpec adaptive decoding throughput"}, settings, EvidenceChoice())
    assert [item["filename"] for item in result["papers"]] == ["z_relevant.pdf"]


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
