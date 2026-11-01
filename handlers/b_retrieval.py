"""B / Literature_Retrieval: find local PDFs, return exact filenames and reasons.

The retrieval core follows the team version that scored B 9.75/15 on the
platform (eval 2026-10-26, B-01/B-03 14.25 each): the first pages of every
allowed PDF are scanned, a lexical shortlist (global BM25 plus the best papers
for each query facet) is read more deeply, and ONE model call judges the whole
shortlist together, so relevance is calibrated across all candidates. Material
resolution, cross-team guards and failure timing codes are this repository's.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from core.budget import Deadline
from core.config import Settings, case_budget_seconds
from core.diagnostics import record_stage
from core.execution import check_active
from core.llm import ModelClient, output_budget
from core.paperlib import (
    MaterialRecord,
    Paper,
    bm25_rank,
    index_materials,
    resolve_materials,
    sentence_split,
    terms,
)
from handlers.base import HandlerError, constraints_text, extract_json_object, question_text

SYSTEM = (
    "你是文献检索助手。只能使用候选论文列表中的完整文件名，不能编造文件名或论文。"
    "只输出一个 JSON 对象，不要加代码围栏。"
)
LOGGER = logging.getLogger(__name__)

# Seconds kept for the single selection call while scanning and expanding.
SELECTION_RESERVE = 300.0

STOP_TERMS = {
    "paper", "papers", "study", "studies", "research", "method", "methods",
    "approach", "approaches", "data", "dataset", "datasets", "result", "results",
    "using", "based", "model", "models", "analysis", "review", "propose",
    "proposed", "work", "works", "article", "articles",
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "with", "by", "from",
}


def _positive_env(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(name + " must be positive")
    return value


def _has_cjk(text: str) -> bool:
    return any("一" <= char <= "鿿" for char in text)


def _expand_query(client: ModelClient, query: str, deadline: Deadline, timeout: float,
                  attempts: int = 2, reserve: float = 0.0) -> list[str]:
    """Translate a Chinese search request into English keywords.

    The local paper library is often English while the contest query is Chinese.
    One retry is allowed because a failed expansion weakens the lexical
    shortlist; each failure is logged. The reserve keeps time for selection.
    """
    for attempt in range(1, attempts + 1):
        if attempt > 1 and deadline.remaining() - reserve < 30:
            break
        values = _expand_query_once(client, query, deadline, timeout, reserve)
        if values:
            return values
        LOGGER.warning("B query expansion produced no keywords (attempt %d/%d)", attempt, attempts)
    return []


def _expand_query_once(client: ModelClient, query: str, deadline: Deadline, timeout: float,
                       reserve: float = 0.0) -> list[str]:
    try:
        text = client.complete(
            [
                {
                    "role": "system",
                    "content": "只输出 JSON。把检索需求中的正向主题、方法、数据、任务转成英文关键词和同义词；"
                               "不要扩展被排除的主题，不要添加需求中没有的条件。",
                },
                {
                    "role": "user",
                    "content": (
                        "检索需求：" + query
                        + "\n只输出 {\"keywords\":[\"keyword1\",\"keyword2\"]}，8-15 个英文关键词或同义词。"
                    ),
                },
            ],
            temperature=0.1,
            max_tokens=output_budget(),
            timeout=deadline.timeout(timeout, reserve=reserve, label="query expansion"),
        )
        data = extract_json_object(text)
        values = data.get("keywords")
        if isinstance(values, list):
            return list(dict.fromkeys(item.strip() for item in values
                                      if isinstance(item, str) and 0 < len(item.strip()) <= 80))[:15]
    except Exception:
        check_active()
        return []
    return []


def _looks_like_pdf(path: Path) -> bool:
    check_active()
    try:
        with path.open("rb") as handle:
            # PDF readers can handle a header preceded by leading bytes; a
            # strict byte-zero check silently discards such readable papers.
            return b"%PDF-" in handle.read(1024)
    except OSError:
        return False


def _candidate_papers(search_text: str, facets: list[str], papers: list[Paper], size: int) -> list[Paper]:
    """Global BM25 ranking plus the strongest papers for each query facet.

    A facet (one translated keyword) can name a sub-topic the global ranking
    under-weights; its best papers join the shortlist before the remaining
    global ranks fill it. Order: global top two thirds, facet picks, the rest.
    """
    ranked = bm25_rank(search_text, papers, top_k=len(papers))
    chosen = {paper.filename: paper for paper, _ in ranked[:max(1, size * 2 // 3)]}
    facet_rankings = [bm25_rank(facet, papers, top_k=3) for facet in facets[:15]]
    for position in range(3):
        for ranking in facet_rankings:
            if position < len(ranking):
                paper, value = ranking[position]
                if value > 0 and len(chosen) < size:
                    chosen.setdefault(paper.filename, paper)
    for paper, _ in ranked:
        if len(chosen) >= size:
            break
        chosen.setdefault(paper.filename, paper)
    return list(chosen.values())


def _retrieval_excerpt(record: MaterialRecord, query: str) -> str:
    """Abstract plus matching body sentences, with room reserved for both."""
    query_terms = set(terms(query)) - STOP_TERMS
    sentences = list(dict.fromkeys(" ".join(sentence.split()) for sentence in sentence_split(record.text)))
    ranked = sorted(enumerate(sentences), key=lambda item: (-len(query_terms & set(terms(item[1]))), item[0]))
    selected: list[str] = []
    available = 850
    for _, sentence in ranked:
        if not (query_terms & set(terms(sentence))):
            continue
        piece = sentence if len(sentence) <= available else sentence[:available - 1] + "…"
        selected.append(piece)
        available -= len(piece) + 1
        if available < 60 or len(selected) >= 6:
            break
    abstract = " ".join(record.abstract.split())[:500]
    return (f"摘要：{abstract or '（无可提取摘要）'}\n   正文相关句："
            f"{' '.join(selected) or '（未发现字面匹配句，按摘要判断语义）'}")


def _bounded(deadline: Deadline, reserve: float, cap: float, floor: float) -> Deadline:
    """Sub-budget for PDF work: up to ``cap`` seconds, leaving ``reserve`` of
    the case, but never under ``floor`` (no evidence means no answer at all).
    index_materials() keeps 30 s of the deadline it gets, hence the + 30."""
    return Deadline(max(floor, min(cap, deadline.remaining() - reserve)) + 30.0)


class RetrievalHandler:
    def handle(self, question: dict[str, Any], settings: Settings, client: ModelClient) -> dict[str, Any]:
        case_id = question["case_id"]
        deadline = Deadline(case_budget_seconds("Literature_Retrieval", settings))
        shortlist_size = _positive_env("CONTEST_B_SHORTLIST", 60)
        query = question_text(question)
        record_stage(settings, case_id, "material_resolution")
        paths = resolve_materials(question.get("materials"), settings.data_dir,
                                  allow_library=True, allow_descriptions=True, lenient=True,
                                  prose_scope=True)
        record_stage(settings, case_id, "materials_resolved", resolved_count=len(paths))
        if not paths:
            if question.get("materials"):
                # A declared scope that holds no paper: a timing code, not a
                # silent empty answer.
                raise HandlerError("retrieval scope contains no papers")
            return {"case_id": case_id, "papers": []}
        # Do not trust extensions: keep only files that actually look like PDFs.
        resolved_count = len(paths)
        paths = [path for path in paths if _looks_like_pdf(path)]
        record_stage(settings, case_id, "pdf_headers_checked", resolved_count=resolved_count,
                     accepted_count=len(paths))
        if not paths:
            raise HandlerError("retrieval materials contain no recognizable PDFs")

        # Scan the first pages of EVERY allowed PDF before choosing what to read
        # deeply: alphabetical position must not decide what can be retrieved.
        scanned = [record for record in index_materials(
            paths, max_pages=2, page_selection="head_tail",
            deadline=_bounded(deadline, SELECTION_RESERVE + 60, 150.0, 30.0)) if record.text.strip()]
        if not scanned:
            raise HandlerError("retrieval materials have no extractable evidence")

        search_text = query
        keywords: list[str] = []
        if _has_cjk(query):
            keywords = _expand_query(client, query, deadline, settings.request_timeout,
                                     reserve=SELECTION_RESERVE)
            if keywords:
                search_text = query + " " + " ".join(keywords)
        record_stage(settings, case_id, "query_expanded", cjk=_has_cjk(query), keyword_count=len(keywords))
        facets = keywords or list(dict.fromkeys(term for term in terms(query) if term not in STOP_TERMS))[:15]
        first_pages = [Paper(path=record.path, filename=record.filename, text=record.text) for record in scanned]
        shortlist = _candidate_papers(search_text, facets, first_pages, shortlist_size)
        if len(scanned) > len(shortlist):
            LOGGER.warning("B shortlist: deep-reading %d of %d scanned papers", len(shortlist), len(scanned))

        # Whole papers for the shortlist only (fair time share per file), so
        # evidence on a middle page can still reach the selection prompt.
        deep = index_materials([paper.path for paper in shortlist],
                               deadline=_bounded(deadline, SELECTION_RESERVE, 120.0, 20.0))
        first_by_path = {record.path: record for record in scanned}
        # A paper whose deep read failed keeps its first-page evidence.
        records = [record if record.text.strip() else first_by_path[record.path] for record in deep]
        record_stage(settings, case_id, "evidence_ready", resolved_count=len(paths), readable_count=len(scanned),
                     shortlist_count=len(records))

        papers = [Paper(path=record.path, filename=record.filename, text=record.text) for record in records]
        ranked = bm25_rank(search_text, papers, top_k=len(papers))
        by_name = {record.filename: record for record in records}
        names = {paper.filename.casefold(): paper.filename for paper, _ in ranked}
        listing = []
        for index, (paper, value) in enumerate(ranked, start=1):
            record = by_name[paper.filename]
            listing.append(
                f"{index}. {paper.filename}（BM25={value:.2f}）\n"
                f"   标题：{record.title}\n"
                f"   证据摘录：{_retrieval_excerpt(record, search_text)}"
            )
        extra = constraints_text(question)
        prompt = (
            f"检索需求：{query}\n\n"
            f"额外约束：\n{extra or '（无）'}\n\n"
            "候选论文（只能从这里选，filename 必须逐字一致）：\n"
            + "\n".join(listing)
            + "\n\n请按相关性从高到低排序，返回全部有证据的相关候选；题目指定数量时遵守该数量。判定规则：\n"
            "1. 每篇必须有材料证据支持核心主题，并满足题目明确的必需条件与排除条件；"
            "重点关注项用于排序，不能擅自增加为每篇都必须满足的条件；\n"
            "2. 只共享 paper/study/method 这类泛词、没有实质主题重合的论文不要返回；\n"
            "3. 同义词和跨语言语义匹配有效，但仅在背景或参考文献中提及主题不等于研究该主题；误报和漏报都影响得分；\n"
            "4. reason 用材料中实际研究的对象、方法或结论说明匹配理由，不能只写“相关”，不能编造材料未提供的信息。\n"
            "只输出 JSON：{\"papers\":[{\"filename\":\"...\",\"reason\":\"...\"}]}。"
        )

        raw_hits = None
        for attempt in (1, 2):
            check_active()
            if attempt > 1 and deadline.remaining() < 120:
                break
            try:
                record_stage(settings, case_id, "model_rerank", candidate_count=len(ranked), attempt=attempt)
                text = client.complete(
                    [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=output_budget(),
                    timeout=deadline.timeout(settings.request_timeout, label="retrieval selection"),
                )
                value = extract_json_object(text).get("papers")
                if not isinstance(value, list):
                    raise HandlerError("papers is not a list")
                raw_hits = value
                break
            except Exception as exc:
                check_active()
                record_stage(settings, case_id, "model_rerank_failed", error_type=type(exc).__name__)
                LOGGER.warning("B selection attempt %d failed (%s)", attempt, type(exc).__name__)
        if raw_hits is None:
            # Lexical overlap cannot establish relevance or verify arbitrary
            # conditions, so there is no keyword fallback.
            raise HandlerError("retrieval failed before any valid rerank response")

        # A successful empty list is a genuine rejection; keep it empty.
        hits: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw_hits:
            if not isinstance(item, dict):
                continue
            reason = str(item.get("reason", "")).strip()
            key = str(item.get("filename", "")).strip().casefold()
            # The platform matches file names case-insensitively; always emit
            # the real file name, never the model's spelling of it.
            filename = names.get(key, "")
            if not filename or key in seen or not reason:
                if key and key not in names:
                    LOGGER.warning("B dropped a returned name that is not a candidate")
                continue
            seen.add(key)
            hits.append({"filename": filename, "reason": reason})
        return {"case_id": case_id, "papers": hits}
