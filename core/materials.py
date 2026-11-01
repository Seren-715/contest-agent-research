"""Resolve concrete report sources or select within an explicitly described library.

A natural-language library scope is not a missing filename. It requires a
relevance decision before it can become a list of cited report sources.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from core.budget import Deadline
from core.config import Settings
from core.diagnostics import record_stage
from core.execution import check_active
from core.io import write_json
from core.llm import ModelClient, ModelError, output_budget
from core.paperlib import (Paper, bm25_rank, describe_material_scope, evidence_excerpt,
                           index_materials, material_exists, resolve_materials)
from handlers.base import HandlerError, constraints_text, extract_json_object, question_text


def _env_int(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_number(text: str) -> int | None:
    """Chinese numerals 1-99: 五, 十, 十二, 二十, 两."""
    if text.isdigit():
        return int(text)
    if "十" not in text:
        return _CN_DIGITS.get(text) if len(text) == 1 else None
    tens, _, ones = text.partition("十")
    if (tens and tens not in _CN_DIGITS) or (ones and ones not in _CN_DIGITS):
        return None
    return (_CN_DIGITS[tens] if tens else 1) * 10 + (_CN_DIGITS[ones] if ones else 0)


def _required_count(question: dict[str, Any]) -> int | None:
    """Best-effort count the task states ("至少十篇", "5 篇指定论文"), else None.

    Reads title, input, constraints and expected_output; the largest match
    wins. Used only as a hint in the selection prompt, never to cut results.
    """
    fields = [question.get(key) for key in ("title", "input", "expected_output")]
    constraints = question.get("constraints")
    fields.extend(constraints if isinstance(constraints, list) else [constraints])
    text = " ".join(str(value) for value in fields if value)
    number = r"(\d+|[零一二两三四五六七八九十]{1,3})"
    counts = []
    unit = r"\s*(?:篇|项|份|个)"
    work = r"\s*(?:指定|实际引用的|相关)?\s*的?\s*(?:论文|文献|材料|PDF|研究|工作)"
    for pattern in (r"(?:至少|不少于|不低于|最少)\s*(?:引用|列出|使用|包含|涵盖|比较)?\s*" + number + unit,
                    number + unit + work,
                    r"at\s+least\s+(\d+)\s+(?:[\w-]+\s+){0,3}?(?:papers?|sources?|articles?|studies)",
                    r"(\d+)\s+(?:designated|specified)\s+papers?"):
        for match in re.finditer(pattern, text, re.IGNORECASE):
            value = _cn_number(match.group(1))
            if value is not None and 1 <= value <= 50:
                counts.append(value)
    return max(counts) if counts else None


def _display_name(path: Path) -> str:
    """Up to two parent folders plus the file name: folders often name the topic."""
    return "/".join(path.parts[-3:])


def select_report_materials(
    question: dict[str, Any], settings: Settings, client: ModelClient, deadline: Deadline,
) -> list[Path]:
    case_id = question["case_id"]
    if not isinstance(case_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", case_id):
        raise HandlerError("invalid report case identity")
    declarations = question.get("materials")
    # An existing file or directory is never a description, even when its
    # name reads like one (e.g. a folder "指定论文").
    descriptions = [value for value in describe_material_scope(declarations)
                    if not material_exists(value, settings.data_dir)]
    paths = resolve_materials(declarations, settings.data_dir, allow_descriptions=bool(descriptions))
    if not descriptions:
        return paths
    values = [declarations] if isinstance(declarations, str) else declarations or []
    # An explicit PDF/directory list remains authoritative even if accompanied
    # by a prose explanation. Auxiliary-only declarations do not specify PDFs.
    explicit = [value for value in values if not (isinstance(value, str) and value.strip() in descriptions)]
    if explicit and resolve_materials(explicit, settings.data_dir):
        return paths
    if not paths:
        raise HandlerError("described paper library has no readable PDFs")
    record_stage(settings, case_id, "described_scope_selection", candidate_files=len(paths))
    # Indexing and selection get separate budgets; generation keeps the rest.
    work = Deadline(min(150.0, 0.15 * deadline.remaining() + 30.0))  # index_materials keeps 30 s of it
    work.require(1, "report source indexing")
    records = [item for item in index_materials(paths, deadline=work) if item.text.strip()]
    if not records:
        raise HandlerError("described paper library has no extractable evidence")
    query = question_text(question)
    title = str(question.get("title") or "").strip()
    stated = _required_count(question)
    required = stated or 5  # the rules score five designated materials
    papers = [Paper(item.path, _display_name(item.path), item.text) for item in records]
    # BM25 only orders the list; it never removes a paper. A Chinese task over
    # English papers scores ~0 everywhere, so a cut-off would be alphabetical.
    ranked = bm25_rank(" ".join([title, query, *descriptions]), papers, top_k=len(papers))
    order = [paper.path for paper, _ in ranked] + [paper.path for paper in papers]
    by_path = {item.path: item for item in records}
    candidates = [by_path[path] for path in dict.fromkeys(order)]
    by_name = {item.filename: item for item in records}
    batch_size = _env_int("CONTEST_SELECT_BATCH", 160)
    excerpt_chars = _env_int("CONTEST_SELECT_EXCERPT_CHARS", 600)
    selected: dict[str, dict[str, str]] = {}
    processed = 0
    for offset in range(0, len(candidates), batch_size):
        check_active()
        generation_reserve = min(300.0, 0.35 * deadline.remaining())
        if selected and deadline.remaining() < generation_reserve + 30:
            logging.getLogger(__name__).warning("report_selection_budget_stopped case_id=%s processed=%d", case_id, offset)
            break
        batch = candidates[offset:offset + batch_size]
        listing = "\n\n".join(
            f"{index}. 路径：{_display_name(item.path)}\n文件名：{item.filename}\n"
            + " ".join(evidence_excerpt(item, " ".join([title, query]), excerpt_chars).split())
            for index, item in enumerate(batch, start=1)
        )
        prompt = (
            "本题的材料是论文库中被“指定”的一组论文，不是所有相关论文。题目通常指定 "
            f"{required} 篇。请根据题目标题、任务、材料描述，并结合目录名、文件名、论文标题和摘录，"
            "找出这组指定论文。\n"
            f"题目标题：{title or '（无）'}\n任务：{query}\n材料描述：" + "；".join(descriptions)
            + "\n约束：" + (constraints_text(question) or "（无）") + "\n\n候选PDF：\n" + listing + "\n\n"
            "要求：按“属于本题指定论文”的把握从高到低排序；同一目录下与题目主题一致的一组论文通常就是指定论文；"
            "题目点名的方法、系统或论文（包括其别名）必须找出。漏掉一篇指定论文的代价远高于多选一篇同主题论文，"
            "因此把握大的标 confidence=\"core\"，与题目主题一致但拿不准是否属于指定组的标 \"possible\"；"
            "明显属于其他题目或其他方向的论文不要返回；不得编造文件名。"
            "只输出JSON：{\"papers\":[{\"filename\":\"完整原名.pdf\",\"confidence\":\"core\",\"reason\":\"具体依据\"}]}。"
        )
        try:
            timeout = deadline.timeout(settings.request_timeout, reserve=generation_reserve,
                                       label="report source selection")
            text = client.complete([{"role": "system", "content": "你负责在论文库中识别本题指定的论文，只输出JSON。"},
                                    {"role": "user", "content": prompt}],
                                   temperature=0.1, max_tokens=output_budget(), timeout=timeout)
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
                # A missing label keeps the earlier behaviour: the paper counts as core.
                confidence = "possible" if str(hit.get("confidence", "")).strip().lower() == "possible" else "core"
                selected.setdefault(name, {"filename": name, "reason": reason.strip(), "sha256": by_name[name].sha256,
                                           "confidence": confidence})
    # Nothing is cut: a parsed count can be incomplete ("至少 5 篇……十余项"),
    # and a missed designated paper loses citation credit while an extra one
    # does not (scoring rules, 引文溯源类). The label only orders the list:
    # core papers first, each group in the model's order.
    order = ([name for name, item in selected.items() if item["confidence"] == "core"]
             + [name for name, item in selected.items() if item["confidence"] == "possible"])
    selected = {name: selected[name] for name in order}
    if not selected:
        raise HandlerError("no source matched the described report scope")
    # This is a selection receipt, not a claim that model relevance is gold.
    write_json(settings.team_dir / "work" / "materials" / f"{case_id}.json",
               {"case_id": case_id, "scope_kind": "described_library", "candidates": len(candidates),
                "processed_candidates": processed, "candidate_coverage_complete": processed == len(candidates),
                "selected": list(selected.values()), "relevance_verified_by": "model_on_source_excerpts"})
    record_stage(settings, case_id, "report_sources_selected", selected_count=len(selected))
    return [by_name[name].path for name in selected]
