import pytest
from core.schemas import validate_answer


def report():
    return {"case_id": "E-01", "path": "data/expert/E-01-灵感Idea报告-team.md",
            "markdown": "文" * 900 + "\n## 参考文献\n[1] paper.pdf"}


@pytest.mark.parametrize("path", ["elsewhere/report.md", "data/expert/E-02-灵感Idea报告-team.md",
                                  "data/expert/E-01.md", "data/expert/sub/E-01-灵感Idea报告-team.md"])
def test_wrong_report_path_is_rejected(path):
    data = report()
    data["path"] = path
    with pytest.raises(ValueError):
        validate_answer("Idea", data)


def test_requested_function_must_agree_with_case():
    with pytest.raises(ValueError):
        validate_answer("Survey", report())


def test_three_digit_case_id_is_supported():
    data = report()
    data.update(case_id="E-100", path="data/expert/E-100-灵感Idea报告-team.md")
    validate_answer("Idea", data)


def test_conservative_upper_limit_includes_heading_symbols():
    data = report()
    data["markdown"] = "# " + "题" * 4000 + "\n" + data["markdown"]
    with pytest.raises(ValueError, match="maximum"):
        validate_answer("Idea", data)
