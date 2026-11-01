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
from core.budget import Deadline
from core.io import write_report
from core.execution import check_active
from core.paperlib import MaterialRecord, evidence_excerpt, index_materials, terms
from core.schemas import report_limit_errors
from handlers.base import HandlerError, strip_fences
from handlers.evidence import AuditReport, audit_report, ensure_coverage

REFERENCE_MARKER = "## 参考文献"

# These labels select source passages; they do not assert extracted facts or
# treat keyword matches as entailment. Missing labels stay explicitly unknown.
SURVEY_FACETS = (
    ("研究问题", r"problem|task|aim|question|challenge|研究问题|任务|目标"),
    ("方法", r"method|propos|framework|algorithm|architecture|implementation|approach|方法|提出|算法|架构"),
    ("数据与条件", r"dataset|benchmark|corpus|sample|participant|数据|样本|条件"),
    ("评价指标", r"metric|accuracy|recall|precision|latency|throughput|指标|准确率|吞吐|时延"),
    ("结果与比较", r"result|finding|conclusion|outperform|improv|baseline|percent|结果|提升|比较|基线|%"),
    ("局限与边界", r"limitation|caveat|weakness|future work|threat|constraint|局限|限制|未来工作|边界"),
)


def _compact_matrix(records: list[MaterialRecord], query: str, max_chars: int) -> str:
    """Keep one bounded source window per paper when the facet layout is too large."""
    headers = [f"[{index}] 完整文件名：{record.filename}\n" for index, record in enumerate(records, 1)]
    room = max_chars - sum(map(len, headers)) - 2 * len(headers)
    # The matrix limit is a soft layout budget. Do not trade away every
    # source quote merely to fit headers in an unrealistically small budget.
    per_paper = max(120, room // max(1, len(records)))
    blocks: list[str] = []
    for header, record in zip(headers, records):
        excerpt = evidence_excerpt(record, query=query, max_chars=per_paper)
        blocks.append(header + excerpt)
    result = "\n\n".join(blocks)
    # Source identities are not silently discarded to satisfy a cosmetic
    # layout budget. Real model context limits still belong to the caller.
    logging.getLogger(__name__).warning("survey_matrix_compacted papers=%d chars=%d budget=%d", len(records), len(result), max_chars)
    return result


def survey_evidence_matrix(
    records: list[MaterialRecord], *, query: str = "", max_total_chars: int = 36000,
) -> str:
    """Build a fair, bounded per-paper matrix of verbatim candidate passages.

    Offsets refer to the original pypdf page text, not flattened prompt text.
    There is deliberately no model-generated fact or confidence in this layer.
    """
    if not records:
        return ""
    headers = [f"[{i}] 完整文件名：{r.filename}\n内容哈希：{r.sha256}\n"
               for i, r in enumerate(records, 1)]
    available = max_total_chars - sum(len(header) for header in headers) - 2 * len(records)
    per_paper = min(6000, available // len(records))
    if per_paper < 600:
        return _compact_matrix(records, query, max_total_chars)
    query_terms = set(terms(query))
    blocks: list[str] = []
    for header, record in zip(headers, records):
        check_active()
        passages = list(getattr(record, "passages", ()))
        candidates = [(p.page, p.start, p.end, p.text) for p in passages if p.text.strip()]
        if not candidates and record.text.strip():
            # Compatibility for in-memory records: never invent a page locator.
            candidates = [(None, 0, len(record.text), record.text)]
        selected: dict[tuple[int, int], list[str]] = {}
        missing: list[str] = []
        for label, pattern in SURVEY_FACETS:
            ranked = []
            for position, (_, _, _, text) in enumerate(candidates):
                matches = list(re.finditer(pattern, text, re.IGNORECASE))
                if not matches:
                    continue
                overlap = len(query_terms & set(terms(text)))
                heading = bool(re.search(r"(?:^|\n)\s*(?:\d+[.\s]*)?(?:" + pattern + r")", text, re.IGNORECASE))
                numeric = bool(re.search(r"\d+(?:\.\d+)?\s*(?:%|percent|ms|seconds|accuracy)", text, re.IGNORECASE))
                score = min(overlap, 4) + 4 * heading + 2 * numeric + min(len(matches), 3)
                ranked.append((score, position, matches[0].start()))
            if ranked:
                used_locations: list[tuple[object, int]] = []
                for _, position, match_start in sorted(ranked, key=lambda item: (-item[0], item[1])):
                    page, start, _, _ = candidates[position]
                    # Overlapping chunks from one page cannot occupy both
                    # evidence slots while another page supplies a result.
                    if any(page == seen_page and abs(start - seen_start) < 800
                           for seen_page, seen_start in used_locations):
                        continue
                    selected.setdefault((position, match_start), []).append(label)
                    used_locations.append((page, start))
                    if len(used_locations) == 2:
                        break
            else:
                missing.append(label)
        # Preserve some source context even when all facet expressions miss.
        if candidates and not selected:
            position = max(range(len(candidates)),
                           key=lambda i: len(query_terms & set(terms(candidates[i][3]))))
            selected[(position, 0)] = ["任务相关上下文"]
        notes = "未定位维度：" + "、".join(missing) + "；未定位不等于论文未报告。\n" if missing else ""
        warnings = getattr(record, "warnings", ())
        if warnings:
            notes += "解析限制：" + "；".join(warnings)[:400] + "\n"
        # Reserve notes first, then divide the remainder between facets. Long
        # abstracts cannot consume the budget belonging to result/limit pages.
        window_limit = max(1, (per_paper - len(notes)) // 180)
        if len(selected) > window_limit:
            selected = dict(sorted(selected.items(), key=lambda item: -len(item[1]))[:window_limit])
            notes += "预算限制：只保留部分候选窗口。\n"
        quote_budget = max(0, per_paper - len(notes) - 100 * len(selected))
        each = min(1200, quote_budget // max(1, len(selected)))
        if selected and each < 80:
            return _compact_matrix(records, query, max_total_chars)
        lines: list[str] = []
        for (position, match_start), labels in selected.items():
            page, start, _, raw = candidates[position]
            quote_start = max(0, min(match_start - each // 3, len(raw) - each))
            quote = raw[quote_start:quote_start + each]
            source_start = start + quote_start
            locator = f"PDF页{page}，原文字符[{source_start}:{source_start + len(quote)}]" if page is not None else "页码未定位"
            lines.append(f"候选维度={'/'.join(labels)}；{locator}\n原文：{quote}")
        block = header + notes + "\n".join(lines)
        if not candidates:
            block += "无法提取正文；不得推断该论文的具体方法、数值或结论。"
        blocks.append(block)
    result = "\n\n".join(blocks)
    if len(result) > max_total_chars:
        return _compact_matrix(records, query, max_total_chars)
    return result


_ABSTRACT_HEAD = re.compile(r"(?im)^\s*(?:abstract|摘\s*要)\b[\s.:：—-]*")
_CONTRIBUTION = re.compile(
    r"(?i)\b(?:(?:following|our|main|key|primary)\s+(?:main\s+|key\s+)?contributions|"
    r"contributions\s+(?:are|of this (?:paper|work))|we make the following|this paper makes|"
    r"to summarize,|in summary,)")
_CONCLUSION_HEAD = re.compile(
    r"(?im)^\s*(?:\d+(?:\.\d+)*\.?\s*|[IVX]+\.\s*)?(?:conclusions?|concluding remarks|"
    r"(?:discussion|summary) and conclusions?|conclusions? and future work|结论|总结)\s*$")
_DISCUSSION_HEAD = re.compile(r"(?im)^\s*(?:\d+(?:\.\d+)*\.?\s*|[IVX]+\.\s*)?(?:discussion|summary|讨论)\s*$")
_BACK_MATTER = re.compile(r"(?im)^\s*(?:references|bibliography|acknowledge?ments?\.?|参考文献|致谢)\b")


def _page_texts(record: MaterialRecord) -> list[tuple[int, str]]:
    """Rebuild each indexed page's text from its overlapping passages."""
    pages: dict[int, str] = {}
    for span in getattr(record, "passages", ()):
        previous = pages.get(span.page, "")
        pages[span.page] = previous + span.text[max(0, len(previous) - span.start):]
    if not pages and record.text.strip():
        return [(0, record.text)]
    return sorted(pages.items())


def _window(pages: list[tuple[int, str]], index: int, start: int, size: int) -> tuple[tuple[int, ...], str]:
    """Quote from one page, continued onto the next page only if it is the
    adjacent PDF page (an unindexed page in between would hide a gap). The
    continuation is marked inline and every page is returned for the label."""
    page, text = pages[index]
    quote = " ".join(text[start:start + size].split())
    used = (page,)
    # A continuation shorter than a sentence fragment only adds noise.
    if size - len(quote) >= 80 and index + 1 < len(pages) and page and pages[index + 1][0] == page + 1:
        raw = pages[index + 1][1]
        back = _BACK_MATTER.search(raw)  # never continue into references
        following = " ".join(raw[:back.start() if back else len(raw)].split())[:max(0, size - len(quote))]
        if following:
            quote += f" [PDF页{page + 1}] " + following
            used = (page, page + 1)
    return used, quote


def core_passages(record: MaterialRecord, *, abstract_chars: int = 1500, contribution_chars: int = 900,
                  conclusion_chars: int = 1200) -> list[tuple[str, tuple[int, ...], str]]:
    """Verbatim abstract, contribution statement and conclusion of one paper.

    Headline claims and numbers usually sit there. Query ranking with a
    Chinese task over English papers does not guarantee these spans reach the
    prompt, so they are located by section wording instead. Returns
    (label, pages, quote); a part that cannot be located is left out.
    """
    pages = _page_texts(record)
    found: list[tuple[str, tuple[int, ...], str]] = []
    for index, (page, text) in enumerate(pages[:3]):
        match = _ABSTRACT_HEAD.search(text) or re.search(r"(?i)\babstract\b[\s.:：—-]*", text)
        if match:
            used, quote = _window(pages, index, match.end(), abstract_chars)
            # The window is whitespace-joined: match the heading inline.
            intro = re.search(r"(?i)\b(?:1|I)\.?\s+introduction\b", quote)
            if intro and intro.start() > 20:
                quote = quote[:intro.start()].strip()
                if f"[PDF页{used[-1]}]" not in quote:
                    used = used[:1]
            found.append(("摘要", used, quote))
            break
    else:
        if pages:
            used, quote = _window(pages, 0, 0, abstract_chars)
            found.append(("首页", used, quote))
    for index, (page, text) in enumerate(pages[:6]):
        match = _CONTRIBUTION.search(text)
        if match:
            used, quote = _window(pages, index, match.start(), contribution_chars)
            found.append(("贡献陈述", used, quote))
            break
    for heading in (_CONCLUSION_HEAD, _DISCUSSION_HEAD):
        located = False
        for index in range(len(pages) - 1, len(pages) // 3 - 1, -1):
            _, text = pages[index]
            matches = list(heading.finditer(text))
            if matches:
                start = matches[-1].start()
                back = _BACK_MATTER.search(text, matches[-1].end())
                size = back.start() - start if back is not None and back.start() - start < conclusion_chars \
                    else conclusion_chars
                used, quote = _window(pages, index, start, size)
                found.append(("结论", used, quote))
                located = True
                break
        if located:
            break
    return [(label, used, quote) for label, used, quote in found if quote.strip()]


def core_evidence(records: list[MaterialRecord], **limits: int) -> str:
    """Per-paper block of core_passages(), numbered like the other evidence."""
    blocks: list[str] = []
    for index, record in enumerate(records, start=1):
        check_active()
        parts = core_passages(record, **limits)
        lines = [f"（{label}，" + ("PDF页" + "-".join(map(str, used)) if used and used[0] else "页码未定位")
                 + f"）{quote}" for label, used, quote in parts]
        located = {label for label, _, _ in parts}
        missing = [label for label in ("摘要", "贡献陈述", "结论") if label not in located and not
                   (label == "摘要" and "首页" in located)]
        note = ("未定位：" + "、".join(missing) + "（不等于论文没有）\n") if missing else ""
        blocks.append(f"[{index}] 完整文件名：{record.filename}\n标题：{record.title[:200]}\n" + note + "\n".join(lines))
    return "\n\n".join(blocks)


def survey_materials_bundle(
    paths: list[Path], *, query: str = "", deadline: Deadline | None = None,
) -> tuple[str, list[MaterialRecord]]:
    records = index_materials(paths, deadline=deadline)
    return survey_evidence_matrix(records, query=query), records


def materials_bundle(
    paths: list[Path],
    *,
    query: str = "",
    max_chars: int = 1500,
    deadline: Deadline | None = None,
) -> tuple[str, list[MaterialRecord]]:
    """Return the prompt block plus indexed records for the evidence gate."""
    records = index_materials(paths, deadline=deadline)
    blocks: list[str] = []
    for index, record in enumerate(records, start=1):
        excerpt = evidence_excerpt(record, query=query, max_chars=max_chars)
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
        records = index_materials(paths)
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
