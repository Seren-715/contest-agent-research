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
    "请根据下面本轮提供的任务材料，自行实现完整的、兼容 Python 3.9 的标准库算法"
    "（平台算法容器固定为 Python 3.9；不要使用 3.10+ 才有的语法或标准库特性）。"
    "定义 run_experiment(request)，不得联网、不得打印日志、不得读取隐藏标签或材料以外的文件。"
    "必须实现材料指定的方法、参数和步骤；换用其他算法即使指标更好也不算复现。"
    "正式任务可能与任何练习题不同：方法、参数、字段和返回结构只以下面的当轮材料为准。"
    "run_experiment 返回的是算法内部结果对象，按 protocol.json 规定的返回字段、类型和嵌套结构给出；"
    "它不是最终 Reproduction 答卷，不要返回 case_id/status/metrics/figures 或自算指标。"
    "材料中的字段和类型模板只说明格式，占位值不能作为实际结果。"
    "实现必须确定、单进程、只使用标准库。"
    "只返回一个完整的 ```python 代码块，不要省略函数、不要返回补丁、不要拆分成多段，"
    "不要输出长篇思考过程。"
)

# Files read from the round's case directory, in this order. Other text
# material in that directory is appended generically (never by case number).
PRIMARY_MATERIALS = ("method.md", "protocol.json", "sample.json")
# Execution adapters and integrity metadata: not task material for the model.
NON_TASK_FILES = {"worker.py", "artifacts.py", "font.py", "experiment.py", "manifest.json"}
EXTRA_SUFFIXES = {".md", ".txt", ".json", ".py", ".csv"}
EXTRA_MATERIAL_CHARS = 40000


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
    """Generic prompt from the round's materials only (D rules update:
    no task-specific hints, skeletons or checklists in the image)."""
    parts = [PROMPT_HEADER, f"【题目输入】\n{task_input}"]
    for name in PRIMARY_MATERIALS:
        path = materials_dir / name
        if path.is_file():
            parts.append(f"【{name}】\n{_read(path)}")
    budget = EXTRA_MATERIAL_CHARS
    for path in sorted(materials_dir.iterdir()) if materials_dir.is_dir() else []:
        if (not path.is_file() or path.name in PRIMARY_MATERIALS or path.name in NON_TASK_FILES
                or path.suffix.lower() not in EXTRA_SUFFIXES):
            continue
        try:
            text = _read(path)
        except (OSError, UnicodeDecodeError):
            continue
        if len(text) > budget:
            continue
        budget -= len(text)
        parts.append(f"【{path.name}】\n{text}")
    if len(parts) == 2:
        raise GenerationError(f"no task material found in {materials_dir}")
    parts.append(f"【case_id】{case_id}")
    return "\n\n".join(parts)


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
