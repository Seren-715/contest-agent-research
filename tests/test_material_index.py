from pathlib import Path

import pytest
from pypdf import PdfReader

from core.execution import ExecutionCancelled, ExecutionControl, execution_context
from core.budget import Deadline
from core.paperlib import (
    MaterialResolutionError, describe_material_scope, evidence_excerpt, index_materials,
    has_explicit_material_scope, is_material_description, resolve_materials, terms,
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


def test_shared_bundle_reads_all_pages_and_keeps_evidence_without_query(tmp_path):
    from handlers.reporting import materials_bundle
    path = tmp_path / "idea.pdf"
    make_pdf(path, [f"Source page {number + 1}." for number in range(24)])
    block, records = materials_bundle([path])
    assert records[0].indexed_pages == tuple(range(1, 25))
    assert "Source page 1" in block
    assert "p.1" in block


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


@pytest.mark.parametrize("budget", [120, 750])
def test_excerpt_window_keeps_late_query_hit_and_exact_locator(budget):
    import re
    from core.paperlib import MaterialRecord, EvidencePassage
    raw = "Generic background prose. " * 40 + "QuarrySpec evaluates ZebraSet accuracy 81.2 percent under condition Z."
    record = MaterialRecord(Path("p.pdf"), "p.pdf", "a" * 64, 100, 1, "Study", "Background", raw,
                            passages=(EvidencePassage(10, 73, 73 + len(raw), raw),))
    excerpt = evidence_excerpt(record, "QuarrySpec ZebraSet accuracy", max_chars=budget)
    assert len(excerpt) <= budget
    assert "QuarrySpec" in excerpt and "81.2" in excerpt
    match = re.search(r"chars (\d+):(\d+)\] (.*)", excerpt)
    assert match
    begin, end = int(match[1]) - 73, int(match[2]) - 73
    assert raw[begin:end] == match[3]


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


@pytest.mark.parametrize("prefix", ["data/论文库", "data/papers"])
def test_package_relative_materials_resolve_separate_readonly_mount(tmp_path, monkeypatch, prefix):
    data = tmp_path / "output" / "data"
    mount = tmp_path / "readonly-data"
    source = mount / prefix.split("/")[1] / "topic" / "source.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF-")
    monkeypatch.setenv("DATA_PATH", str(mount))
    assert resolve_materials([prefix + "/topic/source.pdf"], data) == [source]
    # Full suffix must survive mapping; a basename elsewhere is no substitute.
    with pytest.raises(MaterialResolutionError, match="not found"):
        resolve_materials([prefix + "/wrong/source.pdf"], data)


def test_package_relative_path_still_rejects_two_real_matches(tmp_path, monkeypatch):
    data = tmp_path / "output" / "data"
    mount = tmp_path / "readonly-data"
    for index, base in enumerate([data, mount]):
        source = base / "论文库" / "source.pdf"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"%PDF-" + bytes([index]))
    monkeypatch.setenv("DATA_PATH", str(mount))
    with pytest.raises(MaterialResolutionError, match="ambiguous"):
        resolve_materials(["data/论文库/source.pdf"], data)


@pytest.mark.parametrize("declaration", ["source.pdf", "data/论文库/source.pdf"])
def test_identical_pdf_copies_across_mounts_are_one_material(tmp_path, monkeypatch, declaration):
    data = tmp_path / "output" / "data"
    mount = tmp_path / "readonly-data"
    for base in [data, mount]:
        source = base / "论文库" / "source.pdf"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"%PDF-identical")
    monkeypatch.setenv("DATA_PATH", str(mount))
    assert resolve_materials([declaration], data) == [data / "论文库" / "source.pdf"]
    assert len(resolve_materials([], data, allow_library=True)) == 1


def test_explicit_pdf_and_existing_auxiliary_file_do_not_fail_entire_scope(tmp_path):
    data, root = library(tmp_path)
    note = root / "conditions.txt"
    note.write_text("Experiment conditions", encoding="utf-8")
    assert resolve_materials([str(root / "allowed.pdf"), str(note)], data) == [root / "allowed.pdf"]
    assert resolve_materials([str(note)], data) == []
    with pytest.raises(MaterialResolutionError, match="not found"):
        resolve_materials([str(root / "allowed.pdf"), "missing.txt"], data)
    outside = tmp_path / "outside.txt"
    outside.write_text("not authorized", encoding="utf-8")
    with pytest.raises(MaterialResolutionError, match="outside allowed"):
        resolve_materials([str(outside)], data)


@pytest.mark.parametrize("description", [
    "本地论文文件夹内的 PDF",
    "本地论文文件夹内指定的电池回收 PDF",
    "本题指定的城市气候与步行规划论文 PDF",
    "Local papers provided for this question",
])
def test_official_descriptions_require_opt_in_and_explicit_selection(tmp_path, description, caplog):
    data, root = library(tmp_path)
    assert is_material_description(description)
    assert describe_material_scope([description]) == [description]
    with pytest.raises(MaterialResolutionError, match="requires explicit selection"):
        resolve_materials([description], data)
    assert resolve_materials([description], data, allow_descriptions=True) == [root / "allowed.pdf"]
    assert "requires_selection=true" in caplog.text


@pytest.mark.parametrize("concrete", ["missing.pdf", "本题指定的论文.pdf", "data/papers/missing.pdf", "*.pdf"])
def test_concrete_missing_never_turns_into_a_description(tmp_path, concrete):
    data, _ = library(tmp_path)
    assert not is_material_description(concrete)
    with pytest.raises(MaterialResolutionError, match="not found"):
        resolve_materials([concrete], data, allow_descriptions=True)


def test_description_does_not_expand_explicit_pdf_selection(tmp_path):
    data, root = library(tmp_path)
    (root / "other.pdf").write_bytes(b"%PDF-other")
    description = "本地论文文件夹内的 PDF"
    assert resolve_materials([description, "allowed.pdf"], data, allow_descriptions=True) == [root / "allowed.pdf"]
    with pytest.raises(MaterialResolutionError, match="not found"):
        resolve_materials([description, "missing.pdf"], data, allow_descriptions=True)
    assert resolve_materials([], data, allow_descriptions=True) == []
    empty = root / "empty"
    empty.mkdir()
    assert resolve_materials([description, str(empty)], data, allow_descriptions=True) == []


def test_description_discovery_does_not_scan_the_entire_data_mount(tmp_path, monkeypatch):
    data = tmp_path / "output" / "data"
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "unrelated.pdf").write_bytes(b"%PDF-")
    monkeypatch.setenv("DATA_PATH", str(mount))
    assert resolve_materials(["本题指定的论文 PDF"], data, allow_descriptions=True) == []


def test_material_scope_routing_distinguishes_auxiliary_and_pdf_declarations():
    assert not has_explicit_material_scope(["本题指定的论文 PDF", "conditions.csv"])
    assert has_explicit_material_scope(["本题指定的论文 PDF", "data/papers/allowed.pdf"])
    assert has_explicit_material_scope(["本题指定的论文 PDF", "data/papers"])
    assert not has_explicit_material_scope([])


def test_duplicate_directory_mount_requires_equal_pdf_paths_and_contents(tmp_path, monkeypatch):
    data = tmp_path / "output" / "data"
    mount = tmp_path / "readonly-data"
    for base in [data, mount]:
        folder = base / "papers" / "topic"
        folder.mkdir(parents=True)
        (folder / "source.pdf").write_bytes(b"%PDF-identical")
    monkeypatch.setenv("DATA_PATH", str(mount))
    # Auxiliary differences do not expand PDF scope and are not read here.
    (mount / "papers" / "notes.txt").write_text("not indexed", encoding="utf-8")
    assert resolve_materials(["data/papers"], data) == [data / "papers" / "topic" / "source.pdf"]
    extra = mount / "papers" / "extra.pdf"
    extra.write_bytes(b"%PDF-extra")
    with pytest.raises(MaterialResolutionError, match="ambiguous"):
        resolve_materials(["data/papers"], data)
    extra.unlink()
    (mount / "papers" / "topic" / "source.pdf").write_bytes(b"%PDF-different")
    with pytest.raises(MaterialResolutionError, match="ambiguous"):
        resolve_materials(["data/papers"], data)


def test_directory_mount_with_same_basename_in_different_relative_path_is_ambiguous(tmp_path, monkeypatch):
    data = tmp_path / "output" / "data"
    mount = tmp_path / "readonly-data"
    for base, subdir in [(data, "topic"), (mount, "other")]:
        folder = base / "papers" / subdir
        folder.mkdir(parents=True)
        (folder / "source.pdf").write_bytes(b"%PDF-identical")
    monkeypatch.setenv("DATA_PATH", str(mount))
    with pytest.raises(MaterialResolutionError, match="ambiguous"):
        resolve_materials(["data/papers"], data)


def test_slow_early_file_cannot_starve_later_files_of_index_time(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("core.paperlib.time.monotonic", lambda: clock[0])
    monkeypatch.setenv("CONTEST_INDEX_SECONDS", "6")

    class SlowPage:
        def extract_text(self):
            clock[0] += 1.0  # every page costs one simulated second
            return "Evidence text for this page of the study."

    class SlowReader:
        def __init__(self, *_):
            self.pages = [SlowPage() for _ in range(10)]

    monkeypatch.setattr("core.paperlib.PdfReader", SlowReader)
    paths = []
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        path = tmp_path / name
        path.write_bytes(b"%PDF-")
        paths.append(path)
    records = index_materials(paths)
    # Without a per-file share, a.pdf would consume the whole 6 s budget.
    assert all(record.text.strip() for record in records)
    assert all("index_budget_exhausted" not in record.warnings for record in records)
    assert all("index_time_limit" in record.warnings for record in records)


def test_slow_reader_setup_still_indexes_at_least_one_page(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("core.paperlib.time.monotonic", lambda: clock[0])
    monkeypatch.setenv("CONTEST_INDEX_SECONDS", "9")
    setup_cost = {"a.pdf": 6.0}

    class Page:
        def extract_text(self):
            clock[0] += 1.0
            return "Evidence text for this page of the study."

    class Reader:
        def __init__(self, path):
            clock[0] += setup_cost.get(Path(path).name, 0.0)  # e.g. a slow xref rebuild
            self.pages = [Page() for _ in range(10)]

    monkeypatch.setattr("core.paperlib.PdfReader", Reader)
    paths = []
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        path = tmp_path / name
        path.write_bytes(b"%PDF-")
        paths.append(path)
    records = index_materials(paths)
    # a.pdf's setup alone exceeds its 3 s share; it must still keep page 1.
    assert records[0].indexed_pages == (1,)
    assert all(record.text.strip() for record in records)
    assert clock[0] <= 9.0
