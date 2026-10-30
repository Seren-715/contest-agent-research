"""A / Chat: answer plus exactly one clarifying question."""

from __future__ import annotations

from typing import Any

from core.config import Settings
from core.llm import ModelClient
from handlers.base import HandlerError, constraints_text, extract_json_object, question_text

SYSTEM = (
    "你是一名严谨的科研助手。回答必须基于通用的科研常识与题目给出的信息，"
    "不要编造具体论文、数字或引用。只输出一个 JSON 对象，不要加代码围栏或解释。"
)


class ChatHandler:
    def handle(self, question: dict[str, Any], settings: Settings, client: ModelClient) -> dict[str, Any]:
        case_id = question["case_id"]
        extra = constraints_text(question)
        prompt = (
            f"题目：{question_text(question)}\n\n"
            f"额外约束：\n{extra or '（无）'}\n\n"
            "请完成两件事：\n"
            "1. 直接回答题目中的科研问题，给出有条件的、可操作的解释；\n"
            "2. 提出一个最关键的、用于限定研究范围的澄清问题。\n"
            "只输出 JSON：{\"answer\": \"...\", \"clarifying_question\": \"...\"}"
        )
        text = client.complete(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            temperature=0.3,
        )
        try:
            data = extract_json_object(text)
        except HandlerError:
            data = {}
        answer = str(data.get("answer") or "").strip() or text.strip()
        clarifying = str(data.get("clarifying_question") or "").strip()
        if not answer:
            raise HandlerError("chat answer is empty")
        if not clarifying:
            clarifying = "你希望把研究范围限定在哪个具体对象、场景或时间尺度上？"
        return {"case_id": case_id, "answer": answer, "clarifying_question": clarifying}
