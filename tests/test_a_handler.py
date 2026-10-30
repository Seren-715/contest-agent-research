from __future__ import annotations

from handlers.a_chat import ChatHandler


def test_chat_handler_parses_json(settings) -> None:
    from tests.conftest import FakeClient

    client = FakeClient(
        ['{"answer": "光照强度与时长共同影响生长速度。", "clarifying_question": "研究哪种植物？"}']
    )
    question = {"case_id": "A-01", "function": "Chat", "input": "解释光照与生长。", "constraints": []}
    result = ChatHandler().handle(question, settings, client)
    assert result == {
        "case_id": "A-01",
        "answer": "光照强度与时长共同影响生长速度。",
        "clarifying_question": "研究哪种植物？",
    }


def test_chat_handler_falls_back_when_not_json(settings) -> None:
    from tests.conftest import FakeClient

    client = FakeClient(["这是一段自然语言回答。"])
    question = {"case_id": "A-02", "function": "Chat", "input": "问题", "constraints": []}
    result = ChatHandler().handle(question, settings, client)
    assert result["answer"] == "这是一段自然语言回答。"
    assert result["clarifying_question"]
