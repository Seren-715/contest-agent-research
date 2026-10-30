"""Evidence gate for Survey / Idea reports.

This is a Python port of PaperCode's ClaimLedger + verification discipline:
every factual sentence must carry a citation to a specified paper, every
specified paper must be traceable in the body and reference list, and a report
with unresolved blockers is not treated as final. The gate never invents facts;
when coverage is missing it appends a short verbatim excerpt from the indexed
paper, marked as an excerpt rather than an inference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.paperlib import MaterialRecord, sentence_split, terms
from core.schemas import report_limit_errors

REFERENCE_MARKER = "## 参考文献"
PLACEHOLDER_RE = re.compile(
    r"\b(TODO|TBD|FIXME|XXX)\b|待补充|待完善|占位符|placeholder|lorem ipsum|\[citation needed\]|\?\?\?",
    re.IGNORECASE,
)
CITATION_RE = re.compile(r"\[\s*(\d+(?:\s*[,，]\s*\d+)*)(?:\s*,\s*p\.?\s*\d+)?\s*\]")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", re.MULTILINE)
SIMULATION_CLAIM_RE = re.compile(r"(模拟观察|simulated observation)[^。！？.!?]{0,80}(论文|paper)[^。！？.!?]{0,40}(证明|表明|发现|show|prove)", re.IGNORECASE)


@dataclass
class Finding:
    category: str
    severity: str  # "blocker" | "warning"
    message: str


@dataclass
class Claim:
    text: str
    citations: list[int] = field(default_factory=list)
    supported: bool = False
    requires_citation: bool = True


@dataclass
class AuditReport:
    function: str
    findings: list[Finding] = field(default_factory=list)
    ledger: list[Claim] = field(default_factory=list)

    @property
    def blockers(self) -> list[Finding]:
        return [item for item in self.findings if item.severity == "blocker"]

    @property
    def warnings(self) -> list[Finding]:
        return [item for item in self.findings if item.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.blockers

    def repair_text(self, limit: int = 12) -> str:
        lines = [f"- [{item.category}] {item.message}" for item in self.findings[:limit]]
        return "\n".join(lines) if lines else "（无）"


def _body_and_refs(markdown: str) -> tuple[str, str]:
    if REFERENCE_MARKER in markdown:
        body, _, refs = markdown.partition(REFERENCE_MARKER)
        return body, refs
    return markdown, ""


def headings(markdown: str) -> list[str]:
    return [value.strip().lower() for value in HEADING_RE.findall(markdown)]


def has_section(markdown: str, *keywords: str) -> bool:
    haystack = "\n".join(headings(markdown))
    return any(keyword.lower() in haystack for keyword in keywords)


def citation_ids(text: str) -> list[int]:
    return [int(value) for group in CITATION_RE.findall(text or "") for value in re.findall(r"\d+", group)]


STRONG_FACT_RE = re.compile(
    r"\d|%|准确率|召回率|精确率|mrr|recall|precision|f1|bm25|mae|rmse|latency|"
    r"吞吐|耗时|优于|低于|高于|提升|下降|达到|报告了|表明|发现|证明|结果显示|"
    r"experiment(?:s)? show|results? show|we (?:found|report)|outperform|improv(?:e|ed)",
    re.IGNORECASE,
)


# An Idea may define future experiments and acceptance thresholds. These are
# proposals, not reported measurements. A mixed sentence still needs evidence
# for any attributed method/result, even when it also describes a future plan.
REPORTED_FACT_RE = re.compile(
    r"报告了|报告显示|结果显示|实验证明|实验表明|已经|已获得|已达到|实测结果|已实测|观察到|"
    r"取得了|获得了|达到了|"
    r"(?:本次|本轮|实际|已完成的)实验(?:的)?(?:准确率|召回率|吞吐|耗时|误差)(?:为|是|达到)|"
    r"(?:论文|文献|作者|已有研究|现有研究|已有方法|该方法|原方法|基线方法)[^。！？.!?]{0,80}"
    r"(?:提出|采用|使用|发现|证明|表明|达到|取得|获得|提升|降低|优于|(?:准确率|吞吐)(?:为|是))|"
    r"(?:paper|authors?|prior work|previous work)[^.!?]{0,80}"
    r"(?:propos|us(?:e|es|ed)|found|show|achiev|report)|"
    r"results? show|experiments? show|we (?:found|report)|measured|observed",
    re.IGNORECASE,
)


def _is_fact_claim(sentence: str, function: str = "Survey") -> bool:
    if len(sentence) < 12:
        return False
    if re.match(r"^\s*(#|[-*]\s*\[|\||```)", sentence):
        return False
    if function == "Idea":
        # Numbers/metric names do not establish that a sentence is reporting
        # an existing result. Keep that lexical uncertainty out of blockers.
        return bool(REPORTED_FACT_RE.search(sentence))
    # Transitional/structural sentences ("本节比较...", "本文提出...") are not
    # empirical claims; requiring a citation on them causes needless repairs.
    if re.match(r"^\s*(本节|本文|下面|首先|其次|再次|最后|综上|因此|据此|为此|接下来|本报告|该节)", sentence):
        return False
    return bool(STRONG_FACT_RE.search(sentence))


def build_ledger(markdown: str, record_count: int, function: str = "Survey") -> list[Claim]:
    body, _ = _body_and_refs(markdown)
    # Headings are structural, not claims; remove them before sentence splitting
    # so a heading cannot swallow the first factual sentence of its section.
    body = re.sub(r"^\s{0,3}#{1,6}\s+.*$", "", body, flags=re.MULTILINE)
    ledger: list[Claim] = []
    for sentence in sentence_split(body):
        values = citation_ids(sentence)
        if values:
            ledger.append(Claim(sentence, values, all(1 <= value <= record_count for value in values)))
        elif _is_fact_claim(sentence, function):
            ledger.append(Claim(sentence, [], False))
        elif function == "Idea" and STRONG_FACT_RE.search(sentence):
            ledger.append(Claim(sentence, [], False, requires_citation=False))
    return ledger


def _citation_coverage(markdown: str, records: list[MaterialRecord], function: str) -> list[Finding]:
    body, refs = _body_and_refs(markdown)
    findings: list[Finding] = []
    used = set(citation_ids(body))
    if function == "Survey":
        for index, record in enumerate(records, start=1):
            if index not in used:
                findings.append(
                    Finding(
                        "missing_inline_citation",
                        "blocker",
                        f"正文没有引用 [{index}]（{record.filename}）；综述要求逐篇引用指定材料。",
                    )
                )
            if record.filename not in refs:
                findings.append(
                    Finding(
                        "missing_reference_entry",
                        "blocker",
                        f"参考文献没有列出完整文件名：{record.filename}",
                    )
                )
    else:
        if not used:
            findings.append(Finding("missing_inline_citation", "blocker", "正文没有任何 [编号] 引用。"))
        if records and not any(record.filename in refs for record in records):
            findings.append(
                Finding("missing_reference_entry", "blocker", "参考文献没有列出任何指定 PDF 的完整文件名。")
            )
    return findings


def _section_coverage(markdown: str, function: str) -> list[Finding]:
    findings: list[Finding] = []
    required: list[tuple[str, tuple[str, ...]]] = (
        [
            ("研究问题/引言", ("引言", "研究问题", "introduction")),
            ("方法与技术脉络", ("方法", "技术路线", "method")),
            ("数据与评价指标", ("数据", "指标", "dataset", "metric")),
            ("结论与局限", ("结论", "局限", "conclusion", "limitation")),
        ]
        if function == "Survey"
        else [
            ("研究问题与边界", ("研究问题", "边界", "research question")),
            ("动机与已有方法局限", ("动机", "已有方法", "局限", "motivation")),
            ("核心 Idea 与技术路线", ("idea", "技术路线", "方法", "technical route")),
            ("基线、数据集与实验设计", ("基线", "数据集", "实验设计", "baseline")),
            ("消融与量化指标", ("消融", "指标", "ablation", "metric")),
            ("多文献互补关系", ("互补", "complement")),
            ("复现瓶颈反思", ("瓶颈", "复现", "bottleneck", "reproduc")),
            ("可行性与失败边界", ("可行性", "失败边界", "feasibility", "failure")),
            ("创新论证与可证伪方式", ("创新", "可证伪", "novelty", "falsif")),
        ]
    )
    for label, keywords in required:
        if not has_section(markdown, *keywords) and not any(keyword in markdown.lower() for keyword in keywords):
            # The contest schema does not require these exact nine headings.
            # Coverage affects semantic quality/scoring, not answer validity.
            report_finding = Finding("missing_section", "warning", f"缺少或未明确写出「{label}」部分。")
            findings.append(report_finding)
    return findings


def audit_report(markdown: str, records: list[MaterialRecord], function: str) -> AuditReport:
    report = AuditReport(function=function)
    text = markdown or ""
    for error in report_limit_errors(text, function):
        report.findings.append(Finding("report_limits", "blocker", error))
    placeholders = sorted(set(value.group(0) for value in PLACEHOLDER_RE.finditer(text)))
    if placeholders:
        report.findings.append(
            Finding("placeholder_text", "blocker", "正文仍含占位标记：" + "、".join(placeholders[:5]))
        )

    report.findings.extend(_citation_coverage(text, records, function))
    report.findings.extend(_section_coverage(text, function))

    report.ledger = build_ledger(text, len(records), function)
    unsupported = [claim for claim in report.ledger if not claim.supported and claim.requires_citation]
    for claim in unsupported[:8]:
        report.findings.append(
            Finding(
                "unsupported_claim",
                "blocker",
                "事实性语句没有可追溯引用：" + claim.text[:110],
            )
        )
    if len(unsupported) > 8:
        report.findings.append(
            Finding("unsupported_claim", "blocker", f"另有 {len(unsupported) - 8} 条事实性语句缺少引用。")
        )

    uncertain = [claim for claim in report.ledger if not claim.supported and not claim.requires_citation]
    for claim in uncertain[:8]:
        report.findings.append(
            Finding(
                "claim_semantics_unverified", "warning",
                "指标或量化表述须区分设计、待验证阈值与已有结果；关键词匹配不能核定事实：" + claim.text[:110],
            )
        )

    invalid = sorted({value for claim in report.ledger for value in claim.citations if value < 1 or value > len(records)})
    if invalid:
        report.findings.append(
            Finding("invalid_citation", "blocker", "引用编号超出指定材料范围：" + "、".join(map(str, invalid)))
        )

    if function == "Idea":
        body, _ = _body_and_refs(text)
        if not re.search(r"差异|不同|区别|differ", body, re.IGNORECASE):
            report.findings.append(Finding("novelty_gap", "warning", "没有明确说明与已有工作的差异。"))
        if not re.search(r"互补|complement", body, re.IGNORECASE):
            report.findings.append(Finding("complementarity_gap", "warning", "没有明确说明多篇文献的互补关系。"))
        if SIMULATION_CLAIM_RE.search(body):
            report.findings.append(
                Finding("simulation_overclaim", "warning", "把模拟观察写成了论文结论，需要区分证据边界。")
            )
    return report


def _clean_quote(text: str, limit: int = 420) -> str:
    value = re.sub(r"\s+", " ", (text or "").strip())
    return value[:limit]


def ensure_coverage(
    markdown: str,
    records: list[MaterialRecord],
    function: str,
) -> tuple[str, list[str]]:
    """Append evidence-bound citation paragraphs and missing reference filenames.

    This is the local equivalent of PaperCode's forced draft: the report remains
    honest (excerpts are quoted, not inferred), but it becomes structurally
    complete instead of falling back to a content-free placeholder.
    """
    text = (markdown or "").strip() + "\n"
    if REFERENCE_MARKER in text:
        body, _, refs = text.partition(REFERENCE_MARKER)
    else:
        body, refs = text, ""
    additions: list[str] = []
    used = set(citation_ids(body))
    require_all = function == "Survey"
    for index, record in enumerate(records, start=1):
        need = (require_all and index not in used) or (function == "Idea" and not used and index == 1)
        if need:
            quote = _clean_quote(record.abstract or record.text)
            if quote:
                paragraph = (
                    f"[{index}] 指定材料《{record.filename}》的正文摘录为：“{quote}”。"
                    "本段只转述指定材料的摘录，未作额外推断。"
                )
            else:
                paragraph = (
                    f"[{index}] 指定材料为《{record.filename}》；正文摘录无法提取，"
                    "本段只记录材料范围，不对其内容作事实断言。"
                )
            body = body.rstrip() + "\n\n" + paragraph + "\n"
            additions.append(f"inline:{record.filename}")

    ref_lines = [line for line in refs.splitlines() if line.strip()]
    for index, record in enumerate(records, start=1):
        if record.filename not in refs:
            ref_lines.append(f"[{index}] {record.filename}")
            additions.append(f"reference:{record.filename}")

    if not additions:
        return text, additions
    body = body.rstrip()
    refs_text = "\n".join(ref_lines).strip()
    rebuilt = body + "\n\n" + REFERENCE_MARKER + "\n"
    if refs_text:
        rebuilt += refs_text + "\n"
    return rebuilt, additions


def novelty_overlap(topic: str, records: list[MaterialRecord]) -> list[tuple[MaterialRecord, float]]:
    """PaperCode-style PriorWorkMap: term overlap between topic and each paper."""
    topic_terms = {term for term in terms(topic) if len(term) > 1}
    if not topic_terms:
        return [(record, 0.0) for record in records]
    scored: list[tuple[MaterialRecord, float]] = []
    for record in records:
        paper_terms = set(terms(record.title + " " + record.abstract))
        overlap = len(topic_terms & paper_terms) / max(1, len(topic_terms))
        scored.append((record, overlap))
    scored.sort(key=lambda item: -item[1])
    return scored
