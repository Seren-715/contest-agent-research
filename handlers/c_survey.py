"""C / Survey: evidence-bound Markdown survey over the specified papers.

Uses PaperCode's Gate-Honest / ClaimLedger discipline as an executable gate:
the draft is audited after generation, failed claims/citations are sent back for
one complete revision, and missing coverage is completed with verbatim excerpts.
"""

from __future__ import annotations

from dataclasses import replace
import logging
from typing import Any

from core.budget import Deadline
from core.diagnostics import record_stage
from core.config import Settings, case_budget_seconds
from core.execution import check_active
from core.llm import ModelClient, ModelError
from core.paperlib import resolve_materials
from handlers.base import HandlerError, constraints_text, question_text
from handlers.reporting import audit, finalize, survey_materials_bundle as materials_bundle

SYSTEM = (
    "你是严谨的文献综述作者，按 PaperCode 的 Gate-Honest / ClaimLedger 纪律工作："
    "每条事实、数字、方法归因都必须绑定到指定论文的 [编号]；"
    "只能使用给定论文摘录中能确认的信息，无法确认的内容不写；"
    "区分论文报告的结果、作者观点与你的归纳；发现论文之间的冲突要明确说明；"
    "不要编造页码、数字、作者或文献。"
)

CLAIM_RULES = (
    "证据规则（必须逐条遵守）：\n"
    "1. 每一句包含事实、数字、方法、数据集、指标或结论的话，句末都必须有 [编号]；\n"
    "2. 没有引用依据的数字和结论一律不要写，不要用常识补全；\n"
    "3. 只使用上方给出的论文摘录，不得引入摘录之外的具体事实；\n"
    "4. 参考文献条目必须逐字包含完整 PDF 文件名，编号与正文一致；\n"
    "5. 每篇指定论文都必须至少在正文引用一次。"
)


class SurveyHandler:
    def handle(self, question: dict[str, Any], settings: Settings, client: ModelClient) -> dict[str, Any]:
        deadline = Deadline(case_budget_seconds("Survey", settings))
        case_id = question["case_id"]
        query = question_text(question)
        record_stage(settings, case_id, "material_resolution")
        paths = resolve_materials(question.get("materials"), settings.data_dir)
        if not paths:
            raise HandlerError("survey materials are missing: no PDFs resolved")
        record_stage(settings, case_id, "evidence_matrix", material_count=len(paths))
        block, records = materials_bundle(paths, query=query, deadline=deadline)
        record_stage(settings, case_id, "evidence_ready", record_count=len(records), evidence_chars=len(block))
        extra = constraints_text(question)
        base_prompt = (
            f"综述任务：{query}\n\n"
            f"额外约束：\n{extra or '（无）'}\n\n"
            "指定论文的候选证据矩阵（编号、完整文件名、内容哈希、PDF页码和原文跨度）：\n"
            f"{block}\n\n"
            "矩阵标签仅用于检索原文，不是已核实事实；先按研究问题、方法、数据与实验条件、指标、"
            "结果与比较、局限六维整理各论文事实，再写综述。缺失信息明确未核验，不补造。"
            "具体数值须同时核对方法、数据集、单位、指标、实验条件和比较对象；"
            "原文存在同一数字不等于支持该事实。尽量覆盖各篇论文的具体贡献和条件，"
            "不要仅写少量正确但空泛的背景。只有真实定位的页码才可引用。\n\n"
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
            return bounded.complete(messages, temperature=temperature, max_tokens=6000)

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
