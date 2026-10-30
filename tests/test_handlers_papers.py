from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.config import Settings
from core.schemas import validate_answer
from handlers.b_retrieval import RetrievalHandler
from handlers.c_survey import SurveyHandler
from handlers.e_idea import IdeaHandler
from tests.conftest import FakeClient

PAPERS = [
    (
        "urban_green_2024.pdf",
        "Urban green space and surface temperature: a remote sensing study",
        "We compare green space coverage and land surface temperature across districts "
        "using remote sensing. Higher vegetation coverage is associated with lower summer "
        "surface temperature, with differences between urban core and suburban areas.",
    ),
    (
        "urban_heat_2023.pdf",
        "Neighborhood vegetation and urban heat",
        "This paper measures vegetation fraction and heat exposure in residential "
        "neighborhoods. It reports that tree canopy reduces daytime heat, while sparse "
        "grass cover has a weaker effect.",
    ),
    (
        "remote_sensing_2022.pdf",
        "Remote sensing of urban green infrastructure",
        "We evaluate vegetation indices from satellite imagery and their relationship "
        "with surface temperature. The analysis separates parks, street trees and "
        "private gardens, and discusses seasonal variation.",
    ),
    (
        "heat_equity_2024.pdf",
        "Heat equity and urban greening",
        "The study links green space distribution with socio-economic indicators and "
        "surface temperature. It finds that disadvantaged neighborhoods often have less "
        "canopy cover and higher heat exposure.",
    ),
    (
        "green_design_2021.pdf",
        "Designing urban green space for cooling",
        "We review cooling mechanisms of urban green space, including shade, "
        "evapotranspiration and ventilation. The paper compares design configurations "
        "and measurement methods.",
    ),
]


def _make_pdf(path: Path, title: str, body: str) -> None:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 6, title + "\n\n" + body)
    pdf.output(str(path))


@pytest.fixture
def paper_settings(tmp_path: Path) -> tuple[Settings, list[Path]]:
    data = tmp_path / "data"
    library = data / "论文库"
    library.mkdir(parents=True)
    paths: list[Path] = []
    for name, title, body in PAPERS:
        path = library / name
        _make_pdf(path, title, body)
        paths.append(path)

    team = "test-team"
    settings = Settings(
        team=team,
        workspace_root=tmp_path,
        data_dir=data,
        team_dir=data / team,
        expert_dir=data / "expert",
        figure_dir=data / "figure" / team,
        llm_base_url=None,
        llm_api_key=None,
        llm_model=None,
        mock_llm=False,
        request_timeout=30.0,
        case_budget=180.0,
        d_client=tmp_path / "run_experiment.py",
        d_materials_root=tmp_path / "experiments",
    )
    settings.ensure_dirs()
    return settings, paths


def _markdown(names: list[str]) -> str:
    body = "# 综述\n\n## 引言与研究问题\n" + ("本文比较指定论文的方法、数据、结论和适用条件，区分证据、研究边界与未核验推断 [1]。" * 12)
    body += "\n\n## 动机与已有方法局限\n已有方法在数据条件上存在局限 [1]。"
    body += "\n\n## 核心 Idea 与技术路线\n本文提出一个可验证的技术路线 [1]。"
    body += "\n\n## 方法与技术脉络\n"
    for index, _ in enumerate(names, start=1):
        body += f"论文 [{index}] 在遥感数据上比较了绿地覆盖率与地表温度 [{index}]。"
    body += "\n\n## 基线、数据集与实验设计\n基线与数据集按论文条件设置 [1]。"
    body += "\n\n## 消融与量化指标\n消融与量化指标需要同时报告 [1]。"
    body += "\n\n## 数据与评价指标\n各论文的数据与指标见正文 [1]。"
    body += "\n\n## 结论、局限与冲突\n现有证据只支持各自条件下的观察 [1]。"
    body += "\n\n## 多文献互补关系\n各论文的证据互补 [1]。"
    body += "\n\n## 复现瓶颈反思\n复现瓶颈需要逐项核验 [1]。"
    body += "\n\n## 可行性与失败边界\n可行性取决于数据条件 [1]。"
    body += "\n\n## 创新论证与可证伪方式\n创新点需要可证伪的判定，且与已有方法不同 [1]。"
    references = "\n\n## 参考文献\n" + "\n".join(
        f"[{index}] {name}" for index, name in enumerate(names, start=1)
    )
    return body + references


def test_b_retrieval_only_returns_real_filenames(paper_settings) -> None:
    settings, paths = paper_settings
    client = FakeClient(
        [
            json.dumps({"keywords": ["urban", "green", "surface", "temperature", "remote", "sensing"]}, ensure_ascii=False),
            json.dumps(
                {
                    "papers": [
                        {"filename": paths[0].name, "reason": "直接研究绿地与地表温度。"},
                        {"filename": "ghost_paper.pdf", "reason": "不存在的文件。"},
                    ]
                },
                ensure_ascii=False,
            ),
        ]
    )
    question = {
        "case_id": "B-01",
        "function": "Literature_Retrieval",
        "input": "城市绿地 地表温度 遥感",
        "materials": [str(path) for path in paths],
        "constraints": [],
    }
    result = RetrievalHandler().handle(question, settings, client)
    assert [item["filename"] for item in result["papers"]] == [paths[0].name]
    validate_answer("Literature_Retrieval", result)


def test_c_survey_writes_matching_markdown(paper_settings) -> None:
    settings, paths = paper_settings
    names = [path.name for path in paths]
    markdown = _markdown(names)
    client = FakeClient([markdown])
    question = {
        "case_id": "C-01",
        "function": "Survey",
        "input": "比较城市绿地与地表温度研究。",
        "materials": [str(path) for path in paths],
        "constraints": [],
    }
    result = SurveyHandler().handle(question, settings, client)
    assert result["path"] == "data/expert/C-01-综述-test-team.md"
    saved = (settings.workspace_root / result["path"]).read_text(encoding="utf-8")
    assert saved == result["markdown"]
    validate_answer("Survey", result)


def test_e_idea_writes_matching_markdown(paper_settings) -> None:
    settings, paths = paper_settings
    names = [path.name for path in paths]
    markdown = _markdown(names)
    client = FakeClient([markdown])
    question = {
        "case_id": "E-01",
        "function": "Idea",
        "input": "提出一个降低步行热暴露的路线规划方案。",
        "materials": [str(path) for path in paths],
        "constraints": [],
    }
    result = IdeaHandler().handle(question, settings, client)
    assert result["path"] == "data/expert/E-01-灵感Idea报告-test-team.md"
    saved = (settings.workspace_root / result["path"]).read_text(encoding="utf-8")
    assert saved == result["markdown"]
    validate_answer("Idea", result)


def test_e_idea_accepts_partial_citations(paper_settings) -> None:
    settings, paths = paper_settings
    name = paths[0].name
    markdown = (
        "# Idea\n\n"
        "## 研究问题与边界\n"
        + ("正文" * 330)
        + " [1]。\n\n"
        "## 动机与已有方法局限\n已有方法与本文方案不同 [1]。\n\n"
        "## 核心 Idea 与技术路线\n技术路线围绕可验证机制展开 [1]。\n\n"
        "## 基线、数据集与实验设计\n基线与数据集按论文条件设置 [1]。\n\n"
        "## 消融与量化指标\n消融与指标需要同时报告 [1]。\n\n"
        "## 多文献互补关系\n指定论文的证据互补 [1]。\n\n"
        "## 复现瓶颈反思\n复现瓶颈需要逐项核验 [1]。\n\n"
        "## 可行性与失败边界\n可行性取决于数据条件 [1]。\n\n"
        "## 创新论证与可证伪方式\n创新点需要可证伪的判定 [1]。\n\n"
        f"## 参考文献\n[1] {name}\n"
    )
    client = FakeClient([markdown])
    question = {
        "case_id": "E-02",
        "function": "Idea",
        "input": "提出一个可验证的研究方案。",
        "materials": [str(path) for path in paths],
        "constraints": [],
    }
    result = IdeaHandler().handle(question, settings, client)
    assert result["path"].startswith("data/expert/E-02-灵感Idea报告-")
    validate_answer("Idea", result)
