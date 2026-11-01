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


def test_missing_declared_material_decodes_as_resolution_not_found(settings, monkeypatch):
    error = _fail(settings, monkeypatch, ["missing.pdf"])
    assert failure_delay(settings, "B-01", error) == 20 * 2 + 7  # lenient B: nothing could be resolved


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
