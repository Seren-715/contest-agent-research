from __future__ import annotations

from pathlib import Path

import pytest

from core.paperlib import MaterialRecord, Paper, bm25_rank
from handlers.evidence import audit_report, ensure_coverage


def _record(name: str, text: str) -> MaterialRecord:
    return MaterialRecord(
        path=Path(name),
        filename=name,
        sha256="0" * 64,
        bytes=len(text),
        pages=1,
        title=name.replace(".pdf", ""),
        abstract=text[:300],
        text=text,
    )


def _survey(markdown_body: str, refs: str) -> str:
    padding = "以下内容按章节组织。" * 80
    return "# 综述\n\n" + padding + "\n\n" + markdown_body + "\n\n## 参考文献\n" + refs + "\n"


def test_audit_blocks_uncited_factual_sentence() -> None:
    records = [_record("a.pdf", "We propose a method and report accuracy 0.9.")]
    markdown = _survey(
        "## 引言与研究问题\n本文比较了指定方法 [1]。\n\n"
        "## 方法与技术脉络\n该方法的准确率是 0.9。\n\n"
        "## 数据与评价指标\n数据与指标见 [1]。\n\n"
        "## 结论、局限与冲突\n现有证据支持该观察 [1]。",
        "[1] a.pdf",
    )
    report = audit_report(markdown, records, "Survey")
    assert any(item.category == "unsupported_claim" for item in report.blockers)


def test_audit_accepts_fully_cited_survey() -> None:
    records = [
        _record("a.pdf", "Paper A reports a method with accuracy 0.9."),
        _record("b.pdf", "Paper B reports a retrieval baseline."),
    ]
    markdown = _survey(
        "## 引言与研究问题\n论文 A 报告了分类方法 [1]，论文 B 报告了检索基线 [2]。\n\n"
        "## 方法与技术脉络\nA 的方法准确率为 0.9 [1]，B 的方法使用检索基线 [2]。\n\n"
        "## 数据与评价指标\n两篇论文分别报告了各自的数据条件 [1][2]。\n\n"
        "## 结论、局限与冲突\n现有证据只支持各自条件下的观察 [1][2]。",
        "[1] a.pdf\n[2] b.pdf",
    )
    report = audit_report(markdown, records, "Survey")
    assert not report.blockers


def test_ensure_coverage_adds_missing_citation_and_reference() -> None:
    records = [
        _record("a.pdf", "Paper A reports a method with accuracy 0.9."),
        _record("b.pdf", "Paper B reports a dataset and a baseline."),
    ]
    markdown = _survey(
        "## 引言与研究问题\n论文 A 报告了分类方法 [1]。\n\n"
        "## 方法与技术脉络\n论文 A 的方法准确率为 0.9 [1]。\n\n"
        "## 数据与评价指标\n论文 A 报告了数据条件 [1]。\n\n"
        "## 结论、局限与冲突\n现有证据只支持各自条件下的观察 [1]。",
        "[1] a.pdf",
    )
    fixed, additions = ensure_coverage(markdown, records, "Survey")
    assert "[2]" in fixed
    assert "b.pdf" in fixed
    assert "inline:b.pdf" in additions
    assert "reference:b.pdf" in additions


def test_survey_rejects_duplicate_or_ambiguous_reference_entries() -> None:
    records = [_record("a.pdf", "A"), _record("b.pdf", "B")]
    body = "研究材料分别为 A [1] 和 B [2]。"
    for refs in ("[1] a.pdf\n[1] b.pdf\n[2] b.pdf", "[1] a.pdf b.pdf\n[2] b.pdf",
                 "[1] a.pdf\n[2] a.pdf"):
        markdown = _survey(body, refs)
        assert any(f.category == "missing_reference_entry" for f in audit_report(markdown, records, "Survey").blockers)
        fixed, _ = ensure_coverage(markdown, records, "Survey")
        assert any(f.category == "missing_reference_entry" for f in audit_report(fixed, records, "Survey").blockers)


def test_survey_accepts_consistent_renumbering() -> None:
    records = [_record("a.pdf", "A"), _record("b.pdf", "B")]
    markdown = _survey("研究材料分别为 A [2] 和 B [1]。", "[1] b.pdf\n[2] a.pdf")
    report = audit_report(markdown, records, "Survey")
    assert not any(f.category in ("missing_reference_entry", "missing_inline_citation") for f in report.blockers)
    fixed, additions = ensure_coverage(markdown, records, "Survey")
    assert not additions
    assert fixed == markdown


def test_survey_filename_matching_is_not_a_substring() -> None:
    records = [_record("a.pdf", "A"), _record("ba.pdf", "B")]
    markdown = _survey("研究材料分别为 A [1] 和 B [2]。", "[1] ba.pdf\n[2] ba.pdf")
    assert any(f.category == "missing_reference_entry" for f in audit_report(markdown, records, "Survey").blockers)
    markdown = _survey("研究材料分别为 A [1] 和 B [2]。", "[1] a.pdf\n[2] ba.pdf")
    assert not any(f.category == "missing_reference_entry" for f in audit_report(markdown, records, "Survey").blockers)


def test_survey_reference_entry_can_continue_on_next_line() -> None:
    records = [_record("a.pdf", "A")]
    markdown = _survey("材料为 A [1]。", "- [1] Paper title\n  a.pdf")
    assert not any(f.category == "missing_reference_entry" for f in audit_report(markdown, records, "Survey").blockers)


def test_bm25_rank_prefers_topically_relevant_paper() -> None:
    papers = [
        Paper(Path("urban.pdf"), "urban.pdf", "urban green space surface temperature remote sensing"),
        Paper(Path("quantum.pdf"), "quantum.pdf", "quantum chromodynamics lattice gauge theory"),
        Paper(Path("heat.pdf"), "heat.pdf", "neighborhood vegetation urban heat exposure"),
    ]
    ranked = bm25_rank("urban green space temperature", papers, top_k=3)
    assert ranked[0][0].filename == "urban.pdf"
    assert ranked[0][1] > ranked[-1][1]


@pytest.mark.parametrize("sentence", [
    "计划在五个随机种子上重复实验，以准确率和吞吐作为评价指标，并将失败率低于百分之一设为验收目标。",
    "本研究拟重复实验5次，报告准确率的均值与方差，并检查吞吐是否满足验收目标。",
    "假设新机制能够降低延迟，将通过对照实验验证该假设是否成立。",
    "若候选方案的失败率高于百分之一，则回退到原有路径并报告失败样本。",
    "计划比较候选方案与基线方法准确率，并在五个随机种子上评估差异是否稳定。",
    "计划实测候选方案吞吐并报告结果，以固定的数据切分评估方案稳定性。",
    "We plan to compare accuracy and throughput across five random seeds.",
])
def test_idea_future_design_is_not_treated_as_reported_fact(sentence) -> None:
    records = [_record("a.pdf", "Material describes a task.")]
    markdown = _survey(sentence + "\n材料仅说明任务范围 [1]。", "[1] a.pdf")
    report = audit_report(markdown, records, "Idea")
    assert not report.blockers


@pytest.mark.parametrize("sentence", [
    "该方法的准确率已经达到百分之九十，并在论文实验中优于对照方法。",
    "计划开展对照实验，因为该方法准确率为百分之九十而基线仍有改进空间。",
    "论文报告了候选方法吞吐提升的结果，计划用五个随机种子进行复核。",
    "计划运行五轮实验，同时已有论文表明该方法可以显著提升准确率。",
    "本文计划复现实验，因为作者报告了现有方法显著降低时延的结论。",
    "We plan to evaluate a mechanism that prior work reports improves accuracy.",
])
def test_idea_reported_or_mixed_claim_still_requires_citation(sentence) -> None:
    records = [_record("a.pdf", "Material describes a task.")]
    markdown = _survey(sentence + "\n材料仅说明任务范围 [1]。", "[1] a.pdf")
    report = audit_report(markdown, records, "Idea")
    assert any(item.category == "unsupported_claim" for item in report.blockers)


def test_survey_fact_gate_is_not_changed_by_idea_design_exception() -> None:
    # Survey citations are per paragraph: the uncited plan sentence stands alone.
    markdown = _survey("计划采用准确率和吞吐指标进行实验比较，并将验收阈值定义为百分之一。\n\n材料范围 [1]。", "[1] a.pdf")
    report = audit_report(markdown, [_record("a.pdf", "Source")], "Survey")
    assert any(item.category == "unsupported_claim" for item in report.blockers)


def test_idea_section_layout_is_quality_warning_not_schema_failure() -> None:
    report = audit_report(_survey("材料范围 [1]。", "[1] a.pdf"), [_record("a.pdf", "Source")], "Idea")
    assert not report.blockers
    assert any(item.category == "missing_section" for item in report.warnings)


@pytest.mark.parametrize("body,refs,category", [
    ("没有正文引用。", "[1] a.pdf", "missing_inline_citation"),
    ("材料范围 [9]。", "[1] a.pdf", "invalid_citation"),
    ("材料范围 [1]。", "[1] other.pdf", "missing_reference_entry"),
])
def test_idea_citation_requirements_remain_blocking(body, refs, category) -> None:
    report = audit_report(_survey(body, refs), [_record("a.pdf", "Source")], "Idea")
    assert any(item.category == category for item in report.blockers)


def test_survey_citation_covers_its_paragraph_but_not_the_next() -> None:
    record = _record("a.pdf", "Source")
    cited = _survey("方法在基准上提升了百分之五的准确率。第二句仍属同一段 [1]。", "[1] a.pdf")
    assert not [item for item in audit_report(cited, [record], "Survey").blockers if item.category == "unsupported_claim"]
    uncited = _survey("材料范围 [1]。\n\n方法在基准上提升了百分之五的准确率。", "[1] a.pdf")
    assert any(item.category == "unsupported_claim" for item in audit_report(uncited, [record], "Survey").blockers)
