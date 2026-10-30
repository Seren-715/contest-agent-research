from __future__ import annotations

from core.paperlib import probe_pdf


def test_probe_pdf_detects_signature(tmp_path) -> None:
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.4\nnot a real body")
    result = probe_pdf(path)
    assert result["exists"] is True
    assert result["looks_like_pdf"] is True
    assert result["size_bytes"] > 0


def test_probe_pdf_rejects_fake_extension(tmp_path) -> None:
    path = tmp_path / "paper.pdf"
    path.write_text("plain text", encoding="utf-8")
    result = probe_pdf(path)
    assert result["looks_like_pdf"] is False


def test_probe_pdf_missing_file(tmp_path) -> None:
    result = probe_pdf(tmp_path / "missing.pdf")
    assert result["exists"] is False
    assert result["error"] == "file not found"
