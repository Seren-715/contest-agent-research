from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

import d_adapter.pipeline as pipeline_module
import core.solve as solve_module
from d_adapter import selftest


BAD_SOURCE = """
def run_experiment(request):
    n = len(request["data"]["texts"])
    return {"baseline": ["x"] * n, "candidate": None, "details": {}}
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


def test_d01_validator_rejects_null_candidate() -> None:
    request = {"data": {"train": [{"text": "a b", "label": "x"}], "texts": ["a"]}}
    result = {"baseline": ["x"], "candidate": None, "details": {}}
    errors = selftest.validate("D-01", "run", request, result)
    assert errors
    assert "candidate" in errors[0]


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
