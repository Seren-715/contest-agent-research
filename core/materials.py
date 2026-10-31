"""Resolve concrete report sources or select within an explicitly described library.

A natural-language library scope is not a missing filename. It requires a
relevance decision before it can become a list of cited report sources.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from core.budget import Deadline
from core.config import Settings
from core.diagnostics import record_stage
from core.execution import check_active
from core.io import write_json
from core.llm import ModelClient, ModelError
from core.paperlib import (Paper, bm25_rank, describe_material_scope, evidence_excerpt,
                           index_materials, is_material_description, resolve_materials)
from handlers.base import HandlerError, constraints_text, extract_json_object, question_text


def select_report_materials(
    question: dict[str, Any], settings: Settings, client: ModelClient, deadline: Deadline,
) -> list[Path]:
    case_id = question["case_id"]
    if not isinstance(case_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", case_id):
        raise HandlerError("invalid report case identity")
    declarations = question.get("materials")
    descriptions = describe_material_scope(declarations)
    paths = resolve_materials(declarations, settings.data_dir, allow_descriptions=bool(descriptions))
    if not descriptions:
        return paths
    values = [declarations] if isinstance(declarations, str) else declarations or []
    # An explicit PDF/directory list remains authoritative even if accompanied
    # by a prose explanation. Auxiliary-only declarations do not specify PDFs.
    explicit = [value for value in values if not is_material_description(value)]
    if explicit and resolve_materials(explicit, settings.data_dir):
        return paths
    if not paths:
        raise HandlerError("described paper library has no readable PDFs")
    record_stage(settings, case_id, "described_scope_selection", candidate_files=len(paths))
    work = Deadline(min(120.0, max(0.0, deadline.remaining() - 90.0)))
    work.require(1, "report source selection")
    records = [item for item in index_materials(paths, deadline=work) if item.text.strip()]
    if not records:
        raise HandlerError("described paper library has no extractable evidence")
    query = question_text(question)
    papers = [Paper(item.path, item.filename, item.text) for item in records]
    ranked = bm25_rank(query + " " + " ".join(descriptions), papers, top_k=len(papers))
    by_name = {item.filename: item for item in records}
    candidates = [by_name[item.filename] for item, _ in ranked[:80]]
    if len(records) > len(candidates):
        logging.getLogger(__name__).warning("report_selection_candidate_limit case_id=%s count=%d total=%d",
                                            case_id, len(candidates), len(records))
    selected: dict[str, dict[str, str]] = {}
    processed = 0
    for offset in range(0, len(candidates), 40):
        check_active()
        if selected and work.remaining() < 2:
            logging.getLogger(__name__).warning("report_selection_budget_stopped case_id=%s processed=%d", case_id, offset)
            break
        batch = candidates[offset:offset + 40]
        listing = "\n\n".join("文件名：" + item.filename + "\n" + evidence_excerpt(item, query, 750) for item in batch)
        prompt = (
            "材料范围是自然语言描述，不是具体文件清单。请从下列实际PDF选择本题有原文证据支持的论文。\n"
            "材料范围：" + "；".join(descriptions) + "\n任务：" + query
            + "\n约束：" + constraints_text(question) + "\n候选PDF：\n" + listing
            + "\n返回符合主题及任务要求的所有论文，不凑数；拒绝不相关或违反排除条件的论文。"
            "若题目要求至少若干篇，应在有证据的前提下尽量满足，不得编造。"
            "只输出JSON：{\"papers\":[{\"filename\":\"完整原名.pdf\",\"reason\":\"具体相关依据\"}]}。"
        )
        try:
            timeout = min(work.timeout(settings.request_timeout, label="report source selection"),
                          deadline.timeout(settings.request_timeout, reserve=90, label="report generation reserve"))
            text = client.complete([{"role": "system", "content": "你负责在允许论文范围内核验相关性，只输出JSON。"},
                                    {"role": "user", "content": prompt}],
                                   temperature=0.1, max_tokens=4000, timeout=timeout)
            hits = extract_json_object(text).get("papers")
            if not isinstance(hits, list):
                raise HandlerError("report source selection did not return papers")
        except (ModelError, TimeoutError, HandlerError, ValueError):
            check_active()
            if not selected:
                raise
            logging.getLogger(__name__).warning("report_selection_partial case_id=%s processed=%d selected=%d",
                                                case_id, offset, len(selected))
            break
        allowed = {item.filename for item in batch}
        processed += len(batch)
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            name, reason = hit.get("filename"), hit.get("reason")
            if isinstance(name, str) and name in allowed and isinstance(reason, str) and len(reason.strip()) >= 8:
                selected[name] = {"filename": name, "reason": reason.strip(), "sha256": by_name[name].sha256}
    if not selected:
        raise HandlerError("no source matched the described report scope")
    # This is a selection receipt, not a claim that model relevance is gold.
    write_json(settings.team_dir / "work" / "materials" / f"{case_id}.json",
               {"case_id": case_id, "scope_kind": "described_library", "candidates": len(candidates),
                "processed_candidates": processed, "candidate_coverage_complete": processed == len(candidates),
                "selected": list(selected.values()), "relevance_verified_by": "model_on_source_excerpts"})
    record_stage(settings, case_id, "report_sources_selected", selected_count=len(selected))
    return [by_name[name].path for name in selected]
