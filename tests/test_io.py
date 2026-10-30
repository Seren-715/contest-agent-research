from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.io import OutputError, check_result, report_path, write_report, write_result


def test_write_result_and_readback(settings) -> None:
    path = write_result(settings, "A-01", {"case_id": "A-01", "answer": "x", "clarifying_question": "y"})
    assert path.exists()
    assert check_result(path, "A-01")["answer"] == "x"


def test_case_id_mismatch_is_detected(settings) -> None:
    path = write_result(settings, "A-01", {"case_id": "A-01", "answer": "x", "clarifying_question": "y"})
    with pytest.raises(OutputError):
        check_result(path, "A-02")


def test_report_path_and_write(settings) -> None:
    markdown = "x" * 900 + "\n## 参考文献\n[1] p.pdf\n"
    relative = write_report(settings, "Survey", "C-01", markdown)
    assert relative == "data/expert/C-01-综述-test-team.md"
    assert (settings.workspace_root / relative).read_text(encoding="utf-8") == markdown
    assert report_path(settings, "Idea", "E-01").name == "E-01-灵感Idea报告-test-team.md"
