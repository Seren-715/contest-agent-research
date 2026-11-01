from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import Settings  # noqa: E402


class FakeClient:
    def __init__(self, responses: list[str] | None = None):
        self.responses = list(responses or [])
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages, **kwargs) -> str:
        self.calls.append(messages)
        if not self.responses:
            raise AssertionError("FakeClient ran out of responses")
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)

    def complete_raw(self, messages, **kwargs):
        content = self.complete(messages, **kwargs)
        return {
            "choices": [
                {"finish_reason": "stop", "message": {"role": "assistant", "content": content}}
            ]
        }


@pytest.fixture(autouse=True)
def _survey_fact_check_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Call-count tests target one behaviour at a time; the fact-check pass has its own tests."""
    monkeypatch.setenv("CONTEST_SURVEY_FACT_CHECK", "0")
    # The B failure timing code sleeps up to minutes; tests/test_failure_code.py covers it.
    monkeypatch.setenv("CONTEST_FAILURE_TIMING", "0")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    team = "test-team"
    return Settings(
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
