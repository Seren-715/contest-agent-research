from __future__ import annotations

from pathlib import Path

import pytest

from core.paperlib import MaterialRecord, sentence_split
from handlers.evidence import audit_report, build_ledger


def _record(name: str) -> MaterialRecord:
    return MaterialRecord(Path(name), name, "0" * 64, 20, 3, "Title", "Source", "Source")


@pytest.mark.parametrize("sentence", [
    "论文报告了分类准确率提升 [1, p. 3]。",
    "论文报告了分类准确率提升 [1,p.3]。",
    "论文报告了分类准确率提升。[1]",
    "论文报告了分类准确率提升。[1, p. 3]",
    "论文报告了分类准确率提升！[1,2]",
    "The paper reports improved accuracy [1, p. 3].",
    "The paper reports improved accuracy. [1, p. 3]",
    "The paper reports improved accuracy.[1]",
    "The paper reports improved accuracy? [1,2]",
])
def test_valid_citation_formats_remain_bound_to_their_claim(sentence) -> None:
    ledger = build_ledger(sentence, 2, "Idea")
    assert len(ledger) == 1
    assert ledger[0].supported
    assert ledger[0].citations


@pytest.mark.parametrize("function", ["Survey", "Idea"])
def test_page_and_post_punctuation_citations_pass_complete_report_audit(function) -> None:
    markdown = ("# 报告\n" + "本文讨论适用条件与研究边界。" * 60
                + "\n论文报告了分类准确率提升 [1, p. 3]。"
                + "\n另一论文报告了方法性能改善。[2]\n## 参考文献\n[1] a.pdf\n[2] b.pdf")
    assert not audit_report(markdown, [_record("a.pdf"), _record("b.pdf")], function).blockers


@pytest.mark.parametrize("text", [
    "论文报告了分类准确率提升。另一论文报告了吞吐改善 [1]。",
    "The paper reports improved accuracy. Another paper reports improved throughput [1].",
    "论文报告了分类准确率提升。\n[1] 另一段的论文报告了吞吐改善。",
])
def test_later_sentence_citation_does_not_cover_a_truly_uncited_claim(text) -> None:
    ledger = build_ledger(text, 1, "Idea")
    assert any(not claim.supported for claim in ledger)


def test_post_sentence_citation_does_not_cover_following_uncited_sentence() -> None:
    ledger = build_ledger("论文报告了分类准确率提升。[1]另一论文报告了吞吐改善。", 1, "Idea")
    assert len(ledger) == 2
    assert ledger[0].supported
    assert not ledger[1].supported


@pytest.mark.parametrize("citation", ["[9]", "[9, p. 3]", "[1,9]"])
def test_invalid_reference_numbers_remain_blocking(citation) -> None:
    markdown = ("# 报告\n" + "本文讨论适用条件与研究边界。" * 60
                + "\n论文报告了分类准确率提升。" + citation
                + "\n## 参考文献\n[1] a.pdf")
    report = audit_report(markdown, [_record("a.pdf")], "Idea")
    assert any(item.category == "invalid_citation" for item in report.blockers)


def test_page_reference_does_not_split_and_decimal_values_are_preserved() -> None:
    assert sentence_split("The paper reports accuracy 0.9 [1, p. 3].") == [
        "The paper reports accuracy 0.9 [1, p. 3]."
    ]
