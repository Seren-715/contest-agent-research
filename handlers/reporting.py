"""Shared helpers for C / Survey and E / Idea reports.

The report pipeline is a Python port of PaperCode's evidence gate:

1. index every specified PDF (hash, pages, title, abstract, query-relevant sentences);
2. let the model write with an explicit claim/citation contract;
3. audit the draft with a ClaimLedger (uncited factual sentences are blockers);
4. repair once from the audit findings;
5. complete missing citation/reference coverage with verbatim excerpts (never invented facts).

A structurally valid but content-free "受限运行" placeholder is not an acceptable
final state: it scores zero and hides the evidence that is actually available.
"""

from __future__ import annotations

import logging

import re
from pathlib import Path
from typing import Any

from core.config import Settings
from core.io import write_report
from core.paperlib import Paper, MaterialRecord, evidence_excerpt, index_materials
from core.schemas import report_limit_errors
from handlers.base import HandlerError, strip_fences
from handlers.evidence import AuditReport, audit_report, ensure_coverage

REFERENCE_MARKER = "## 参考文献"


def materials_bundle(
    paths: list[Path],
    *,
    query: str = "",
    max_chars: int = 1500,
) -> tuple[str, list[MaterialRecord]]:
    """Return the prompt block plus indexed records for the evidence gate."""
    records = index_materials(paths, max_pages=12)
    blocks: list[str] = []
    for index, record in enumerate(records, start=1):
        paper = Paper(path=record.path, filename=record.filename, text=record.text)
        excerpt = evidence_excerpt(paper, query=query, max_chars=max_chars)
        blocks.append(f"[{index}] 完整文件名：{record.filename}\n{excerpt}")
    return "\n\n".join(blocks), records


def materials_block(paths: list[Path], *, max_chars: int = 4500) -> str:
    """Backward-compatible wrapper used by older call sites."""
    block, _ = materials_bundle(paths, max_chars=max_chars)
    return block


def validate_report(
    markdown: str,
    paths: list[Path],
    *,
    min_chars: int = 800,
    require_all: bool = True,
) -> list[str]:
    errors = report_limit_errors(markdown, "Survey" if require_all else "Idea", min_chars)
    marker = REFERENCE_MARKER
    if marker not in markdown:
        return errors + [f"缺少 {marker} 标题"]
    body, _, refs = markdown.partition(marker)
    if require_all:
        for index, path in enumerate(paths, start=1):
            if f"[{index}" not in body:
                errors.append(f"正文没有引用 [{index}]（{path.name}）")
            if path.name not in refs:
                errors.append(f"参考文献没有列出完整文件名：{path.name}")
    else:
        if not re.search(r"\[\d+", body):
            errors.append("正文没有 [编号] 引用")
        if not any(path.name in refs for path in paths):
            errors.append("参考文献没有列出任何指定 PDF 的完整文件名")
    return errors


def audit(markdown: str, records: list[MaterialRecord], function: str) -> AuditReport:
    return audit_report(markdown, records, function)


def finalize(
    settings: Settings,
    function: str,
    case_id: str,
    markdown: str,
    paths: list[Path],
    *,
    require_all: bool = True,
    records: list[MaterialRecord] | None = None,
    query: str = "",
) -> dict[str, Any]:
    markdown = strip_fences(markdown).strip() + "\n"
    if records is None:
        records = index_materials(paths, max_pages=12)
    if function == "Survey":
        markdown, _ = ensure_coverage(markdown, records, function)
    report = audit_report(markdown, records, function)
    if report.blockers:
        logging.getLogger(__name__).warning(
            "report_rejected case_id=%s function=%s categories=%s",
            case_id, function, sorted({item.category for item in report.blockers}),
        )
        raise HandlerError("; ".join(item.message for item in report.blockers[:6]))
    path = write_report(settings, function, case_id, markdown)
    return {"case_id": case_id, "path": path, "markdown": markdown}
