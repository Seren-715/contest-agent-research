"""E / Idea: an evidence-bound, falsifiable research proposal.

The structure follows PaperCode's NoveltyCheckpoint -> PaperSpine discipline:
first state what the specified papers already cover and where the overlap is,
then derive a differentiated, falsifiable idea with an explicit evidence map.
The audit gate checks citation support, section coverage, novelty/complementarity
language, and the boundary between simulated observations and paper conclusions.
"""

from __future__ import annotations

from typing import Any
from dataclasses import replace

from core.config import Settings, case_budget_seconds
from core.budget import Deadline
from core.diagnostics import record_stage
from core.execution import check_active
from core.llm import ModelClient
from core.materials import select_report_materials
from handlers.base import HandlerError, constraints_text, question_text
from handlers.evidence import novelty_overlap
from handlers.reporting import audit, finalize, materials_bundle

SYSTEM = (
    "你是科研方案设计者，按 PaperCode 的 NoveltyCheckpoint / PaperSpine / Gate-Honest 纪律工作："
    "先识别已有方法与重合点，再论证差异与互补；中心论点必须由证据链支撑；"
    "题目里的模拟观察只能当作待验证现象，不能写成论文结论或已完成的改进实验；"
    "区分已有方法与你的新机制；只使用给定论文摘录中能确认的事实。"
)

CLAIM_RULES = (
    "证据规则（必须逐条遵守）：\n"
    "1. 每一句涉及已有论文事实、方法、实测数字或结论的话，句末都必须有 [编号]；\n"
    "2. 明确标为未来计划、待验证假设或验收目标的实验设计与量化阈值不冒充已有结果，"
    "不为这些新设计虚挂论文引用；同句若包含已有结果或论文归因，仍必须引用该部分的来源；\n"
    "3. 题目给出的模拟观察只能写成待验证现象，不能写成论文结论或已完成的实验；\n"
    "4. 参考文献条目必须逐字包含完整 PDF 文件名；\n"
    "5. 至少引用一篇指定论文，并在正文说明每篇论文补上了哪一块证据。"
)

CHECKLIST = (
    "报告必须覆盖以下内容，并使用同名 Markdown 标题：\n"
    "## 研究问题与边界\n"
    "## 动机与已有方法局限\n"
    "## 核心 Idea 与技术路线\n"
    "## 基线、数据集与实验设计\n"
    "## 消融与量化指标\n"
    "## 多文献互补关系\n"
    "## 复现瓶颈反思\n"
    "## 可行性与失败边界\n"
    "## 创新论证与可证伪方式\n"
    "## 参考文献\n\n"
    "其中「动机与已有方法局限」要先写 NoveltyCheckpoint：已有工作覆盖了什么、还缺什么、"
    "你的方案与它们的差异是什么；「多文献互补关系」要逐篇说明补上了哪一块证据；"
    "「复现瓶颈反思」要写清数据、环境、算力、评价口径或工程实现上的具体瓶颈；"
    "「可行性与失败边界」要同时说明场景可行性、落地约束（算力、延迟、数据权限、工程成本）和失败边界；"
    "「创新论证与可证伪方式」要给出可证伪的预测和判定阈值。"
)


class IdeaHandler:
    def handle(self, question: dict[str, Any], settings: Settings, client: ModelClient) -> dict[str, Any]:
        deadline = Deadline(case_budget_seconds("Idea", settings))
        case_id = question["case_id"]
        query = question_text(question)
        record_stage(settings, case_id, "material_resolution")
        paths = select_report_materials(question, settings, client, deadline)
        if not paths:
            raise HandlerError("idea materials are missing: no PDFs resolved")
        block, records = materials_bundle(paths, query=query, max_chars=1500, deadline=deadline)
        record_stage(settings, case_id, "evidence_ready", material_count=len(paths), evidence_chars=len(block))
        overlaps = novelty_overlap(query, records)
        overlap_lines = "\n".join(
            f"- {record.filename}：主题词重合度 {ratio:.0%}；标题：{record.title}"
            for record, ratio in overlaps
        )
        extra = constraints_text(question)
        base_prompt = (
            f"研究灵感任务：{query}\n\n"
            f"额外约束：\n{extra or '（无）'}\n\n"
            "指定论文（编号、完整文件名与证据摘录）：\n"
            f"{block}\n\n"
            "【已有工作重合度（本地词项重叠，不是查新结论）】\n"
            f"{overlap_lines or '（未计算出重合度）'}\n\n"
            f"{CLAIM_RULES}\n\n"
            f"{CHECKLIST}\n\n"
            "正文800～4500字符，建议1800～3200；标题行及参考文献节不计，空格、标点和内部换行计入。"
            "不得使用无关通用段落或重复句凑字数；超限时完整重写精简报告，不截断正文。"
            "直接输出 Markdown 正文，不要 JSON、不要代码围栏。"
        )
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": base_prompt}]
        def complete(messages, temperature):
            check_active()
            timeout = deadline.timeout(settings.request_timeout, label="idea generation")
            bounded = replace(client, timeout=min(client.timeout, timeout)) if isinstance(client, ModelClient) else client
            return bounded.complete(messages, temperature=temperature, max_tokens=6000)
        record_stage(settings, case_id, "model_generation")
        text = complete(
            messages,
            0.3,
        )
        markdown = text.strip()
        report = audit(markdown, records, "Idea")
        if report.blockers and deadline.remaining() >= 30:
            repair = (
                "【证据闸门未通过】\n"
                + report.repair_text()
                + "\n\n请返回完整修订版（不要只给差异）：补齐引用和参考文献，删除无法核验的事实，"
                "保留全部要求的小节，只输出 Markdown 正文。"
            )
            messages = messages + [{"role": "assistant", "content": text}, {"role": "user", "content": repair}]
            text = complete(
                messages,
                0.2,
            )
            markdown = text.strip()
        return finalize(
            settings,
            "Idea",
            case_id,
            markdown,
            paths,
            require_all=False,
            records=records,
            query=query,
        )
