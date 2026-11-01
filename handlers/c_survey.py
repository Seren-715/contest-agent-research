"""C / Survey: evidence-bound Markdown survey over the specified papers.

Uses PaperCode's Gate-Honest / ClaimLedger discipline as an executable gate:
the draft is audited after generation, failed claims/citations are sent back for
one complete revision, and missing coverage is completed with verbatim excerpts.
"""

from __future__ import annotations

from dataclasses import replace
import logging
import os
from typing import Any

from core.budget import Deadline
from core.diagnostics import record_stage
from core.config import Settings, case_budget_seconds
from core.execution import check_active
from core.llm import ModelClient, ModelError, output_budget
from core.materials import select_report_materials
from handlers.base import HandlerError, constraints_text, question_text
from handlers.reporting import (audit, core_evidence, finalize, materials_bundle, survey_materials_bundle,
                                survey_evidence_matrix)

SYSTEM = (
    "你是严谨的文献综述作者，按 PaperCode 的 Gate-Honest / ClaimLedger 纪律工作："
    "每条事实、数字、方法归因都必须绑定到指定论文的 [编号]；"
    "只能使用给定论文摘录中能确认的信息，无法确认的内容不写；"
    "区分论文报告的结果、作者观点与你的归纳；发现论文之间的冲突要明确说明；"
    "不要编造页码、数字、作者或文献。"
)

EVIDENCE_TOTAL_CHARS = 60000

CLAIM_RULES = (
    "证据规则（必须逐条遵守）：\n"
    "1. 每一句包含事实、数字、方法、数据集、指标或结论的话，句末都必须有 [编号]；\n"
    "2. 没有引用依据的数字和结论一律不要写，不要用常识补全；\n"
    "3. 只使用上方给出的论文摘录，不得引入摘录之外的具体事实；\n"
    "4. 参考文献条目必须逐字包含完整 PDF 文件名，编号与正文一致；\n"
    "5. 每篇指定论文都必须至少在正文引用一次。"
)


FACT_CHECK_MIN_SECONDS = 150


def _fact_check(complete, base_prompt: str, markdown: str, records, settings: Settings, case_id: str) -> str:
    """One extra pass: re-check every specific claim against the same excerpts.

    The structural audit cannot tell whether a cited sentence is supported, so
    unsupported specifics are removed or softened by a model that sees the
    original evidence again. The revision is kept only if the structural gate
    is no worse and the report did not collapse.
    """
    record_stage(settings, case_id, "fact_check")
    review = (
        "【逐句事实核对】\n"
        "请对照上方论文摘录，逐句检查草稿中的数字、数据集、指标、方法归因、比较结论和实验条件。"
        "某句话的任一要素在其所引论文的摘录中找不到直接依据（数字相同但方法、数据集、指标、单位或条件不符也算不支持），"
        "就删除该句，或改写为摘录能直接确认的较弱表述，或标明“未核验”。"
        "不要新增摘录之外的事实；保留有依据的句子、[编号] 引用、章节、对比表和 '## 参考文献'，每篇论文仍须被引用。"
        "返回完整修订版 Markdown，不要说明修改了什么。"
    )
    try:
        candidate = complete(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": base_prompt},
             {"role": "assistant", "content": markdown}, {"role": "user", "content": review}],
            0.1,
        ).strip()
    except (ModelError, TimeoutError):
        check_active()
        logging.getLogger(__name__).warning("survey_fact_check_failed case_id=%s", case_id)
        return markdown
    before = len(audit(markdown, records, "Survey").blockers)
    after = len(audit(candidate, records, "Survey").blockers)
    if after <= before and len(candidate) >= 0.5 * len(markdown):
        return candidate
    logging.getLogger(__name__).warning("survey_fact_check_rejected case_id=%s before=%d after=%d", case_id, before, after)
    return markdown


class SurveyHandler:
    def handle(self, question: dict[str, Any], settings: Settings, client: ModelClient) -> dict[str, Any]:
        deadline = Deadline(case_budget_seconds("Survey", settings))
        case_id = question["case_id"]
        query = question_text(question)
        record_stage(settings, case_id, "material_resolution")
        paths = select_report_materials(question, settings, client, deadline)
        if not paths:
            raise HandlerError("survey materials are missing: no PDFs resolved")
        use_matrix = os.environ.get("CONTEST_SURVEY_EVIDENCE_MATRIX", "0") == "1"
        record_stage(settings, case_id, "evidence_matrix" if use_matrix else "evidence_excerpt", material_count=len(paths))
        if use_matrix:
            block, records = survey_materials_bundle(paths, query=query, deadline=deadline)
        else:
            # Keep the original query-ranked evidence, and supplement it with
            # diverse facet windows. Either strategy alone can miss relevant
            # sections; both windows contain only verbatim indexed text.
            # Larger evidence budget: short windows rarely carried method,
            # dataset, metric and condition together, which numeric claims need.
            per_paper = min(2400, max(300, 14000 // len(paths)))
            base, records = materials_bundle(paths, query=query, max_chars=per_paper, deadline=deadline)
            # Abstract, contribution statement and conclusion of every paper
            # come first: they carry the headline claims and numbers, and the
            # query-ranked excerpts below do not guarantee them.
            scale = min(1.0, 5 / max(1, len(records)))
            core = core_evidence(records, abstract_chars=int(1500 * scale) or 1, contribution_chars=int(900 * scale) or 1,
                                 conclusion_chars=int(1200 * scale) or 1)
            matrix = survey_evidence_matrix(records, query=query,
                                            max_total_chars=max(9000, EVIDENCE_TOTAL_CHARS - len(core) - len(base) - 100))
            block = ("核心原文（各论文的摘要、贡献陈述与结论，逐字摘录）：\n" + core
                     + "\n\n按任务检索的原文摘录：\n" + base
                     + "\n\n补充原文窗口（候选维度仅用于定位）：\n" + matrix)
        record_stage(settings, case_id, "evidence_ready", record_count=len(records), evidence_chars=len(block))
        extra = constraints_text(question)
        evidence_heading = (
            "指定论文的候选证据矩阵（编号、完整文件名、内容哈希、PDF页码和原文跨度）：\n"
            if use_matrix else "指定论文（编号、完整文件名与可定位的原文摘录）：\n"
        )
        base_prompt = (
            f"综述任务：{query}\n\n"
            f"额外约束：\n{extra or '（无）'}\n\n"
            f"{evidence_heading}"
            f"{block}\n\n"
            "原文摘录及候选标签不是已核实事实；先按研究问题、方法、数据与实验条件、指标、"
            "结果与比较、局限六维整理各论文事实，再写综述。缺失信息明确未核验，不补造。"
            "评分按每篇论文的核心事实逐条核对覆盖，严重事实错误会倒扣，远重于漏写一条："
            "每篇论文都要写清“核心原文”中的方法或系统名称（按原文写法，原文给出别名时一并写出）、"
            "要解决的问题、核心机制、实验设置（模型、数据集或负载、硬件、对比基线）"
            "和摘要或结论中的主要量化结果；数字连同比较对象、条件和“最高/平均”等限定词按原文保留，"
            "不要把一篇论文的数字或结论归到另一篇。"
            "具体数值须同时核对方法、数据集、单位、指标、实验条件和比较对象；"
            "原文存在同一数字不等于支持该事实。尽量覆盖各篇论文的具体贡献和条件，"
            "不要仅写少量正确但空泛的背景。宁可少写数字，也不要写无法逐项对上原文条件的数字；"
            "对比表单元格只写摘录能直接确认的内容，无法确认写“未核验”。"
            "只有真实定位的页码才可引用。\n\n"
            f"{CLAIM_RULES}\n\n"
            "请写一篇正文 800～5000 字符（建议约3500）的中文 Markdown 综述，要求：\n"
            "正文排除标题行及参考文献节，空格、标点和内部换行计入；正文数字引用标记最多40组，"
            "[1,2]算一组，重复标记分别计数。超限时完整重写精简报告，不截断正文。\n"
            "1. 结构：## 引言与研究问题、## 方法与技术脉络、## 数据与评价指标比较、"
            "## 结论、局限与冲突、## 参考文献；\n"
            "2. 用一张 Markdown 表对比各论文的研究问题、方法、数据、评价指标、结论和局限；"
            "表内事实同样用 [编号] 支撑；\n"
            "3. 形成技术脉络，不要简单拼接摘要；不同论文的结论或数据冲突时，写明冲突和各自条件；\n"
            "4. 每节至少有一条引用，不能只把文献列在文末；\n"
            "5. 结尾必须有 '## 参考文献'，逐条列出 [编号] 和完整 PDF 文件名；\n"
            "6. 直接输出 Markdown 正文，不要 JSON、不要代码围栏。"
        )
        def complete(messages: list[dict[str, str]], temperature: float) -> str:
            check_active()
            timeout = deadline.timeout(settings.request_timeout, label="survey generation")
            bounded = replace(client, timeout=min(client.timeout, timeout)) if isinstance(client, ModelClient) else client
            return bounded.complete(messages, temperature=temperature, max_tokens=output_budget())

        record_stage(settings, case_id, "model_generation")
        text = complete(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": base_prompt}],
            0.25,
        )
        markdown = text.strip()
        report = audit(markdown, records, "Survey")
        if report.blockers and deadline.remaining() >= 30:
            repair = (
                "【证据闸门未通过】\n"
                + report.repair_text()
                + "\n\n以上一条草稿为基础，仅修复指出的问题，保留已经有来源的具体事实、条件、数值和章节。"
                "补齐可确认的引用和参考文献；没有原文支持则删除或明确未核验，不另造事实。"
                "修复后重新核对全篇5000正文字符/40引用组限制，返回完整修订版（不要只给差异）。"
            )
            try:
                candidate = complete(
                    [{"role": "system", "content": SYSTEM}, {"role": "user", "content": base_prompt},
                     {"role": "assistant", "content": markdown}, {"role": "user", "content": repair}],
                    0.15,
                ).strip()
                if len(audit(candidate, records, "Survey").blockers) <= len(report.blockers):
                    markdown = candidate
            except (ModelError, TimeoutError):
                # Keep the existing draft for the source-excerpt finalizer;
                # neither an upstream retry failure nor uncertainty erases it.
                check_active()
                logging.getLogger(__name__).warning("survey_repair_failed case_id=%s", case_id)
        if os.environ.get("CONTEST_SURVEY_FACT_CHECK", "1") != "0" and deadline.remaining() >= FACT_CHECK_MIN_SECONDS:
            markdown = _fact_check(complete, base_prompt, markdown, records, settings, case_id)
        return finalize(
            settings,
            "Survey",
            case_id,
            markdown,
            paths,
            require_all=True,
            records=records,
            query=query,
        )
