"""Exercise the raw HTTP adapter with a small SDK stand-in, not an official SDK."""

from __future__ import annotations

from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from core.execution import ExecutionCancelled
from core.io import write_json


@pytest.fixture
def route_module(monkeypatch, tmp_path):
    class Runtime:
        def __init__(self, agent):
            self.agent = agent
            self.app = FastAPI()
            self.telemetry = SimpleNamespace(session=lambda *a, **kw: nullcontext(), flush=lambda: None)
            self._setup_routes()

        def _setup_routes(self):
            @self.app.post("/solve")
            def stock_route():
                return {"incorrect": "SDK envelope"}

    sdk = ModuleType("agenthack")
    sdk.AgentRuntime = Runtime
    sdk.BaseAgent = object
    telemetry = ModuleType("agenthack.telemetry")
    telemetry.task = lambda: nullcontext(SimpleNamespace(result=lambda value: None))
    monkeypatch.setitem(sys.modules, "agenthack", sdk)
    monkeypatch.setitem(sys.modules, "agenthack.telemetry", telemetry)
    monkeypatch.setenv("CONTEST_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("RESEARCH_WORKSPACE_TEAM", "test-team")
    spec = importlib.util.spec_from_file_location("isolated_contest_routes", Path(__file__).parents[1] / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def question(case_id="A-01", function="Chat"):
    return dict(case_id=case_id, function=function, title="Test", input="Question",
                materials=[], constraints=[], expected_output="JSON", output_schema_ref=function)


def test_raw_question_returns_answer_without_sdk_envelope(route_module, monkeypatch, tmp_path):
    received = []
    answer = {"case_id": "A-01", "answer": "Evidence", "clarifying_question": "Scope?"}

    def solve(body):
        received.append(body)
        return answer

    monkeypatch.setattr(route_module, "solve_task", solve)
    with TestClient(route_module.runtime.app) as client:
        response = client.post("/solve", json=question())
    assert response.status_code == 200
    assert response.json() == answer
    assert received == [question()]


def test_failed_idea_is_http_error_without_invalid_answer_file(route_module, monkeypatch, tmp_path, caplog):
    def fail(body):
        raise RuntimeError("generation failed api_key=secret-test-marker")

    monkeypatch.setattr(route_module, "solve_task", fail)
    with TestClient(route_module.runtime.app) as client:
        response = client.post("/solve", json=question("E-01", "Idea"))
    assert response.status_code == 503
    assert response.json()["code"] == "ERR_CASE_FAILED"
    assert not (tmp_path / "data" / "test-team" / "E-01.json").exists()
    assert "case_failed case_id=E-01 function=Idea error_type=RuntimeError" in caplog.text
    assert "secret-test-marker" not in caplog.text


def test_retrieval_failure_is_not_disguised_as_valid_empty_answer(route_module, monkeypatch, tmp_path):
    monkeypatch.setattr(route_module, "solve_task", lambda _: (_ for _ in ()).throw(RuntimeError("lookup failed")))
    with TestClient(route_module.runtime.app) as client:
        response = client.post("/solve", json=question("B-01", "Literature_Retrieval"))
    assert response.status_code == 503
    assert response.json()["code"] == "ERR_CASE_FAILED"
    assert not (tmp_path / "data/test-team/B-01.json").exists()


def test_route_timeout_result_cannot_be_overwritten(route_module, monkeypatch, tmp_path):
    release, finished, rejected = threading.Event(), threading.Event(), threading.Event()
    result = tmp_path / "data" / "test-team" / "A-01.json"

    def late_solve(body):
        release.wait(2)
        try:
            write_json(result, {"case_id": "A-01", "answer": "late"})
        except ExecutionCancelled:
            rejected.set()
            raise
        finally:
            finished.set()

    monkeypatch.setattr(route_module, "solve_task", late_solve)
    monkeypatch.setattr(route_module, "case_timeout_seconds", lambda function: 0.03)
    try:
        with TestClient(route_module.runtime.app) as client:
            response = client.post("/solve", json=question())
            assert response.status_code == 200
            before = result.read_bytes()
            assert json.loads(before) == response.json()
            release.set()
            assert finished.wait(2)
            assert rejected.is_set()
            assert result.read_bytes() == before
    finally:
        release.set()


def test_malformed_question_does_not_start_agent(route_module, monkeypatch):
    def forbidden(body):
        pytest.fail("invalid input reached agent")

    monkeypatch.setattr(route_module, "solve_task", forbidden)
    with TestClient(route_module.runtime.app) as client:
        response = client.post("/solve", json={})
    assert response.status_code == 400
