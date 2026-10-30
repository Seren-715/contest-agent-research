from __future__ import annotations

import asyncio

from agent import ContestAgent


def test_agent_solve_uses_sdk_contract(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CONTEST_TEAM", "test-team")
    monkeypatch.setenv("CONTEST_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("CONTEST_MOCK_LLM", "1")

    question = {
        "case_id": "A-01",
        "function": "Chat",
        "input": "请解释光照与植物生长的关系。",
        "constraints": [],
    }
    result = asyncio.run(ContestAgent().solve(question))
    assert result["case_id"] == "A-01"
    assert result["answer"]
    assert (tmp_path / "data" / "test-team" / "A-01.json").exists()


def test_agent_infers_function_from_case_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CONTEST_TEAM", "test-team")
    monkeypatch.setenv("CONTEST_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("CONTEST_MOCK_LLM", "1")

    result = asyncio.run(
        ContestAgent().solve(
            {
                "case_id": "A-02",
                "input": "请解释光照与植物生长的关系。",
                "constraints": [],
            }
        )
    )
    assert result["case_id"] == "A-02"
