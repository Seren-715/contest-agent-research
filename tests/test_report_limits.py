from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from core.paperlib import MaterialRecord
from core.schemas import report_body, report_limit_errors, validate_answer
from handlers.evidence import audit_report
from handlers.reporting import finalize
from handlers.base import HandlerError
from tests.conftest import FakeClient


def _report(body: str, refs: str = "[1] a.pdf") -> str:
    return "# 报告\n" + body + "\n## 参考文献\n" + refs


def _payload(function: str, markdown: str) -> dict:
    case_id = "C-01" if function == "Survey" else "E-01"
    tag = "综述" if function == "Survey" else "灵感Idea报告"
    return {"case_id": case_id, "path": f"data/expert/{case_id}-{tag}-team.md", "markdown": markdown}


@pytest.mark.parametrize("function,maximum", [("Survey", 5000), ("Idea", 4500)])
def test_report_upper_limit_boundaries(function, maximum) -> None:
    valid = "文" * (maximum - 1) + "\n## 参考文献\n[1] a.pdf"
    assert len(report_body(valid)) == maximum
    validate_answer(function, _payload(function, valid))
    with pytest.raises(ValidationError, match="maximum"):
        validate_answer(function, _payload(function, "文" * maximum + "\n## 参考文献\n[1] a.pdf"))


def test_reference_text_and_heading_lines_do_not_count_as_body() -> None:
    markdown = _report("文" * 799 + "\n## 标题", refs="参考资料" * 2000)
    assert len(report_body(markdown)) == 800
    validate_answer("Survey", _payload("Survey", markdown))
    short = _report("文" * 798, refs="参考资料" * 2000)
    with pytest.raises(ValidationError, match="at least 800"):
        validate_answer("Survey", _payload("Survey", short))


def test_body_spaces_punctuation_and_newlines_are_counted() -> None:
    markdown = "# 标题\n " + "文" * 4500 + "，\n\n## 参考文献\n[1] a.pdf"
    assert len(report_body(markdown)) == 4504
    with pytest.raises(ValidationError, match="maximum"):
        validate_answer("Idea", _payload("Idea", markdown))


def test_survey_compound_numeric_citations_count_as_one_group() -> None:
    valid = _report("正文" * 400 + "[1,2]" * 40, refs="[1] a.pdf\n[2] b.pdf\n" + "[9]" * 100)
    validate_answer("Survey", _payload("Survey", valid))
    with pytest.raises(ValidationError, match="41 citation groups"):
        validate_answer("Survey", _payload("Survey", valid.replace("## 参考文献", "[1]\n## 参考文献")))


def _record() -> MaterialRecord:
    return MaterialRecord(Path("a.pdf"), "a.pdf", "0" * 64, 30, 1, "研究", "原文摘录", "原文摘录")


def _valid_draft() -> str:
    sections = [
        "研究问题与边界", "动机与已有方法局限", "核心 Idea 与技术路线",
        "基线、数据集与实验设计", "消融与量化指标", "多文献互补关系",
        "复现瓶颈反思", "可行性与失败边界", "创新论证与可证伪方式",
    ]
    body = "\n".join(f"## {section}\n本文讨论材料的适用条件与研究边界。" * 1 for section in sections)
    body += "\n本文整理给定材料中的研究边界。" * 50 + "[1]。"
    return _report(body)


def test_audit_and_final_save_reject_over_limit_without_truncating(settings) -> None:
    settings.ensure_dirs()
    markdown = _valid_draft().replace("## 参考文献", "长" * 5000 + "\n## 参考文献")
    report = audit_report(markdown, [_record()], "Survey")
    assert any(item.category == "report_limits" for item in report.blockers)
    with pytest.raises(HandlerError, match="maximum"):
        finalize(settings, "Survey", "C-01", markdown, [Path("a.pdf")], records=[_record()])
    assert not list(settings.expert_dir.glob("*.md"))


def test_idea_does_not_programmatically_append_missing_evidence(settings) -> None:
    settings.ensure_dirs()
    markdown = _valid_draft().replace("[1]。", "。")
    with pytest.raises(HandlerError, match="引用"):
        finalize(settings, "Idea", "E-01", markdown, [Path("a.pdf")], records=[_record()])
    assert not list(settings.expert_dir.glob("*.md"))


@pytest.mark.parametrize("function,module_name,handler_name", [
    ("Survey", "handlers.c_survey", "SurveyHandler"),
    ("Idea", "handlers.e_idea", "IdeaHandler"),
])
def test_over_limit_draft_requests_complete_model_repair(settings, monkeypatch, function, module_name, handler_name) -> None:
    import importlib
    module = importlib.import_module(module_name)
    settings.ensure_dirs()
    monkeypatch.setattr(module, "resolve_materials", lambda *args: [Path("a.pdf")])
    monkeypatch.setattr(module, "materials_bundle", lambda *args, **kwargs: ("[1] a.pdf 原文摘录", [_record()]))
    valid = _valid_draft()
    assert not report_limit_errors(valid, function)
    oversized = valid.replace("## 参考文献", "长" * 5000 + "\n## 参考文献")
    client = FakeClient([oversized, valid])
    case_id = "C-01" if function == "Survey" else "E-01"
    result = getattr(module, handler_name)().handle(
        {"case_id": case_id, "function": function, "input": "研究问题", "materials": ["a.pdf"]},
        settings,
        client,
    )
    assert len(client.calls) == 2
    assert "maximum" in client.calls[1][-1]["content"]
    assert "完整修订版" in client.calls[1][-1]["content"]
    if function == "Idea":
        assert [message["role"] for message in client.calls[1]] == ["system", "user", "assistant", "user"]
        assert client.calls[1][:2] == client.calls[0]
        assert client.calls[1][2]["content"] == oversized
    assert result["markdown"] == valid.strip() + "\n"
    assert (settings.workspace_root / result["path"]).read_text(encoding="utf-8") == result["markdown"]


def test_idea_future_measurement_plan_saves_without_spurious_repair(settings, monkeypatch) -> None:
    import handlers.e_idea as module
    settings.ensure_dirs()
    monkeypatch.setattr(module, "resolve_materials", lambda *args: [Path("a.pdf")])
    monkeypatch.setattr(module, "materials_bundle", lambda *args, **kwargs: ("[1] a.pdf 原文摘录", [_record()]))
    plan = "计划在五个随机种子上重复实验，以准确率和吞吐作为评价指标，并将失败率低于百分之一设为验收目标。"
    markdown = _valid_draft().replace("## 参考文献", plan + "\n## 参考文献")
    client = FakeClient([markdown])
    result = module.IdeaHandler().handle(
        {"case_id": "E-01", "function": "Idea", "input": "研究问题", "materials": ["a.pdf"]},
        settings, client,
    )
    assert len(client.calls) == 1
    assert plan in result["markdown"]
    assert (settings.workspace_root / result["path"]).read_text(encoding="utf-8") == result["markdown"]

def test_compound_citation_counts_one_group_but_checks_every_source():
    from handlers.evidence import build_ledger, citation_ids
    from core.schemas import NUMERIC_CITATION_GROUP

    assert citation_ids("[1,2][3, p. 9]") == [1, 2, 3]
    assert len(NUMERIC_CITATION_GROUP.findall("[1,2][3, p. 9]")) == 2
    ledger = build_ledger("结果提升了10%[1,2]。", record_count=1)
    assert ledger and not ledger[0].supported

