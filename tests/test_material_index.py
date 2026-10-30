from pathlib import Path

import pytest
from pypdf import PdfReader

from core.execution import ExecutionCancelled, ExecutionControl, execution_context
from core.budget import Deadline
from core.paperlib import (
    MaterialResolutionError, evidence_excerpt, index_materials, resolve_materials, terms,
)


def make_pdf(path, pages):
    from fpdf import FPDF
    pdf = FPDF()
    pdf.set_auto_page_break(False)
    for text in pages:
        pdf.add_page()
        pdf.set_font("Helvetica", size=10)
        pdf.multi_cell(180, 5, text)
    pdf.output(str(path))


def test_index_recovers_middle_page_and_exact_raw_spans(tmp_path):
    path = tmp_path / "study.pdf"
    pages = ["Background context for this independent experiment." for _ in range(24)]
    pages[9] = "QuarrySpec evaluates ZebraSet with accuracy 81.2 percent in condition Z."
    make_pdf(path, pages)
    record, = index_materials([path])
    assert record.indexed_pages == tuple(range(1, 25))
    assert not record.warnings
    raw_pages = PdfReader(str(path)).pages
    for span in record.passages:
        assert raw_pages[span.page - 1].extract_text()[span.start:span.end] == span.text
    excerpt = evidence_excerpt(record, "QuarrySpec ZebraSet accuracy", max_chars=500)
    assert "81.2" in excerpt and "p.10" in excerpt
    assert len(excerpt) <= 500
    assert len(record.sha256) == 64


def test_character_budget_is_shared_across_pages_not_consumed_by_intro(tmp_path):
    path = tmp_path / "long.pdf"
    make_pdf(path, ["INTRO " * 500, "Middle conclusion.", "Tail conclusion."])
    record, = index_materials([path], max_chars=300)
    assert "Tail conclusion" in record.text
    assert "Middle conclusion" in record.text
    assert "page_char_limit:1" in record.warnings
    assert len(record.text) <= 302  # two page separators


def test_page_limit_is_explicit_and_samples_across_document(tmp_path):
    path = tmp_path / "pages.pdf"
    make_pdf(path, [f"Page {number} of this study." for number in range(24)])
    record, = index_materials([path], max_pages=5)
    assert len(record.indexed_pages) == 5
    assert record.indexed_pages[0] == 1 and record.indexed_pages[-1] == 24
    assert "pdf_page_limit" in record.warnings


def test_idea_legacy_bundle_keeps_head_eight_tail_four_pages(tmp_path):
    from handlers.reporting import materials_bundle
    path = tmp_path / "idea.pdf"
    make_pdf(path, [f"Source page {number + 1}." for number in range(24)])
    _, records = materials_bundle([path])
    assert records[0].indexed_pages == tuple(list(range(1, 9)) + list(range(21, 25)))


def test_byte_limit_prevents_reader_construction(tmp_path, monkeypatch):
    path = tmp_path / "huge.pdf"
    path.write_bytes(b"%PDF-" + b"x" * 100)
    monkeypatch.setenv("CONTEST_PDF_MAX_BYTES", "20")
    monkeypatch.setattr("core.paperlib.PdfReader", lambda *_: pytest.fail("must not parse oversized file"))
    record, = index_materials([path])
    assert "pdf_byte_limit" in record.warnings
    assert not record.text and not record.passages


def test_cancelled_index_does_not_swallow_cancellation(tmp_path):
    control = ExecutionControl()
    with execution_context(control):
        control.cancel()
        with pytest.raises(ExecutionCancelled):
            index_materials([tmp_path / "any.pdf"])


def test_exhausted_work_budget_does_not_start_pdf_parse(tmp_path, monkeypatch):
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-")
    monkeypatch.setattr("core.paperlib.PdfReader", lambda *_: pytest.fail("no parsing budget remains"))
    record, = index_materials([path], deadline=Deadline(0))
    assert "index_budget_exhausted" in record.warnings
    # Even fallback paths with no local Deadline must honor the outer reserve.
    with execution_context(ExecutionControl(seconds=10)):
        record, = index_materials([path])
        assert "index_budget_exhausted" in record.warnings


def test_chinese_phrase_matches_inside_unsegmented_sentence():
    assert {"检索", "索增", "增强"} <= set(terms("检索增强"))
    assert set(terms("检索增强")) & set(terms("本文提出检索增强生成方法"))


def library(tmp_path):
    data = tmp_path / "data"
    root = data / "papers"
    root.mkdir(parents=True)
    (root / "allowed.pdf").write_bytes(b"%PDF-")
    return data, root


def test_explicit_missing_never_falls_back_even_in_B(tmp_path):
    data, root = library(tmp_path)
    for allow in (True, False):
        with pytest.raises(MaterialResolutionError, match="not found"):
            resolve_materials(["missing.pdf"], data, allow_library=allow)
        with pytest.raises(MaterialResolutionError, match="not found"):
            resolve_materials(["allowed.pdf", "missing.pdf"], data, allow_library=allow)
    assert resolve_materials(None, data) == []
    assert resolve_materials([], data) == []
    assert resolve_materials(None, data, allow_library=True) == [root / "allowed.pdf"]


def test_same_basename_is_rejected_but_explicit_path_disambiguates(tmp_path):
    data, root = library(tmp_path)
    other = root / "other"
    other.mkdir()
    (other / "allowed.pdf").write_bytes(b"%PDF-other")
    # Bare basenames must consider ALL matches, even if one is at the root.
    with pytest.raises(MaterialResolutionError, match="ambiguous"):
        resolve_materials(["allowed.pdf"], data)
    assert resolve_materials([str(other / "allowed.pdf")], data) == [other / "allowed.pdf"]


def test_relative_paths_preserve_scope_and_reject_traversal(tmp_path):
    data, root = library(tmp_path)
    assert resolve_materials(["papers/allowed.pdf"], data) == [root / "allowed.pdf"]
    assert resolve_materials(["data/papers/allowed.pdf"], data) == [root / "allowed.pdf"]
    assert resolve_materials(str(root), data) == [root / "allowed.pdf"]
    with pytest.raises(MaterialResolutionError, match="parent traversal"):
        resolve_materials(["../secret.pdf"], data)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-")
    with pytest.raises(MaterialResolutionError, match="outside allowed"):
        resolve_materials([str(outside)], data)


def test_whole_library_does_not_scan_unrelated_data_when_library_exists(tmp_path, monkeypatch):
    data, root = library(tmp_path)
    mount = tmp_path / "mount"
    (mount / "papers").mkdir(parents=True)
    (mount / "papers" / "second.pdf").write_bytes(b"%PDF-")
    (mount / "unrelated.pdf").write_bytes(b"%PDF-")
    monkeypatch.setenv("DATA_PATH", str(mount))
    resolved = resolve_materials([], data, allow_library=True)
    assert {p.name for p in resolved} == {"allowed.pdf", "second.pdf"}
