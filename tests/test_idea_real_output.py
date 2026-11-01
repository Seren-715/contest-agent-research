from __future__ import annotations

from pathlib import Path

import pytest

from core.paperlib import MaterialRecord
from handlers.evidence import audit_report


def _record() -> MaterialRecord:
    return MaterialRecord(Path("smoke1.pdf"), "smoke1.pdf", "0" * 64, 100, 1,
                          "smoke1", "Urban green space, surface temperature and remote sensing.",
                          "Urban green space, surface temperature and remote sensing.")


def _report(sentence: str) -> str:
    return ("# Idea\n" + "本文区分材料范围和新方案的适用边界。" * 60 + "\n"
            + sentence + "\n材料的主题范围可核对 [1]。\n## 参考文献\n[1] smoke1.pdf")


@pytest.mark.parametrize("filename", ["draft-1.md", "draft-2.md"])
def test_actual_model_report_is_not_rejected_for_design_metrics(filename) -> None:
    # Replays an actual retained model response verbatim; no credentials or
    # remote requests. The developer diagnostic bundle is intentionally local.
    draft = Path(__file__).parents[1] / "dist" / "deepseek-debug-20261030" / filename
    if not draft.is_file():
        pytest.skip("local real-output diagnostic bundle is unavailable")
    markdown = draft.read_text(encoding="utf-8")
    report = audit_report(markdown, [_record()], "Idea")
    assert not report.blockers


@pytest.mark.parametrize("sentence", [
    "实验拟设置以下基线（均为待执行计划，不是已有实验）：B0仅背景变量；B1为背景加绿地数量。",
    "可证伪预测拟为：P1，配置特征的增量不低于百分之二，否则拒绝该假设。",
    "指标包括RMSE、MAE和吞吐，主指标用于比较所设定的候选方案。",
    "预先设定的验收目标为：残差空间自相关下降至少百分之三十。",
    "失败边界为：指标增量低于阈值、置信区间跨零，或城市留出性能崩溃。",
    "核心想法是建立增量验证框架，观察预测误差是否稳定下降。",
    "RMSE and throughput are evaluation metrics for the planned comparison.",
])
def test_design_metric_keywords_are_uncertain_warnings_not_citation_failures(sentence) -> None:
    report = audit_report(_report(sentence), [_record()], "Idea")
    assert not report.blockers
    assert any(finding.category == "claim_semantics_unverified" for finding in report.warnings)


@pytest.mark.parametrize("sentence", [
    "本次实验的准确率为95%，比基线提升10个百分点。",
    "本方案在测试集上取得了95%的准确率，显著优于对照组。",
    "论文在公开测试集上获得了95%的准确率。",
    "论文报告了候选方法的准确率达到百分之九十，计划进一步设置对照实验。",
    "可证伪预测拟设为下降百分之三十，因为已有论文表明该方法能够降低误差。",
    "本研究计划重复实验，因为作者报告了现有方法显著提升准确率的结果。",
    "该方法的准确率已经达到百分之九十，并在实验中优于基线。",
    "The authors report improved accuracy; we plan to repeat the experiment.",
])
def test_existing_or_mixed_attributed_result_still_requires_source(sentence) -> None:
    report = audit_report(_report(sentence), [_record()], "Idea")
    assert any(finding.category == "unsupported_claim" for finding in report.blockers)


def test_illegal_citation_and_genuinely_uncited_survey_are_still_blocked() -> None:
    report = audit_report(_report("实验拟设置候选模型 [9]。"), [_record()], "Idea")
    assert any(finding.category == "invalid_citation" for finding in report.blockers)
    # Survey citations cover their own paragraph only: make the claim a paragraph of its own.
    survey = audit_report(_report("\n指标包括准确率和吞吐，主指标用于比较所设定的候选方案。\n"), [_record()], "Survey")
    assert any(finding.category == "unsupported_claim" for finding in survey.blockers)
