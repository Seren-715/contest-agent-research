"""B failures are encoded as 20 * phase + error seconds of processing time."""
from __future__ import annotations

import pytest

import core.solve as solve
from core.failure_code import failure_delay
from tests.conftest import FakeClient


@pytest.fixture(autouse=True)
def _timing_on(monkeypatch):
    monkeypatch.setenv("CONTEST_FAILURE_TIMING", "1")


def _fail(settings, monkeypatch, materials):
    monkeypatch.setattr(solve, "model_client", lambda s: FakeClient([]))
    with pytest.raises(Exception) as caught:
        solve.solve_task({"case_id": "B-01", "function": "Literature_Retrieval",
                          "input": "adaptive decoding", "materials": materials}, settings)
    return caught.value


@pytest.mark.parametrize("materials,expected", [
    (["missing.pdf"], 180 + 10 * 3 + 0),            # bare PDF names, not found
    (["data/论文库/x.pdf", "data/论文库/y.pdf"], 180 + 10 * 1 + 0),  # data-relative paths
    ("ABSOLUTE", 180 + 10 * 0 + 0),                 # absolute paths (platform-native form)
])
def test_unresolvable_b_materials_encode_entry_shape_and_reason(settings, monkeypatch, tmp_path, materials, expected):
    if materials == "ABSOLUTE":
        # "/elsewhere/x.pdf" is not absolute on Windows (no drive); build a native one.
        materials = [str(tmp_path / "elsewhere" / "x.pdf")]
    error = _fail(settings, monkeypatch, materials)
    assert failure_delay(settings, "B-01", error) == expected


def test_cjk_scope_without_any_library_decodes_as_empty_scope(settings, monkeypatch):
    # B reads unmatched Chinese prose as the library scope; with no library
    # on disk that is phase 3 (materials resolved) + error 19, not 230.
    error = _fail(settings, monkeypatch, ["论文合集"])
    assert failure_delay(settings, "B-01", error) == 20 * 3 + 19


def test_non_pdf_material_decodes_as_header_check(settings, monkeypatch):
    note = settings.data_dir / "论文库" / "fake.pdf"
    note.parent.mkdir(parents=True)
    note.write_text("not a pdf", encoding="utf-8")
    error = _fail(settings, monkeypatch, ["fake.pdf"])
    assert failure_delay(settings, "B-01", error) == 20 * 4 + 9  # headers checked, then no recognizable PDFs


def test_unexpected_os_error_and_missing_diagnostics(settings):
    assert failure_delay(settings, "B-02", PermissionError("denied")) == 15  # no diagnostics yet: phase 0


def test_timing_code_can_be_disabled(settings, monkeypatch):
    monkeypatch.setenv("CONTEST_FAILURE_TIMING", "0")
    assert failure_delay(settings, "B-01", ValueError("x")) == 0


@pytest.mark.parametrize("message,code", [
    ("report body has 5200 characters; Idea maximum is 4500", 1),
    ("report body has 300 characters; at least 800 required excluding references/headings", 2),
    ("事实性语句没有可追溯引用：……", 6),
    ("引用编号超出指定材料范围：7", 7),
    ("正文没有任何 [编号] 引用。", 5),
])
def test_idea_failure_codes(message, code):
    from core.failure_code import idea_failure_delay
    from handlers.base import HandlerError
    assert idea_failure_delay(HandlerError(message)) == 20 * code


def test_delay_is_skipped_when_it_would_not_fit_the_case_time_limit():
    from core.failure_code import fit_delay
    assert fit_delay(250, 540, 1.0) == 250
    assert fit_delay(220, 900, 700.0) == 0      # 700 + 220 + 15 > 900
    assert fit_delay(0, 540, 1.0) == 0


def test_code_ranges_do_not_overlap():
    from core.failure_code import REJECTED_QUESTION_DELAY
    phase_codes = {20 * phase + error for phase in range(9) for error in range(1, 20)}
    detail_codes = {180 + 10 * shape + reason for shape in range(8) for reason in range(4)}
    assert max(phase_codes) < min(detail_codes)
    assert REJECTED_QUESTION_DELAY not in phase_codes | detail_codes
    assert REJECTED_QUESTION_DELAY > max(detail_codes)
