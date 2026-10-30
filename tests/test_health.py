from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import app


def test_health() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["healthy"] is True
    assert payload["status"] == "healthy"


def test_solve_chat_with_mock_model(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CONTEST_TEAM", "test-team")
    monkeypatch.setenv("CONTEST_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("CONTEST_MOCK_LLM", "1")

    client = TestClient(app)
    question = {
        "case_id": "A-01",
        "function": "Chat",
        "title": "测试题",
        "input": "请解释光照与植物生长的关系。",
        "materials": [],
        "constraints": [],
        "expected_output": "回答并追问",
        "output_schema_ref": "Chat",
    }
    response = client.post("/solve", json={"task_id": "A-01", "input": question})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["task_id"] == "A-01"
    assert payload["status"] == "success"
    assert payload["output"]["case_id"] == "A-01"
    assert payload["output"]["answer"]
    assert payload["output"]["clarifying_question"]

    result = tmp_path / "data" / "test-team" / "A-01.json"
    assert result.exists()
    assert json.loads(result.read_text(encoding="utf-8"))["case_id"] == "A-01"
