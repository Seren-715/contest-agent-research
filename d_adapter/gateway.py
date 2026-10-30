"""Generate a complete D-class implementation through the contest Gateway.

The model response is saved in full, then the source is extracted verbatim. No
local edits are allowed: if the response is truncated or has multiple versions,
ask the model again for one complete implementation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.budget import Deadline
from core.execution import check_active
from core.llm import ModelClient, ModelError
from d_adapter.source import SourceError
from tools import save_generated_source, write_text_file


class GenerationError(RuntimeError):
    pass


@dataclass
class GeneratedSource:
    path: Path
    sha256: str
    response_path: Path


# The contest agent itself runs on Python 3.11. This prompt targets the
# platform's *algorithm* container, which protocol.json freezes at Python 3.9.
# Keep the distinction: telling the model to write 3.11-only code would make the
# generated implementation fail in the platform executor.
PROMPT_HEADER = (
    "请根据以下公开方法和协议，自行实现完整的、兼容 Python 3.9 的标准库算法"
    "（平台算法容器固定为 Python 3.9；不要使用 3.10+ 才有的语法或标准库特性）。"
    "定义 run_experiment(request)，不得联网、不得打印日志、不得读取隐藏标签。"
    "只返回一个完整的 ```python 代码块，不要省略函数、不要返回补丁、不要拆分成多段。"
    "返回算法内部结构（baseline/candidate/details），不要返回 case_id/status/metrics/figures "
    "或自算指标。实现必须确定、单进程、只使用标准库，并严格遵守 protocol.json 的 response_schema。"
    "不要输出长篇思考过程；最终只返回一个完整的 ```python 代码块。"
)

CASE_SKELETON = {
    "D-01": (
        '{"baseline":["label",...],"candidate":["label",...],'
        '"details":{"classes":["..."],"vocabulary":["..."],"idf":[...],'
        '"weights":[[...]]}}'
    ),
    "D-02": (
        'run: {"baseline":{"qid":[{"id":"...","score":1.0}]},"candidate":{...},'
        '"details":{"selected":{"k1":...,"b":...,"title_weight":...},'
        '"search":[{"config":{...},"rankings":{"qid":[{"id":"...","score":1.0}]},'
        '"recall_at_5_percent":...,"mrr_at_10":...,'
        '"group_metrics":{"groups":{...},"worst_mrr_at_10":...,'
        '"macro_mrr_at_10":...,"macro_recall_at_5_percent":...}}]}}; '
        'rank: {"candidate":{"qid":[{"id":"...","score":1.0}]}}'
    ),
    "D-05": (
        '{"baseline":{"trace":[{"id":"...","flow":"...","arrival_ms":...,'
        '"service_ms":...,"deadline_ms":...,"server":0,"dispatch_ms":...,'
        '"setup_ms":...,"start_ms":...,"end_ms":...,"wait_ms":...}],'
        '"served_ms":{"flow":0},"dropped":[{"id":"...","reason":"deadline"}]},'
        '"candidate":{...},"details":{}}'
    ),
}

CASE_CHECKLIST = {
    "D-01": (
        "自测会检查：baseline/candidate 与 data.texts 对齐；candidate 非 null；"
        "details 直接包含 classes/vocabulary/idf/weights；classes/vocabulary 字典序；"
        "weights 为 类别数×(词表大小+1)，最后一列 bias；空文本保留 bias；无 NaN/Inf；"
        "允许数值等价，但训练方法、特征构造、类别顺序和并列规则必须符合 method.md。"
    ),
    "D-02": (
        "自测会检查：baseline/candidate 覆盖全部 test 文档；details.selected 含 k1/b/title_weight；"
        "details.search 恰好 27 行；每行含 config/rankings/recall_at_5_percent/mrr_at_10/group_metrics；"
        "dev rankings 覆盖全部文档；N 含空字段文档；avg_len=0 取 1；重复查询词重复计分；"
        "rank 阶段只用 data.selected，不重新选参。"
    ),
    "D-05": (
        "自测会检查：baseline 为 FIFO、candidate 为加权公平；每个任务恰好出现在 trace 或 dropped 一次；"
        "served_ms 覆盖 flow_weights 全部流；trace 含 server/dispatch_ms/setup_ms/start_ms/end_ms/wait_ms；"
        "start=dispatch+setup，end=start+service，wait=start-arrival；start<deadline，end<=horizon；"
        "dropped 按 id 排序；拒绝任务不回队、不改变上一流；派发即累计已分配服务量。"
    ),
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_generation_meta(work_dir: Path, name: str, body: dict[str, Any]) -> None:
    """Record finish_reason/usage next to the response for budget diagnosis."""
    try:
        choices = body.get("choices") or []
        finish = choices[0].get("finish_reason") if choices and isinstance(choices[0], dict) else None
        content = ""
        if choices and isinstance(choices[0], dict):
            content = str((choices[0].get("message") or {}).get("content") or "")
        write_text_file(
            str(work_dir / f"generation-meta-{name}.json"),
            json.dumps(
                {
                    "finish_reason": finish,
                    "usage": body.get("usage") or {},
                    "content_chars": len(content),
                },
                ensure_ascii=False,
            ),
        )
    except Exception:
        pass


def _complete_raw(client: ModelClient, messages: list[dict[str, str]], max_tokens: int,
                  deadline: Deadline, reserve: float) -> dict[str, Any]:
    """Ask for a large output, but fall back if the gateway caps max_tokens."""
    check_active()
    try:
        return client.complete_raw(
            messages, temperature=0.1, max_tokens=max_tokens,
            timeout=deadline.timeout(getattr(client, "timeout", 300.0), reserve=reserve,
                                     label="model generation"),
        )
    except ModelError as exc:
        text = str(exc).lower()
        if max_tokens > 16000 and any(
            token in text for token in ("max_tokens", "maximum", "context length", "too large", "out of range")
        ):
            check_active()
            return client.complete_raw(
                messages, temperature=0.1, max_tokens=16000,
                timeout=deadline.timeout(getattr(client, "timeout", 300.0), reserve=reserve,
                                         label="model token-limit retry"),
            )
        raise


def build_prompt(case_id: str, task_input: str, materials_dir: Path) -> str:
    method = _read(materials_dir / "method.md")
    protocol = _read(materials_dir / "protocol.json")
    sample = _read(materials_dir / "sample.json")
    return "\n\n".join(
        [
            PROMPT_HEADER,
            f"【题目输入】\n{task_input}",
            f"【method.md】\n{method}",
            f"【protocol.json】\n{protocol}",
            f"【sample.json】\n{sample}",
            f"【case_id】{case_id}",
            "【自测会检查】\n" + CASE_CHECKLIST.get(case_id, ""),
            "【返回结构骨架】\n" + CASE_SKELETON.get(case_id, ""),
        ]
    )


def generate(
    client: ModelClient,
    case_id: str,
    task_input: str,
    materials_dir: Path,
    work_dir: Path,
    deadline: Deadline,
    max_tokens: int = 64000,
    reserve: float = 0.0,
) -> GeneratedSource:
    if not materials_dir.exists():
        raise GenerationError(f"D materials not found: {materials_dir}")
    work_dir.mkdir(parents=True, exist_ok=True)
    prompt = build_prompt(case_id, task_input, materials_dir)

    deadline.require(5.0, "model generation")
    body = _complete_raw(client, [{"role": "user", "content": prompt}], max_tokens, deadline, reserve)
    check_active()
    response_path = work_dir / "generation-response.json"
    write_text_file(str(response_path), json.dumps(body, ensure_ascii=False))
    _write_generation_meta(work_dir, "initial", body)

    try:
        saved = save_generated_source(body, str(work_dir / "implementation.py"))
    except SourceError as exc:
        finish = (body.get("choices") or [{}])[0].get("finish_reason")
        raise GenerationError(f"source extraction failed (finish_reason={finish!r}): {exc}") from exc

    return GeneratedSource(path=Path(saved["path"]), sha256=saved["sha256"], response_path=response_path)


def repair(
    client: ModelClient,
    case_id: str,
    task_input: str,
    materials_dir: Path,
    work_dir: Path,
    previous_response: dict[str, Any],
    error: str,
    deadline: Deadline,
    attempt: int = 1,
    max_tokens: int = 64000,
    reserve: float = 0.0,
) -> GeneratedSource:
    """Ask the same session model for a complete fixed implementation.

    Never patch the source locally. The caller must send the model's new complete
    response to the platform client.
    """
    deadline.require(5.0, "model repair")
    prompt = build_prompt(case_id, task_input, materials_dir)
    previous = ""
    choices = previous_response.get("choices") or []
    if choices and isinstance(choices[0], dict):
        previous = str((choices[0].get("message") or {}).get("content", ""))
    body = _complete_raw(
        client,
        [
            {
                "role": "user",
                "content": (
                    prompt
                    + "\n\n【上一次完整回复】\n"
                    + previous
                    + "\n\n【自测错误】\n"
                    + error
                    + "\n\n请返回一份完整修复后的实现，仍然只输出一个完整的 ```python 代码块。"
                ),
            }
        ],
        max_tokens,
        deadline,
        reserve,
    )
    check_active()
    response_path = work_dir / f"generation-response-repair-{attempt}.json"
    write_text_file(str(response_path), json.dumps(body, ensure_ascii=False))
    _write_generation_meta(work_dir, f"repair-{attempt}", body)
    try:
        saved = save_generated_source(body, str(work_dir / f"implementation-repair-{attempt}.py"))
    except SourceError as exc:
        raise GenerationError(f"repair extraction failed: {exc}") from exc
    return GeneratedSource(path=Path(saved["path"]), sha256=saved["sha256"], response_path=response_path)
