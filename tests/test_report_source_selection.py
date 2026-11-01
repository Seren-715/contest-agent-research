import json
from pathlib import Path

import pytest

from core.budget import Deadline
from core.materials import select_report_materials
from core.paperlib import MaterialResolutionError
from handlers.base import HandlerError
from handlers.b_retrieval import RetrievalHandler
from handlers.c_survey import SurveyHandler
from handlers.e_idea import IdeaHandler
from tests.conftest import FakeClient
from tests.test_handlers_papers import _markdown, paper_settings


def choice(paths):
    return json.dumps({"papers": [{"filename": path.name, "reason": "原文比较城市绿地覆盖与地表温度，符合任务主题。"}
                                  for path in paths]}, ensure_ascii=False)


def test_concrete_report_materials_never_need_model_selection(paper_settings):
    settings, paths = paper_settings
    client = FakeClient([])
    result = select_report_materials({"case_id": "C-01", "materials": [str(p) for p in paths]},
                                     settings, client, Deadline(300))
    assert result == paths and client.calls == []


def test_invalid_case_identity_is_rejected_before_concrete_source_processing(paper_settings):
    settings, paths = paper_settings
    with pytest.raises(HandlerError, match="case identity"):
        select_report_materials({"case_id": "../outside", "materials": [str(paths[0])]},
                                settings, FakeClient([]), Deadline(300))


def test_described_scope_requires_evidence_selection_not_whole_library(paper_settings):
    settings, paths = paper_settings
    selected = paths[:2]
    client = FakeClient([choice(selected)])
    result = select_report_materials({"case_id": "C-01", "input": "urban temperature",
                                     "materials": ["本地论文文件夹内指定的城市气候 PDF"]},
                                    settings, client, Deadline(300))
    assert result == selected
    receipt = json.loads((settings.team_dir / "work/materials/C-01.json").read_text(encoding="utf-8"))
    assert receipt["candidates"] == len(paths)
    assert [item["filename"] for item in receipt["selected"]] == [p.name for p in selected]
    assert all(len(item["sha256"]) == 64 for item in receipt["selected"])


def test_description_whitespace_does_not_become_a_missing_file(paper_settings):
    settings, paths = paper_settings
    result = select_report_materials({"case_id": "E-01", "input": "urban temperature",
                                     "materials": ["  本题指定的本地论文 PDF  "]}, settings,
                                    FakeClient([choice(paths[:1])]), Deadline(300))
    assert result == paths[:1]


def test_explicit_missing_is_not_reinterpreted_as_description(paper_settings):
    settings, _ = paper_settings
    client = FakeClient([])
    with pytest.raises(MaterialResolutionError):
        select_report_materials({"case_id": "C-01", "materials": ["missing.pdf"]}, settings, client, Deadline(300))
    assert not client.calls


def test_no_relevant_selection_does_not_fall_back_to_other_papers(paper_settings):
    settings, _ = paper_settings
    with pytest.raises(HandlerError, match="no source matched"):
        select_report_materials({"case_id": "C-01", "input": "quantum field theory",
                                 "materials": ["本题指定的本地论文 PDF"]},
                                settings, FakeClient(['{"papers":[]}']), Deadline(300))


def test_explicit_sources_plus_description_preserve_exact_scope(paper_settings):
    settings, paths = paper_settings
    client = FakeClient([])
    result = select_report_materials({"case_id": "E-01", "materials": [str(paths[0]), "本题指定的论文 PDF"]},
                                     settings, client, Deadline(300))
    assert result == [paths[0]] and not client.calls


@pytest.mark.parametrize("handler,case_id,description", [
    (SurveyHandler, "C-99", "本地论文文件夹内指定的城市气候 PDF"),
    (IdeaHandler, "E-99", "本题指定的城市气候与步行规划论文 PDF"),
])
def test_descriptive_reports_reach_generation_and_save_matching_files(paper_settings, handler, case_id, description):
    settings, paths = paper_settings
    selected = paths[:3]
    client = FakeClient([choice(selected), _markdown([p.name for p in selected])])
    payload = handler().handle({"case_id": case_id, "input": "比较城市绿地温度研究并提出步行规划研究方案。",
                                "materials": [description]}, settings, client)
    assert len(client.calls) == 2
    assert (settings.workspace_root / payload["path"]).read_text(encoding="utf-8") == payload["markdown"]
    assert paths[-1].name not in payload["markdown"]


def test_B_natural_language_library_scope_reaches_reranking(paper_settings):
    settings, paths = paper_settings
    client = FakeClient([choice(paths[:2])])
    payload = RetrievalHandler().handle({"case_id": "B-99", "input": "urban green temperature",
                                         "materials": ["本地论文文件夹内的 PDF"]}, settings, client)
    assert len(client.calls) == 1 and len(payload["papers"]) == 2


def test_auxiliary_note_does_not_expand_or_block_explicit_pdf_scope(paper_settings):
    settings, paths = paper_settings
    note = paths[0].parent / "conditions.csv"
    note.write_text("season,summer\n", encoding="utf-8")
    client = FakeClient([])
    result = select_report_materials({"case_id": "E-01", "materials": [str(paths[0]), str(note)]},
                                     settings, client, Deadline(300))
    assert result == [paths[0]] and not client.calls


def test_later_selection_batch_failure_keeps_prior_evidence(settings, tmp_path, monkeypatch):
    from core.llm import ModelError
    from core.paperlib import MaterialRecord
    records = [MaterialRecord(tmp_path / f"p{i:02d}.pdf", f"p{i:02d}.pdf", "a" * 64, 100, 1,
                              "Adaptive decoding", "Inference", "Adaptive decoding improves inference.")
               for i in range(50)]
    monkeypatch.setattr("core.materials.resolve_materials", lambda *args, **kwargs: [r.path for r in records])
    monkeypatch.setattr("core.materials.index_materials", lambda *args, **kwargs: records)
    monkeypatch.setenv("CONTEST_SELECT_BATCH", "40")
    class TwoBatches:
        calls = 0
        def complete(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise ModelError("upstream failed")
            return choice([records[0].path])
    client = TwoBatches()
    paths = select_report_materials({"case_id": "C-01", "input": "adaptive decoding",
                                     "materials": ["本地论文文件夹内的 PDF"]}, settings, client, Deadline(300))
    assert paths == [records[0].path] and client.calls == 2
    receipt = json.loads((settings.team_dir / "work/materials/C-01.json").read_text(encoding="utf-8"))
    assert receipt["processed_candidates"] == 40
    assert receipt["candidate_coverage_complete"] is False


def _library(tmp_path, monkeypatch, designated=5, others=100):
    from core.paperlib import MaterialRecord
    records = []
    for i in range(others):
        path = tmp_path / "论文库" / "misc" / f"a{i:03d}.pdf"
        records.append(MaterialRecord(path, path.name, "a" * 64, 100, 1, "Botany", "Forest",
                                      "Botany observations of tropical forests."))
    for i in range(designated):
        path = tmp_path / "论文库" / "battery_recycling" / f"z{i}.pdf"
        records.append(MaterialRecord(path, path.name, "b" * 64, 100, 1, "Battery recycling", "Hydrometallurgy",
                                      "Hydrometallurgical recycling of lithium-ion batteries."))
    monkeypatch.setattr("core.materials.resolve_materials", lambda *args, **kwargs: [r.path for r in records])
    monkeypatch.setattr("core.materials.index_materials", lambda *args, **kwargs: records)
    return records


def test_selection_shows_every_paper_with_its_folder_and_the_task_title(settings, tmp_path, monkeypatch):
    records = _library(tmp_path, monkeypatch)
    designated = [r for r in records if r.filename.startswith("z")]

    class Picker:
        prompts = []

        def complete(self, messages, **kwargs):
            self.prompts.append(messages[-1]["content"])
            return choice([r.path for r in designated])

    client = Picker()
    paths = select_report_materials(
        {"case_id": "C-01", "title": "电动汽车电池回收：回收工艺与生命周期评估综述",
         "input": "阅读本题指定论文，撰写综述，列出至少 5 篇实际引用的指定论文。",
         "materials": ["本地论文文件夹内指定的电池回收 PDF"]}, settings, client, Deadline(900))
    assert paths == [r.path for r in designated]
    prompt = client.prompts[0]
    assert len(client.prompts) == 1
    # Chinese task, English papers: no BM25 cut-off may drop the designated, alphabetically last papers.
    assert all(r.filename in prompt for r in records)
    assert "battery_recycling/z0.pdf" in prompt
    assert "电动汽车电池回收" in prompt and "通常指定 5 篇" in prompt


def test_selection_keeps_at_most_two_more_than_the_designated_count(settings, tmp_path, monkeypatch):
    records = _library(tmp_path, monkeypatch, designated=10)
    picked = [r.path for r in records if r.filename.startswith("z")]
    paths = select_report_materials({"case_id": "C-01", "input": "综述，至少 5 篇指定论文",
                                     "materials": ["本地论文文件夹内指定的电池回收 PDF"]},
                                    settings, FakeClient([choice(picked)]), Deadline(900))
    assert paths == picked[:7]


@pytest.mark.parametrize("question,expected", [
    ({"input": "列出至少 5 篇实际引用的指定论文"}, 5),
    ({"input": "至少6篇"}, 6),
    ({"input": "比较 4 篇指定论文"}, 4),
    ({"input": "撰写综述"}, None),
    ({"input": "撰写综述", "constraints": ["参考文献至少十篇"]}, 10),
    ({"input": "比较十二篇指定论文"}, 12),
    ({"input": "至少两篇"}, 2),
    ({"input": "至少 5 篇", "constraints": ["至少引用十篇指定论文"]}, 10),
    ({"input": "Cite at least 8 papers"}, 8),
])
def test_required_count_is_read_from_the_task(question, expected):
    from core.materials import _required_count
    assert _required_count(question) == expected


def test_stated_requirement_of_ten_keeps_all_ten_selected_papers(settings, tmp_path, monkeypatch):
    records = _library(tmp_path, monkeypatch, designated=10)
    picked = [r.path for r in records if r.filename.startswith("z")]

    class Picker:
        prompts = []

        def complete(self, messages, **kwargs):
            self.prompts.append(messages[-1]["content"])
            return choice(picked)

    client = Picker()
    paths = select_report_materials({"case_id": "C-01", "input": "撰写电池回收综述",
                                     "constraints": ["参考文献至少十篇指定论文"],
                                     "materials": ["本地论文文件夹内指定的电池回收 PDF"]},
                                    settings, client, Deadline(900))
    assert paths == picked
    assert "通常指定 10 篇" in client.prompts[0]


def test_unstated_requirement_never_trims_the_model_selection(settings, tmp_path, monkeypatch):
    records = _library(tmp_path, monkeypatch, designated=9)
    picked = [r.path for r in records if r.filename.startswith("z")]
    paths = select_report_materials({"case_id": "C-01", "input": "撰写电池回收综述",
                                     "materials": ["本地论文文件夹内指定的电池回收 PDF"]},
                                    settings, FakeClient([choice(picked)]), Deadline(900))
    assert paths == picked
