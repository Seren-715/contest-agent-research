"""Platform layouts that made B fail before any model call (eval-20261101: 0 calls, ~0.2 s)."""
from __future__ import annotations

import json
import unicodedata

import pytest

import core.solve as solve
from core.paperlib import MaterialResolutionError, resolve_materials
from tests.conftest import FakeClient


def _pdf(path, body=b"%PDF-1.4 test"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


@pytest.fixture
def mount(tmp_path, monkeypatch):
    data = tmp_path / "output" / "data"
    mount = tmp_path / "readonly"
    monkeypatch.setenv("DATA_PATH", str(mount))
    return data, mount


def test_filename_differing_only_in_case_resolves_to_the_actual_file(mount):
    data, root = mount
    actual = _pdf(root / "论文库" / "Adaptive_Decoding.pdf")
    resolved = resolve_materials(["adaptive_decoding.PDF"], data)
    assert [path.name for path in resolved] == ["Adaptive_Decoding.pdf"]
    assert resolved[0].samefile(actual)


def test_filename_in_another_unicode_normal_form_resolves(mount):
    data, root = mount
    actual = _pdf(root / "论文库" / unicodedata.normalize("NFD", "Café.pdf"))
    assert resolve_materials([unicodedata.normalize("NFC", "Café.pdf")], data) == [actual]


def test_prose_scope_without_scope_words_is_a_description_only_when_allowed(mount):
    data, root = mount
    paper = _pdf(root / "论文库" / "a.pdf")
    assert resolve_materials(["论文库中的全部论文"], data, allow_descriptions=True) == [paper]
    with pytest.raises(MaterialResolutionError, match="not found"):
        resolve_materials(["论文库中的全部论文"], data)


def _symlink(link, target):
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:  # e.g. Windows without symlink privilege
        pytest.skip(f"symlinks unavailable: {exc}")


def test_symlink_leaving_the_data_roots_is_rejected_or_skipped(mount, tmp_path):
    data, root = mount
    outside = _pdf(tmp_path / "outside-store" / "secret.pdf")
    inside = _pdf(root / "论文库" / "a.pdf")
    _symlink(root / "论文库" / "escape.pdf", outside)
    with pytest.raises(MaterialResolutionError, match="outside allowed data roots"):
        resolve_materials(["escape.pdf"], data)
    with pytest.raises(MaterialResolutionError, match="outside allowed data roots"):
        resolve_materials(["本地论文文件夹内的 PDF"], data, allow_descriptions=True)
    # Retrieval over the whole library skips the escaping link and keeps the rest.
    assert resolve_materials(["本地论文文件夹内的 PDF"], data, allow_descriptions=True, lenient=True) == [inside]


def test_symlink_within_the_data_roots_is_read_under_its_library_name(mount):
    data, root = mount
    target = _pdf(root / "store" / "blob-123.pdf")
    _symlink(root / "论文库" / "Adaptive_Decoding.pdf", target)
    resolved = resolve_materials(["本地论文文件夹内的 PDF"], data, allow_descriptions=True)
    assert [path.name for path in resolved] == ["Adaptive_Decoding.pdf"]
    assert resolve_materials(["Adaptive_Decoding.pdf"], data)[0].name == "Adaptive_Decoding.pdf"


def test_parent_traversal_is_still_rejected(mount):
    data, root = mount
    _pdf(root / "论文库" / "a.pdf")
    with pytest.raises(MaterialResolutionError, match="parent traversal"):
        resolve_materials(["论文库/../论文库/a.pdf"], data)


@pytest.mark.parametrize("folder", ["t/论文库", "pdfs", "t/Papers_2026"])
def test_description_discovers_a_library_with_another_name_or_under_the_team(mount, folder):
    data, root = mount
    paper = _pdf(root / folder / "a.pdf")
    _pdf(root / "experiments" / "D-01" / "method.pdf")  # task material, not the library
    assert resolve_materials(["本地论文文件夹内的 PDF"], data, allow_descriptions=True) == [paper]


def test_lenient_retrieval_skips_missing_and_duplicate_entries_but_strict_mode_fails(mount):
    data, root = mount
    a = _pdf(root / "论文库" / "a.pdf")
    _pdf(root / "论文库" / "x" / "dup.pdf", b"%PDF-1")
    _pdf(root / "论文库" / "y" / "dup.pdf", b"%PDF-2")
    assert resolve_materials(["a.pdf", "missing.pdf"], data, lenient=True) == [a]
    with pytest.raises(MaterialResolutionError, match="not found"):
        resolve_materials(["a.pdf", "missing.pdf"], data)
    with pytest.raises(MaterialResolutionError, match="could be resolved"):
        resolve_materials(["missing.pdf"], data, lenient=True)
    library = resolve_materials(["本地论文文件夹内的 PDF"], data, allow_descriptions=True, lenient=True)
    assert sorted(path.name for path in library) == ["a.pdf", "dup.pdf"]
    with pytest.raises(MaterialResolutionError, match="ambiguous PDF filename"):
        resolve_materials(["本地论文文件夹内的 PDF"], data, allow_descriptions=True)


def test_b_case_with_a_case_differing_material_reaches_the_model(settings, monkeypatch, tmp_path):
    from fpdf import FPDF
    mount = tmp_path / "readonly"
    monkeypatch.setenv("DATA_PATH", str(mount))
    path = mount / "论文库" / "Adaptive_Decoding.pdf"
    path.parent.mkdir(parents=True)
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)
    pdf.multi_cell(0, 5, "Adaptive decoding for language inference throughput.")
    pdf.output(str(path))
    reply = json.dumps({"papers": [{"filename": "Adaptive_Decoding.pdf", "reason": "原文研究自适应解码与推理吞吐量。"}]},
                       ensure_ascii=False)
    client = FakeClient([reply])
    monkeypatch.setattr(solve, "model_client", lambda settings: client)
    result = solve.solve_task({"case_id": "B-01", "function": "Literature_Retrieval",
                               "input": "adaptive decoding", "materials": ["adaptive_decoding.pdf"]}, settings)
    assert len(client.calls) == 1
    assert [item["filename"] for item in result["papers"]] == ["Adaptive_Decoding.pdf"]


def test_on_disk_case_reports_the_stored_name(tmp_path):
    from core.paperlib import _on_disk_case
    stored = _pdf(tmp_path / "Adaptive_Decoding.pdf")
    assert _on_disk_case(tmp_path / "adaptive_decoding.PDF") == stored
    assert _on_disk_case(tmp_path / "Adaptive_Decoding.pdf") == stored
    assert _on_disk_case(tmp_path / "missing.pdf") == tmp_path / "missing.pdf"


def test_missing_identifier_is_not_reinterpreted_as_a_library_description(mount):
    data, root = mount
    _pdf(root / "论文库" / "a.pdf")
    for kwargs in ({}, {"allow_descriptions": True}):
        with pytest.raises(MaterialResolutionError, match="not found"):
            resolve_materials(["important_folder"], data, **kwargs)
    with pytest.raises(MaterialResolutionError, match="could be resolved"):
        resolve_materials(["important_folder"], data, allow_descriptions=True, lenient=True)


@pytest.mark.parametrize("text,expected", [
    ("论文库中的全部论文", True), ("all local papers", True), ("important_folder", False),
    ("papers_2024", False), ("目录", False),
])
def test_prose_scope_detection(text, expected):
    from core.paperlib import _looks_like_prose_scope
    assert _looks_like_prose_scope(text) is expected


def test_lenient_explicit_materials_skip_an_escaping_link_and_keep_the_rest(mount, tmp_path):
    data, root = mount
    good = _pdf(root / "论文库" / "good.pdf")
    _symlink(root / "论文库" / "escape.pdf", _pdf(tmp_path / "outside-store" / "secret.pdf"))
    assert resolve_materials(["good.pdf", "escape.pdf"], data, lenient=True) == [good]
    assert resolve_materials(["论文库"], data, lenient=True) == [good]  # explicit directory
    with pytest.raises(MaterialResolutionError, match="outside allowed data roots"):
        resolve_materials(["good.pdf", "escape.pdf"], data)
    with pytest.raises(MaterialResolutionError, match="outside allowed data roots"):
        resolve_materials(["论文库"], data)
