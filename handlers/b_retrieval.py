"""B / Literature_Retrieval: find local PDFs, return exact filenames and reasons.

Retrieval follows PaperCode's material-index + literature-map design: each PDF
is indexed (hash/pages/title/abstract), candidates are ranked with BM25 over the
local corpus, and the model reranks a small, evidence-rich candidate list. False
positives are explicitly discouraged because the contest penalises them.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.budget import Deadline
from core.diagnostics import record_stage
from core.config import Settings, case_budget_seconds
from core.execution import check_active
from core.llm import ModelClient, output_budget
from core.paperlib import (
    MaterialRecord,
    Paper,
    bm25_rank,
    evidence_excerpt,
    index_materials,
    resolve_materials,
)
from handlers.base import HandlerError, constraints_text, extract_json_object, question_text

SYSTEM = (
    "你是文献检索助手。只能使用候选论文列表中的完整文件名，不能编造文件名或论文。"
    "只输出一个 JSON 对象，不要加代码围栏。"
)
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrievalLimits:
    """Operational budgets, not a requirement to return a fixed number of hits."""

    candidates: int = 128
    batch_size: int = 16
    batches: int = 8
    results: int = 80
    excerpt_chars: int = 1400
    # Two-stage retrieval for larger libraries (the contest uses ~100+ PDFs):
    # a lenient short-excerpt screen of every paper, then the detailed rerank.
    screen_threshold: int = 40
    screen_batch: int = 70
    screen_chars: int = 500
    # Seconds the screen must leave for the detailed rerank (at least the
    # last screen call's duration). Without it, a slow gateway let screening
    # use the time and the first rerank timed out: no answer at all.
    rerank_reserve: int = 150

    @classmethod
    def from_env(cls) -> "RetrievalLimits":
        defaults = cls()
        values = {}
        for field in ("candidates", "batch_size", "batches", "results", "excerpt_chars",
                      "screen_threshold", "screen_batch", "screen_chars", "rerank_reserve"):
            value = int(os.environ.get("CONTEST_B_" + field.upper(), getattr(defaults, field)))
            if value <= 0:
                raise ValueError("CONTEST_B_" + field.upper() + " must be positive")
            values[field] = value
        return cls(**values)


def _has_cjk(text: str) -> bool:
    return any("一" <= char <= "鿿" for char in text)


def _expand_query(client: ModelClient, query: str, deadline: Deadline, timeout: float,
                  attempts: int = 2) -> list[str]:
    """Translate a Chinese search request into English keywords.

    The local paper library is often English while the contest query is Chinese.
    Keyword ranking alone would miss everything; this is a cheap bridge before
    the LLM rerank. One retry is allowed because a failed expansion silently
    degrades ranking to filename order; each failure is logged.
    """
    for attempt in range(1, attempts + 1):
        if attempt > 1 and deadline.remaining() < 30:
            break
        values = _expand_query_once(client, query, deadline, timeout)
        if values:
            return values
        LOGGER.warning("B query expansion produced no keywords (attempt %d/%d)", attempt, attempts)
    return []


def _expand_query_once(client: ModelClient, query: str, deadline: Deadline, timeout: float) -> list[str]:
    try:
        text = client.complete(
            [
                {
                    "role": "system",
                    "content": "只输出 JSON。把检索需求转成覆盖主题、方法、数据、任务的英文关键词和同义词。",
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
            timeout=deadline.timeout(timeout, label="query expansion"),
        )
        data = extract_json_object(text)
        values = data.get("keywords")
        if isinstance(values, list):
            return [str(item).strip() for item in values if str(item).strip()]
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


def _rank_records(query: str, records: list[MaterialRecord],
                  keywords: tuple[str, ...] = ()) -> list[tuple[MaterialRecord, float]]:
    """Fuse document and best-passage ranks after deduplicating at paper level.

    When translated keywords are available they form independent rankings, so
    the (usually English) keywords are not diluted by the original CJK query.
    """
    check_active()
    papers = [Paper(path=item.path, filename=item.filename, text=item.text) for item in records]
    passages = []
    for record in records:
        check_active()
        for passage in getattr(record, "passages", ()):
            passages.append(Paper(path=record.path, filename=record.filename, text=passage.text))
    queries = [query]
    if keywords:
        queries.append(" ".join(keywords))
    rankings = []
    for text in queries:
        rankings.append(bm25_rank(text, papers, top_k=len(papers)))
        rankings.append(bm25_rank(text, passages, top_k=len(passages)) if passages else [])
    check_active()
    # One paper contributes once to each ranking; repeated chunks cannot fill
    # the candidate pool. RRF avoids comparing scores across different lengths.
    scores: dict[str, float] = {}
    for ranking in rankings:
        seen = set()
        for paper, value in ranking:
            key = paper.filename.casefold()
            if value <= 0 or key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (60 + len(seen))
    return sorted(
        ((record, scores.get(record.filename.casefold(), 0.0)) for record in records),
        key=lambda item: (-item[1], item[0].filename.casefold(), item[0].filename),
    )


SCREEN_SYSTEM = (
    "你是文献检索初筛助手。只能使用列表中的完整文件名，不能编造。只输出一个 JSON 对象，不要加代码围栏。"
)


def _screen(client: ModelClient, query: str, extra: str, ranked: list[tuple[MaterialRecord, float]],
            search_text: str, limits: RetrievalLimits, deadline: Deadline, settings: Settings,
            case_id: str) -> set[str] | None:
    """Lenient first pass over every paper with short excerpts.

    Sequential detailed batches over 100+ papers do not fit the B time budget,
    so lexical rank would decide which papers the model never sees. Here every
    paper is seen once. Papers of a failed or (for lack of time) unscreened
    batch pass through to the detailed stage. Returns None only if no screen
    batch succeeded.
    """
    kept: set[str] = set()
    succeeded = 0
    per_paper = None  # observed seconds per screened paper
    last = 0.0        # duration of the last screen call
    for offset in range(0, len(ranked), limits.screen_batch):
        check_active()
        batch = ranked[offset:offset + limits.screen_batch]
        names = {record.filename.casefold() for record, _ in batch}
        expected = per_paper * len(batch) if per_paper is not None else 0.0
        if deadline.remaining() < 60 or (
                per_paper is not None
                and deadline.remaining() - expected < max(limits.rerank_reserve, last)):
            # Unscreened papers were never judged: pass them all through, like
            # a failed batch, rather than dropping them. They rank lowest, so
            # the rerank still sees screened survivors first.
            rest = {record.filename.casefold() for record, _ in ranked[offset:]}
            kept |= rest
            LOGGER.warning("B screen stopped to keep rerank time after %d papers; passing %d through", offset, len(rest))
            break
        before = deadline.remaining()
        listing = "\n".join(
            f"{index}. {record.filename}\n" + " ".join(evidence_excerpt(
                record, query=search_text, max_chars=limits.screen_chars).split())
            for index, (record, _) in enumerate(batch, start=1)
        )
        prompt = (
            f"检索需求：{query}\n\n额外约束：\n{extra or '（无）'}\n\n"
            "候选论文（标题与少量原文摘录）：\n" + listing + "\n\n"
            "这是宽松初筛，后面还有带完整证据的复核。凡核心主题、方法、任务或数据可能与检索需求相关的论文都保留；"
            "只排除明显无关的论文，以及摘录已明确显示违反硬约束的论文。拿不准时保留。\n"
            "只输出 JSON：{\"papers\":[{\"filename\":\"...\"}]}，filename 必须逐字一致。"
        )
        try:
            record_stage(settings, case_id, "model_screen", candidate_count=len(batch))
            text = client.complete(
                [{"role": "system", "content": SCREEN_SYSTEM}, {"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=output_budget(),
                timeout=deadline.timeout(settings.request_timeout, label="retrieval screen"),
            )
            raw = extract_json_object(text).get("papers")
            if not isinstance(raw, list):
                raise HandlerError("papers is not a list")
        except Exception as exc:
            check_active()
            record_stage(settings, case_id, "model_screen_failed", error_type=type(exc).__name__)
            LOGGER.warning("B screen batch failed (%s); passing its %d papers through", type(exc).__name__, len(batch))
            kept |= names
            last = max(0.0, before - deadline.remaining())
            per_paper = last / len(batch)
            continue
        last = max(0.0, before - deadline.remaining())
        per_paper = last / len(batch)
        succeeded += 1
        for item in raw:
            key = str(item.get("filename", "")).strip().casefold() if isinstance(item, dict) else str(item).strip().casefold()
            if key in names:
                kept.add(key)
    if not succeeded:
        return None
    record_stage(settings, case_id, "screened", total=len(ranked), kept=len(kept))
    return kept


class RetrievalHandler:
    def handle(self, question: dict[str, Any], settings: Settings, client: ModelClient) -> dict[str, Any]:
        case_id = question["case_id"]
        deadline = Deadline(case_budget_seconds("Literature_Retrieval", settings))
        limits = RetrievalLimits.from_env()
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
        records = index_materials(paths, deadline=deadline)
        warning_counts: dict[str, int] = {}
        for record in records:
            for warning in record.warnings:
                kind = warning.split(":", 1)[0]
                warning_counts[kind] = warning_counts.get(kind, 0) + 1
        indexed_count = len(records)
        records = [record for record in records if record.text.strip()]
        if len(records) < indexed_count:
            LOGGER.warning("B dropped %d of %d PDFs without extractable text: %s",
                           indexed_count - len(records), indexed_count, warning_counts)
        record_stage(settings, case_id, "evidence_ready", resolved_count=len(paths), readable_count=len(records),
                     unreadable_count=indexed_count - len(records), warning_counts=warning_counts)
        if not records:
            raise HandlerError("retrieval materials have no extractable evidence")
        search_text = query
        keywords: list[str] = []
        if _has_cjk(query):
            keywords = _expand_query(client, query, deadline, settings.request_timeout)
            if keywords:
                search_text = query + " " + " ".join(keywords)
        record_stage(settings, case_id, "query_expanded", cjk=_has_cjk(query), keyword_count=len(keywords))
        ranked = _rank_records(query, records, tuple(keywords))
        capacity = min(limits.candidates, limits.batch_size * limits.batches)
        extra = constraints_text(question)
        pool = ranked
        if len(ranked) > limits.screen_threshold:
            kept = _screen(client, query, extra, ranked, search_text, limits, deadline, settings, case_id)
            if kept is not None:
                # Keep lexical order among survivors: it decides who goes first
                # if time runs short in the detailed stage.
                pool = [(record, value) for record, value in ranked if record.filename.casefold() in kept]
                if not pool:
                    return {"case_id": case_id, "papers": []}
        candidates = pool[:capacity]
        if len(pool) > capacity:
            LOGGER.warning("B candidate budget: evaluating %d of %d papers", capacity, len(pool))
        prompt_prefix = (
            f"检索需求：{query}\n\n"
            f"额外约束：\n{extra or '（无）'}\n\n"
            "候选论文（只能从这里选，filename 必须逐字一致）：\n"
        )
        prompt_suffix = (
            "\n\n请按相关性从高到低返回本批所有有证据支持且符合约束的论文；没有则返回空数组，"
            "不要求凑够篇数，也没有固定5篇限制。判定规则：\n"
            "1. 论文的核心主题、方法或任务与检索需求直接相关即可返回，不要求覆盖需求中的每个要素；\n"
            "2. 只共享 paper/study/method 这类泛词、没有实质主题重合的论文不要返回；\n"
            "3. 漏掉真正相关的论文和返回无关论文都会扣分，但命中一篇的收益明显高于一次误报的代价；"
            "主题明确相关而证据摘录不完整时，应保留；\n"
            "4. 必须满足排除条件、时间范围、数据集和任务等硬约束；相关主题但违反约束也不要返回；\n"
            "5. reason 要具体说明论文与检索需求的对应关系和原文依据，不能只写“相关”。\n"
            "只输出 JSON：{\"papers\":[{\"filename\":\"...\",\"reason\":\"...\"}]}。"
        )
        hits: list[dict[str, str]] = []
        seen: set[str] = set()
        successful_batches = 0
        for offset in range(0, len(candidates), limits.batch_size):
            check_active()
            if deadline.remaining() < 15:
                LOGGER.warning("B rerank stopped at deadline after %d candidates", offset)
                break
            batch = candidates[offset:offset + limits.batch_size]
            names = {record.filename.casefold(): record.filename for record, _ in batch}
            listing = []
            for index, (record, value) in enumerate(batch, start=1):
                excerpt = evidence_excerpt(record, query=search_text, max_chars=limits.excerpt_chars)
                listing.append(f"{index}. {record.filename}（RRF={value:.4f}）\n证据摘录：{excerpt}")
            try:
                record_stage(settings, case_id, "model_rerank", candidate_count=len(batch))
                text = client.complete(
                    [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt_prefix + "\n".join(listing) + prompt_suffix}],
                    temperature=0.1,
                    max_tokens=output_budget(),
                    timeout=deadline.timeout(settings.request_timeout, label="retrieval rerank"),
                )
                raw_hits = extract_json_object(text).get("papers")
                if not isinstance(raw_hits, list):
                    raise HandlerError("papers is not a list")
                successful_batches += 1
            except Exception as exc:
                check_active()
                record_stage(settings, case_id, "model_rerank_failed", error_type=type(exc).__name__)
                LOGGER.warning("B rerank batch failed (%s); retaining verified earlier batches", type(exc).__name__)
                raw_hits = []
            # A successful empty list is a genuine rejection. Never replace it
            # with keyword matches, and never require shared tokens to accept
            # a model's valid cross-language / synonymous relevance judgement.
            for item in raw_hits:
                if not isinstance(item, dict):
                    continue
                reason = str(item.get("reason", "")).strip()
                key = str(item.get("filename", "")).strip().casefold()
                # The platform matches file names case-insensitively; always
                # emit the real file name, never the model's spelling of it.
                filename = names.get(key, "")
                if not filename or key in seen or not reason:
                    if key and key not in names:
                        LOGGER.warning("B dropped a returned name that is not in the batch")
                    continue
                seen.add(key)
                hits.append({"filename": filename, "reason": reason})
                if len(hits) >= limits.results:
                    break
            if len(hits) >= limits.results:
                LOGGER.warning("B output budget reached: %d papers", limits.results)
                break
        if not successful_batches:
            # Lexical overlap cannot establish relevance or verify arbitrary
            # conditions, regardless of how the query is phrased.
            raise HandlerError("retrieval failed before any valid rerank response")
        return {"case_id": case_id, "papers": hits}
