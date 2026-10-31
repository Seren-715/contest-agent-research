from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.budget import Deadline
from core.config import case_budget_seconds, case_timeout_seconds, load_settings
from core.llm import ModelClient, ModelError
import core.budget as budget_module
import core.llm as llm_module
import d_adapter.gateway as gateway
import d_adapter.pipeline as pipeline
import d_adapter.runner as runner


@pytest.mark.parametrize("function,expected", [
    ("Chat", 540), ("Literature_Retrieval", 540),
    ("Survey", 900), ("Reproduction", 900), ("Idea", 900),
])
def test_case_limits_match_current_rules(settings, function, expected):
    assert case_timeout_seconds(function, settings) == expected
    assert case_timeout_seconds(function, replace(settings, case_timeout=9999)) == expected
    assert case_timeout_seconds(function, replace(settings, case_timeout=120)) == 120


def test_internal_budget_reserves_finalization(settings):
    configured = replace(settings, case_budget=9999, finalize_reserve=10)
    assert case_budget_seconds("Chat", configured) == 530
    assert case_budget_seconds("Reproduction", configured) == 890
    assert case_budget_seconds("Reproduction", replace(configured, case_timeout=100)) == 90


def test_environment_overrides_cannot_extend_official_limit(monkeypatch):
    monkeypatch.setenv("CONTEST_CASE_TIMEOUT_MS", "9999999")
    monkeypatch.setenv("CONTEST_CASE_BUDGET", "9999")
    settings = load_settings()
    assert case_timeout_seconds("Reproduction", settings) == 900
    assert case_budget_seconds("Reproduction", settings) == 890


def test_deadline_does_not_spend_reserved_time(monkeypatch):
    monkeypatch.setattr(budget_module.time, "monotonic", lambda: 40)
    deadline = Deadline(100, started=0)
    assert deadline.timeout(300, reserve=20) == 40
    with pytest.raises(TimeoutError):
        deadline.timeout(300, reserve=60)
    with pytest.raises(TimeoutError):
        Deadline(20, started=0).timeout(880)


def test_model_request_uses_call_specific_remaining_timeout(monkeypatch):
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kwargs):
            return SimpleNamespace(model_dump=lambda: {"choices": []})

    monkeypatch.setattr(llm_module, "_OpenAI", FakeOpenAI)
    client = ModelClient("http://unused.test/v1", "fake", "fake", timeout=300)
    client.complete_raw([], timeout=17)
    assert captured["timeout"] == 17
    assert captured["max_retries"] == 0


def test_generation_and_token_retry_recompute_remaining_budget(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(budget_module.time, "monotonic", lambda: clock[0])
    seen = []

    class Client:
        timeout = 300

        def complete_raw(self, messages, **kwargs):
            seen.append(kwargs["timeout"])
            clock[0] = 20
            if len(seen) == 1:
                raise ModelError("max_tokens too large")
            return {"choices": []}

    gateway._complete_raw(Client(), [], 64000, Deadline(100, started=0), 70)
    assert seen == [30, 10]


def test_token_retry_stops_when_only_formal_reserve_remains(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(budget_module.time, "monotonic", lambda: clock[0])
    calls = []

    class Client:
        timeout = 300

        def complete_raw(self, messages, **kwargs):
            calls.append(kwargs)
            clock[0] = 30
            raise ModelError("max_tokens too large")

    with pytest.raises(TimeoutError):
        gateway._complete_raw(Client(), [], 64000, Deadline(100, started=0), 70)
    assert len(calls) == 1


def _pipeline_settings(settings):
    configured = replace(settings, case_budget=900, finalize_reserve=10, d_selftest=False)
    (configured.d_materials_root / "D-01").mkdir(parents=True)
    return configured


def test_formal_timeout_uses_remaining_budget_not_old_120_second_cap(settings, monkeypatch):
    settings = _pipeline_settings(settings)
    generated = gateway.GeneratedSource(Path("unused.py"), "digest", Path("response.json"))
    monkeypatch.setattr(pipeline.gateway, "generate", lambda *a, **kw: generated)
    seen = []
    monkeypatch.setattr(pipeline, "run_experiment_client",
                        lambda *a, **kw: seen.append(kw["timeout_seconds"]) or {"status": "completed"})
    assert pipeline.run_case({"case_id": "D-01"}, settings, object())["status"] == "completed"
    assert len(seen) == 1
    assert 120 < seen[0] <= 880


def test_formal_run_never_starts_after_deadline(settings, monkeypatch):
    settings = _pipeline_settings(settings)
    clock = [0.0]
    monkeypatch.setattr(budget_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(pipeline, "Deadline", lambda seconds: Deadline(seconds, started=0))

    def generate(*args, **kwargs):
        clock[0] = 900
        return gateway.GeneratedSource(Path("unused.py"), "digest", Path("response.json"))

    monkeypatch.setattr(pipeline.gateway, "generate", generate)
    seen = []
    monkeypatch.setattr(pipeline, "run_experiment_client", lambda *a, **kw: seen.append(kw))
    with pytest.raises(TimeoutError):
        pipeline.run_case({"case_id": "D-01"}, settings, object())
    assert not seen


def test_runner_caps_official_client_timeout_and_runs_once(settings, monkeypatch):
    settings.d_client.write_text("# not executed", encoding="utf-8")
    seen = []

    class FakeProcess:
        returncode = 0

        def __init__(self, *args, **kwargs):
            seen.append(kwargs)

        def communicate(self, timeout=None):
            seen.append(timeout)
            return '{"case_id":"D-01","status":"completed"}', ""

        def poll(self):
            return 0

    monkeypatch.setattr(runner.subprocess, "Popen", FakeProcess)
    result = runner.run_platform("D-01", Path("implementation.py"), settings, timeout=9999)
    assert result["status"] == "completed"
    assert len(seen) == 2
    assert seen[1] == 880


def test_runner_timeout_kills_client_without_replay(settings, monkeypatch):
    settings.d_client.write_text("# not executed", encoding="utf-8")
    seen = []

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            self.returncode = None
            seen.append("start")

        def communicate(self, timeout=None):
            if timeout is not None:
                raise runner.subprocess.TimeoutExpired("client", timeout)
            return "", ""

        def kill(self):
            seen.append("kill")
            self.returncode = -1

        def poll(self):
            return self.returncode

    monkeypatch.setattr(runner.subprocess, "Popen", FakeProcess)
    with pytest.raises(runner.RunnerError, match="timed out"):
        runner.run_platform("D-01", Path("implementation.py"), settings, timeout=1)
    assert seen == ["start", "kill"]


def _client_returning(body, seen=None):
    from core.llm import ModelClient

    class Stub(ModelClient):
        def complete_raw(self, messages, **kwargs):
            if seen is not None:
                seen.append(kwargs.get("max_tokens"))
            item = body.pop(0) if isinstance(body, list) else body
            if isinstance(item, Exception):
                raise item
            return item

    return Stub("http://x", "k", "m")


def test_truncated_reasoning_without_answer_is_an_error_not_an_answer():
    body = {"choices": [{"finish_reason": "length",
                         "message": {"content": "", "reasoning_content": "Let me think about the papers..."}}]}
    with pytest.raises(ModelError, match="finish_reason=length"):
        _client_returning(body).complete([{"role": "user", "content": "q"}], max_tokens=100)


def test_completed_reasoning_content_is_still_used_when_content_is_empty():
    body = {"choices": [{"finish_reason": "stop", "message": {"content": "", "reasoning_content": "{\"ok\":1}"}}]}
    assert _client_returning(body).complete([{"role": "user", "content": "q"}]) == "{\"ok\":1}"


def test_gateway_token_cap_retries_once_at_16000():
    seen = []
    ok = {"choices": [{"finish_reason": "stop", "message": {"content": "answer"}}]}
    client = _client_returning([ModelError("max_tokens too large"), ok], seen)
    assert client.complete([{"role": "user", "content": "q"}], max_tokens=32000) == "answer"
    assert seen == [32000, 16000]


def test_bcde_calls_use_the_shared_output_budget(monkeypatch):
    from core.llm import output_budget
    assert output_budget() == 32000
    monkeypatch.setenv("CONTEST_MAX_TOKENS", "50000")
    assert output_budget() == 50000
