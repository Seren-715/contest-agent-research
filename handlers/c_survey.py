"""C / Survey: evidence-bound Markdown survey over the specified papers.

Generation follows the team version that scored C 23.5/25 on the platform
(eval 2026-10-26): the model returns sections of paragraphs, each with one
citation group, plus a per-paper comparison; the program only renders it and
the evidence gate audits it, with complete model revisions on findings.
Source selection (described library scopes) is this repository's.
"""

from __future__ import annotations

from typing import Any

from core.budget import Deadline
from core.config import Settings, case_budget_seconds
from core.diagnostics import record_stage
from core.llm import ModelClient
from core.materials import select_report_materials
from handlers.base import HandlerError, constraints_text, question_text
from handlers.reporting import generate_survey, survey_bundle

SYSTEM = (
    "你是严谨的文献综述作者，按 PaperCode 的 Gate-Honest / ClaimLedger 纪律工作："
    "每段事实、数字、方法归因都必须绑定到指定论文的段末合并引用 [编号]；"
    "引用是段落的证据标记，避免在每次提及论文时重复标注；"
    "只能使用给定论文摘录中能确认的信息，无法确认的内容不写；"
    "区分论文报告的结果、作者观点与你的归纳；发现论文之间的冲突要明确说明；"
    "不要编造页码、数字、作者或文献。只输出约定的 JSON 结构，完整撰写所有正文；"
    "文献引用由每段 citations 数组表达，text 字段不得出现方括号编号。"
)

CLAIM_RULES = (
    "证据规则（必须逐条遵守）：\n"
    "1. 每段论文事实、数字、方法或结论必须有对应的 [编号]；同段同来源只标一次，整篇最多 40 组；\n"
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
        paths = select_report_materials(question, settings, client, deadline)
        if not paths:
            raise HandlerError("survey materials are missing: no PDFs resolved")
        per_paper = max(3000, min(10000, 60000 // len(paths)))
        block, records = survey_bundle(paths, query=query, per_paper=per_paper, deadline=deadline)
        record_stage(settings, case_id, "evidence_ready", record_count=len(records), evidence_chars=len(block))
        extra = constraints_text(question)
        prompt = (
            f"综述任务：{query}\n\n"
            f"额外约束：\n{extra or '（无）'}\n\n"
            "指定论文（编号、完整文件名与证据摘录）：\n"
            f"{block}\n\n"
            f"{CLAIM_RULES}\n\n"
            "请写正文约 3500 字符、至少 800 字符且不超过 5000 字符的中文 Markdown 综述；"
            "参考文献不计正文上限；标题、空格、标点计入预算。要求：\n"
            "1. 结构：## 引言与研究问题、## 方法与技术脉络、## 数据与评价指标比较、"
            "## 结论、局限与冲突、## 参考文献；\n"
            "2. 用一张 Markdown 表对比各论文的研究问题、方法、数据、评价指标、结论和局限；"
            "每行只标一次引用，同一行其他单元格不重复；\n"
            "3. 形成技术脉络，不要简单拼接摘要；不同论文的结论或数据冲突时，写明冲突和各自条件；\n"
            "4. 每节至少有一条引用，不能只把文献列在文末；\n"
            "5. 结尾必须有 '## 参考文献'，逐条列出 [编号] 和完整 PDF 文件名；\n"
            "6. 正文引用目标 20～30 组，硬上限 40 组；每节控制在 5～7 组。"
            "相关论文可在段末合并为一组 [1,2,3]，不要写成反复的‘[1]... [2]... [1]...’。\n"
            "‘文献[1]’也算一组引用，不能作为反复使用的主语；用研究主题或方法简称指代。"
            "每个自然段最多在段末放一组引用，建议全文 10～15 个自然段，另加比较表。\n"
            "7. 材料未说明的数据源、测量设备、实验数字和方法细节明确写‘摘录未提供’，不要推测填表。\n"
            "8. 为可靠合并段落引用，返回下列 JSON，由程序仅排版为 Markdown。"
            "你负责完整撰写全部段落和比较表，不得返回程序或通用模板。"
            "sections 包含上列四个正文小节；每节 2～3 个完整自然段；每段 citations 仅列实际依据的论文编号。"
            "text 和表格单元格不得出现 [编号]，不要将引用作为主语反复插入。"
            "comparison 每篇一行；资料没有提供的字段写‘摘录未提供’，不要推测。"
            'JSON 结构：{"sections":[{"heading":"引言与研究问题","paragraphs":'
            '[{"text":"完整段落正文","citations":[1,2]}]}],"comparison":'
            '[{"paper":1,"problem":"...","method":"...","data":"...",'
            '"metrics":"...","findings":"...","limitations":"..."}]}。'
            "参考文献由给定文件名索引排版，不需重复输出；不要代码围栏。"
        )
        record_stage(settings, case_id, "model_generation")
        return generate_survey(client, settings, case_id, SYSTEM, prompt, records, paths, deadline)
