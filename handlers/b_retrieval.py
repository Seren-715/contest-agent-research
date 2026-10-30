"""B / Literature_Retrieval: find local PDFs, return exact filenames and reasons.

Retrieval follows PaperCode's material-index + literature-map design: each PDF
is indexed (hash/pages/title/abstract), candidates are ranked with BM25 over the
local corpus, and the model reranks a small, evidence-rich candidate list. False
positives are explicitly discouraged because the contest penalises them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.config import Settings
from core.llm import ModelClient
from core.paperlib import (
    MaterialRecord,
    Paper,
    bm25_rank,
    evidence_excerpt,
    index_materials,
    resolve_materials,
    terms,
)
from handlers.base import HandlerError, constraints_text, extract_json_object, question_text
from tools import pdf_probe

SYSTEM = (
    "你是文献检索助手。只能使用候选论文列表中的完整文件名，不能编造文件名或论文。"
    "只输出一个 JSON 对象，不要加代码围栏。"
)


def _has_cjk(text: str) -> bool:
    return any("一" <= char <= "鿿" for char in text)


def _expand_query(client: ModelClient, query: str) -> list[str]:
    """Translate a Chinese search request into English keywords.

    The local paper library is often English while the contest query is Chinese.
    Keyword ranking alone would miss everything; this is a cheap bridge before
    the LLM rerank.
    """
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
            max_tokens=400,
        )
        data = extract_json_object(text)
        values = data.get("keywords")
        if isinstance(values, list):
            return [str(item).strip() for item in values if str(item).strip()]
    except Exception:
        return []
    return []


STOP_TERMS = {
    "paper", "papers", "study", "studies", "research", "method", "methods",
    "approach", "approaches", "data", "dataset", "datasets", "result", "results",
    "using", "based", "model", "models", "analysis", "review", "propose",
    "proposed", "work", "works", "article", "articles",
}


def _matched_terms(search_text: str, record: MaterialRecord) -> set[str]:
    query_terms = set(terms(search_text))
    paper_terms = set(terms(record.title + " " + record.abstract + " " + record.text))
    return (query_terms & paper_terms) - STOP_TERMS


class RetrievalHandler:
    def handle(self, question: dict[str, Any], settings: Settings, client: ModelClient) -> dict[str, Any]:
        case_id = question["case_id"]
        query = question_text(question)
        paths = resolve_materials(question.get("materials"), settings.data_dir)
        if not paths:
            return {"case_id": case_id, "papers": []}
        # Do not trust extensions: keep only files that actually look like PDFs.
        paths = [Path(str(item["path"])) for item in (pdf_probe(str(path)) for path in paths) if item["looks_like_pdf"]]
        if not paths:
            return {"case_id": case_id, "papers": []}
        paths = paths[:20]

        records = index_materials(paths, max_pages=6)
        papers = [Paper(path=record.path, filename=record.filename, text=record.text) for record in records]
        search_text = query
        expanded = False
        if _has_cjk(query):
            keywords = _expand_query(client, query)
            if keywords:
                search_text = query + " " + " ".join(keywords)
                expanded = True
        candidates = bm25_rank(search_text, papers, top_k=15)
        by_name = {record.filename: record for record in records}
        candidate_names = {paper.filename for paper, _ in candidates}
        listing = []
        for index, (paper, value) in enumerate(candidates, start=1):
            record = by_name[paper.filename]
            excerpt = evidence_excerpt(paper, query=search_text, max_chars=1000).replace("\n", " ")
            listing.append(
                f"{index}. {paper.filename}（BM25={value:.2f}）\n"
                f"   标题：{record.title}\n"
                f"   证据摘录：{excerpt or '（无可提取正文）'}"
            )
        extra = constraints_text(question)
        prompt = (
            f"检索需求：{query}\n\n"
            f"额外约束：\n{extra or '（无）'}\n\n"
            "候选论文（只能从这里选，filename 必须逐字一致）：\n"
            + "\n".join(listing)
            + "\n\n请按相关性从高到低排序，最多返回 5 篇。判定规则：\n"
            "1. 每篇必须覆盖检索需求中的至少两个具体要素（主题、方法、数据、任务或结论）；\n"
            "2. 只共享 paper/study/method 这类泛词、没有实质主题重合的论文不要返回；\n"
            "3. 误报会按每篇扣分，漏掉真正相关的论文也会扣分；不确定时保留覆盖多个要素的论文；\n"
            "4. reason 要具体说明覆盖了哪些检索要素，不能只写“相关”。\n"
            "只输出 JSON：{\"papers\":[{\"filename\":\"...\",\"reason\":\"...\"}]}。"
        )
        try:
            text = client.complete(
                [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=2000,
            )
            data = extract_json_object(text)
            raw_hits = data.get("papers")
            if not isinstance(raw_hits, list):
                raise HandlerError("papers is not a list")
        except Exception:
            raw_hits = []

        hits: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw_hits:
            if not isinstance(item, dict):
                continue
            filename = str(item.get("filename", "")).strip()
            reason = str(item.get("reason", "")).strip()
            if filename not in candidate_names or filename in seen or len(reason) < 12:
                continue
            record = by_name.get(filename)
            if record is None or (expanded and not _matched_terms(search_text, record)):
                # With query expansion, a filename with no shared content term
                # is a likely false positive; without expansion the LLM rerank
                # is the only cross-language signal, so keep its choice.
                continue
            seen.add(filename)
            hits.append({"filename": filename, "reason": reason})
            if len(hits) >= 5:
                break

        if not hits:
            # Honest deterministic fallback: keep only papers with multiple
            # distinct query-term hits, not a single generic word.
            for paper, value in candidates:
                if value <= 0:
                    continue
                record = by_name[paper.filename]
                matched = _matched_terms(search_text, record)
                if len(matched) < 2:
                    continue
                reason = "标题/摘要/正文同时命中检索要素：" + "、".join(sorted(matched)[:8])
                hits.append({"filename": paper.filename, "reason": reason})
                if len(hits) >= 5:
                    break

        return {"case_id": case_id, "papers": hits}
