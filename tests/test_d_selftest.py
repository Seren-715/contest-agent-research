from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

import d_adapter.pipeline as pipeline_module
import core.solve as solve_module
from d_adapter import selftest


# Fails on the current round's sample request with a real exception: the only
# kind of failure a generic (task-agnostic) self-test may judge.
BAD_SOURCE = """
def run_experiment(request):
    return {"baseline": request["data"]["field_this_task_does_not_have"]}
"""

GOOD_SOURCE = """
import re


def run_experiment(request):
    data = request["data"]
    n = len(data["texts"])
    labels = sorted({row["label"] for row in data["train"]})
    vocabulary = sorted({
        token
        for row in data["train"]
        for token in re.findall(r"[a-z0-9]+", row["text"].lower())
    })
    baseline = [labels[0]] * n
    return {
        "baseline": baseline,
        "candidate": list(baseline),
        "details": {
            "classes": labels,
            "vocabulary": vocabulary,
            "idf": [1.0] * len(vocabulary),
            "weights": [[0.0] * (len(vocabulary) + 1) for _ in labels],
        },
    }
"""


class ScriptedClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete_raw(self, messages, **kwargs):
        self.calls += 1
        code = BAD_SOURCE if self.calls == 1 else GOOD_SOURCE
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "```python\n" + code + "\n```"},
                }
            ]
        }


class TruncatedThenGoodClient(ScriptedClient):
    def complete_raw(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": "```python\n" + BAD_SOURCE},
                    }
                ]
            }
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "```python\n" + GOOD_SOURCE + "\n```"},
                }
            ]
        }


def test_pipeline_repairs_failed_selftest(tmp_path, settings, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    vendor = root / "vendor" / "d-class"
    if not (vendor / "D-01" / "worker.py").exists():
        pytest.skip("vendor/d-class is not present")

    settings = dataclasses.replace(
        settings,
        workspace_root=tmp_path,
        data_dir=tmp_path / "data",
        team_dir=tmp_path / "data" / "test-team",
        expert_dir=tmp_path / "data" / "expert",
        figure_dir=tmp_path / "data" / "figure" / "test-team",
        d_materials_root=vendor,
        d_samples_root=vendor / "公开样例请求",
        d_selftest=True,
        d_repair_attempts=2,
        d_selftest_timeout=10.0,
        case_budget=60.0,
        d_formal_reserve=5.0,
    )
    settings.ensure_dirs()

    client = ScriptedClient()
    called = {}

    def fake_run_experiment_client(case_id, implementation, timeout_seconds=120.0):
        called["case_id"] = case_id
        called["implementation"] = implementation
        return {
            "case_id": case_id,
            "status": "completed",
            "metrics": [
                {"name": "Macro-F1", "unit": "分数", "reference": None, "observed": 1.0, "relative_error": None},
                {"name": "Accuracy", "unit": "%", "reference": None, "observed": 100.0, "relative_error": None},
                {"name": "运行时间", "unit": "秒", "reference": None, "observed": 0.1, "relative_error": None},
            ],
            "figures": [],
        }

    monkeypatch.setattr(pipeline_module, "run_experiment_client", fake_run_experiment_client)

    result = pipeline_module.run_case({"case_id": "D-01", "input": "复现"}, settings, client)
    assert result["status"] == "completed"
    assert client.calls >= 2
    assert "implementation-repair-1.py" in called["implementation"]

    report = json.loads((settings.team_dir / "work" / "D-01" / "selftest.json").read_text(encoding="utf-8"))
    assert report["case_id"] == "D-01"
    assert report["ok"] is True


def test_validation_is_generic_and_never_judges_task_semantics() -> None:
    # Notice (D rules update): no task-specific semantic validators in the image.
    request = {"data": {"train": [{"text": "a b", "label": "x"}], "texts": ["a"]}}
    semantically_dubious = {"baseline": ["x"], "candidate": None, "details": {}}
    for case_id in ("D-01", "D-02", "D-05", "D-09"):
        assert selftest.validate(case_id, "run", request, semantically_dubious) == []
        assert selftest.validate(case_id, "run", request, {}) != []
        assert selftest.validate(case_id, "run", request, ["not", "an", "object"]) != []
    assert not [name for name in dir(selftest) if name.startswith("_validate_d0")]


def _case_dir(tmp_path, protocol=None):
    root = Path(__file__).resolve().parents[1] / "vendor" / "d-class"
    if not (root / "D-01" / "worker.py").exists():
        pytest.skip("vendor/d-class is not present")
    case = tmp_path / "experiments" / "D-01"
    case.mkdir(parents=True)
    for name in ("worker.py", "artifacts.py", "font.py", "protocol.json", "sample.json", "method.md"):
        (case / name).write_bytes((root / "D-01" / name).read_bytes())
    if protocol is not None:
        (case / "protocol.json").write_text(json.dumps(protocol, ensure_ascii=False), encoding="utf-8")
    return case


def test_prompt_is_built_from_the_round_materials_only(tmp_path) -> None:
    from d_adapter import gateway
    case = tmp_path / "D-01"
    case.mkdir()
    (case / "method.md").write_text("ROUND-METHOD: implement method Q.", encoding="utf-8")
    (case / "protocol.json").write_text('{"return": "ROUND-SCHEMA"}', encoding="utf-8")
    (case / "sample.json").write_text('{"case_id": "D-01", "phase": "run", "data": {}}', encoding="utf-8")
    (case / "notes.md").write_text("ROUND-EXTRA material", encoding="utf-8")
    prompt = gateway.build_prompt("D-01", "TASK-INPUT", case)
    for marker in ("ROUND-METHOD", "ROUND-SCHEMA", "TASK-INPUT", "ROUND-EXTRA"):
        assert marker in prompt
    # Nothing depends on the case number except the case number itself.
    other = gateway.build_prompt("D-05", "TASK-INPUT", case)
    assert other.rsplit("【case_id】", 1)[0] == prompt.rsplit("【case_id】", 1)[0]
    assert not hasattr(gateway, "CASE_CHECKLIST") and not hasattr(gateway, "CASE_SKELETON")
    for practice_hint in ("FIFO", "k1", "title_weight", "27 行", "加权公平", "baseline/candidate/details"):
        assert practice_hint not in gateway.PROMPT_HEADER


def test_selftest_runs_the_rounds_own_sample_request(tmp_path, settings) -> None:
    from core.budget import Deadline
    case = _case_dir(tmp_path)
    settings = dataclasses.replace(settings, d_materials_root=case.parent, d_samples_root=None,
                                   d_selftest_timeout=10.0, d_formal_reserve=1.0)
    implementation = tmp_path / "impl.py"
    implementation.write_text("def run_experiment(request):\n    return {'echo': sorted(request)}\n", encoding="utf-8")
    report = selftest.run_selftest("D-01", implementation, settings, tmp_path / "work", Deadline(60))
    assert report.ok and not report.skipped
    assert [sample.name for sample in report.samples] == ["sample"]
    bad = tmp_path / "bad.py"
    bad.write_text(BAD_SOURCE, encoding="utf-8")
    report = selftest.run_selftest("D-01", bad, settings, tmp_path / "work2", Deadline(60))
    assert not report.ok and "field_this_task_does_not_have" in report.error_text()


def test_samples_written_for_another_protocol_are_skipped(tmp_path, settings) -> None:
    from core.budget import Deadline
    root = Path(__file__).resolve().parents[1] / "vendor" / "d-class"
    protocol = json.loads((root / "D-01" / "protocol.json").read_text(encoding="utf-8"))
    protocol["learning_rate"] = 0.123  # the formal round changed a parameter
    case = _case_dir(tmp_path, protocol=protocol)
    settings = dataclasses.replace(settings, d_materials_root=case.parent, d_samples_root=root / "公开样例请求",
                                   d_selftest_timeout=10.0, d_formal_reserve=1.0)
    implementation = tmp_path / "impl.py"
    implementation.write_text("def run_experiment(request):\n    return {'ok': True}\n", encoding="utf-8")
    report = selftest.run_selftest("D-01", implementation, settings, tmp_path / "work", Deadline(60),
                                   max_samples=10)
    assert [sample.name for sample in report.samples] == ["sample"]  # practice requests are stale


def test_pipeline_regenerates_truncated_source(tmp_path, settings, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    vendor = root / "vendor" / "d-class"
    if not (vendor / "D-01" / "worker.py").exists():
        pytest.skip("vendor/d-class is not present")

    settings = dataclasses.replace(
        settings,
        workspace_root=tmp_path,
        data_dir=tmp_path / "data",
        team_dir=tmp_path / "data" / "test-team",
        expert_dir=tmp_path / "data" / "expert",
        figure_dir=tmp_path / "data" / "figure" / "test-team",
        d_materials_root=vendor,
        d_samples_root=vendor / "公开样例请求",
        d_selftest=True,
        d_repair_attempts=1,
        d_selftest_timeout=10.0,
        case_budget=60.0,
        d_formal_reserve=5.0,
    )
    settings.ensure_dirs()

    client = TruncatedThenGoodClient()
    called = {}

    def fake_run_experiment_client(case_id, implementation, timeout_seconds=120.0):
        called["implementation"] = implementation
        return {
            "case_id": case_id,
            "status": "completed",
            "metrics": [
                {"name": "Macro-F1", "unit": "分数", "reference": None, "observed": 1.0, "relative_error": None},
                {"name": "Accuracy", "unit": "%", "reference": None, "observed": 100.0, "relative_error": None},
                {"name": "运行时间", "unit": "秒", "reference": None, "observed": 0.1, "relative_error": None},
            ],
            "figures": [],
        }

    monkeypatch.setattr(pipeline_module, "run_experiment_client", fake_run_experiment_client)

    result = pipeline_module.run_case({"case_id": "D-01", "input": "复现"}, settings, client)
    assert result["status"] == "completed"
    assert client.calls >= 2
    assert "implementation-repair-1.py" in called["implementation"]


def test_solve_task_d_end_to_end(tmp_path, settings, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    vendor = root / "vendor" / "d-class"
    if not (vendor / "D-01" / "worker.py").exists():
        pytest.skip("vendor/d-class is not present")

    settings = dataclasses.replace(
        settings,
        workspace_root=tmp_path,
        data_dir=tmp_path / "data",
        team_dir=tmp_path / "data" / "test-team",
        expert_dir=tmp_path / "data" / "expert",
        figure_dir=tmp_path / "data" / "figure" / "test-team",
        d_materials_root=vendor,
        d_samples_root=vendor / "公开样例请求",
        d_selftest=True,
        d_repair_attempts=2,
        d_selftest_timeout=10.0,
        case_budget=60.0,
        d_formal_reserve=5.0,
    )
    settings.ensure_dirs()

    class GoodClient:
        def complete_raw(self, messages, **kwargs):
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "```python\n" + GOOD_SOURCE + "\n```",
                        },
                    }
                ]
            }

    def fake_run_experiment_client(case_id, implementation, timeout_seconds=120.0):
        return {
            "case_id": case_id,
            "status": "completed",
            "metrics": [
                {"name": "Macro-F1", "unit": "分数", "reference": None, "observed": 0.9, "relative_error": None},
                {"name": "Accuracy", "unit": "%", "reference": None, "observed": 90.0, "relative_error": None},
                {"name": "运行时间", "unit": "秒", "reference": None, "observed": 0.2, "relative_error": None},
            ],
            "figures": [],
        }

    monkeypatch.setattr(solve_module, "model_client", lambda _settings: GoodClient())
    monkeypatch.setattr(pipeline_module, "run_experiment_client", fake_run_experiment_client)

    result = solve_module.solve_task(
        {"case_id": "D-01", "function": "Reproduction", "input": "复现", "materials": [], "constraints": []},
        settings,
    )
    assert result["status"] == "completed"
    assert len(result["metrics"]) == 3

    on_disk = json.loads((settings.team_dir / "D-01.json").read_text(encoding="utf-8"))
    assert set(on_disk) == {"case_id", "status", "metrics", "figures"}
    assert on_disk["status"] == "completed"


def test_run_selftest_caps_sample_count(tmp_path, settings) -> None:
    root = Path(__file__).resolve().parents[1]
    vendor = root / "vendor" / "d-class"
    reference = root / "vendor" / "reference" / "D-01.py"
    if not (vendor / "D-01" / "worker.py").exists() or not reference.exists():
        pytest.skip("vendor/d-class or vendor/reference is not present")

    settings = dataclasses.replace(
        settings,
        workspace_root=tmp_path,
        data_dir=tmp_path / "data",
        team_dir=tmp_path / "data" / "test-team",
        expert_dir=tmp_path / "data" / "expert",
        figure_dir=tmp_path / "data" / "figure" / "test-team",
        d_materials_root=vendor,
        d_samples_root=vendor / "公开样例请求",
        d_selftest_timeout=10.0,
        case_budget=120.0,
        d_formal_reserve=5.0,
        d_selftest_max_samples=1,
    )
    settings.ensure_dirs()
    from core.budget import Deadline

    report = selftest.run_selftest(
        "D-01",
        reference,
        settings,
        settings.team_dir / "work" / "D-01",
        Deadline(120.0),
        max_samples=1,
    )
    assert report.ok
    assert len(report.samples) == 1


def test_pipeline_still_calls_formal_when_selftest_is_skipped(tmp_path, settings, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    vendor = root / "vendor" / "d-class"
    if not (vendor / "D-01" / "worker.py").exists():
        pytest.skip("vendor/d-class is not present")

    settings = dataclasses.replace(
        settings,
        workspace_root=tmp_path,
        data_dir=tmp_path / "data",
        team_dir=tmp_path / "data" / "test-team",
        expert_dir=tmp_path / "data" / "expert",
        figure_dir=tmp_path / "data" / "figure" / "test-team",
        d_materials_root=vendor,
        d_samples_root=vendor / "公开样例请求",
        d_selftest=True,
        d_repair_attempts=1,
        d_selftest_timeout=5.0,
        case_budget=50.0,
        d_formal_reserve=45.0,
    )
    settings.ensure_dirs()

    client = ScriptedClient()
    called = {}

    def fake_run_experiment_client(case_id, implementation, timeout_seconds=120.0):
        called["case_id"] = case_id
        return {
            "case_id": case_id,
            "status": "completed",
            "metrics": [
                {"name": "Macro-F1", "unit": "分数", "reference": None, "observed": 1.0, "relative_error": None},
                {"name": "Accuracy", "unit": "%", "reference": None, "observed": 100.0, "relative_error": None},
                {"name": "运行时间", "unit": "秒", "reference": None, "observed": 0.1, "relative_error": None},
            ],
            "figures": [],
        }

    monkeypatch.setattr(pipeline_module, "run_experiment_client", fake_run_experiment_client)

    result = pipeline_module.run_case({"case_id": "D-01", "input": "复现"}, settings, client)
    assert result["status"] == "completed"
    assert called["case_id"] == "D-01"
    assert client.calls == 1
