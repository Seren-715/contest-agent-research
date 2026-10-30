"""Filesystem contract: write results, reports and figures, then read them back."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.config import Settings
from core.execution import ExecutionCancelled, check_active
from core.files import atomic_write_text
from core.schemas import report_limit_errors


class OutputError(RuntimeError):
    pass


def result_path(settings: Settings, case_id: str) -> Path:
    return settings.team_dir / f"{case_id}.json"


def report_path(settings: Settings, function: str, case_id: str) -> Path:
    if function == "Survey":
        name = f"{case_id}-综述-{settings.team}.md"
    elif function == "Idea":
        name = f"{case_id}-灵感Idea报告-{settings.team}.md"
    else:
        raise OutputError(f"{function} does not produce a report file")
    return settings.expert_dir / name


def relative_path(settings: Settings, path: Path) -> str:
    try:
        return path.resolve().relative_to(settings.workspace_root).as_posix()
    except ValueError as exc:
        raise OutputError(f"path is outside workspace root: {path}") from exc


def write_text(path: Path, text: str) -> None:
    # Route writes through the decorated tool so the SDK records the operation.
    # If the write happens outside an SDK session (for example in the timeout
    # fallback after the session context closed), fall back to a direct write
    # rather than losing the honest failed submission.
    check_active()
    try:
        from tools import write_text_file

        write_text_file(str(path), text)
    except ExecutionCancelled:
        raise
    except Exception:
        atomic_write_text(path, text)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def write_report(settings: Settings, function: str, case_id: str, markdown: str) -> str:
    errors = report_limit_errors(markdown, function)
    if errors:
        raise OutputError("; ".join(errors))
    path = report_path(settings, function, case_id)
    write_text(path, markdown)
    if path.read_bytes() != markdown.encode("utf-8"):
        raise OutputError(f"report readback mismatch: {path}")
    return relative_path(settings, path)


def write_result(settings: Settings, case_id: str, payload: dict[str, Any]) -> Path:
    path = result_path(settings, case_id)
    write_json(path, payload)
    check_result(path, case_id)
    return path


def check_result(path: Path, case_id: str) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        raise OutputError(f"result file missing or empty: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OutputError(f"result file is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise OutputError(f"result file is not a JSON object: {path}")
    if payload.get("case_id") != case_id:
        raise OutputError(f"case_id mismatch in {path}: {payload.get('case_id')!r} != {case_id!r}")
    return payload


def check_figures(settings: Settings, figures: list[dict[str, Any]]) -> None:
    prefix = f"data/figure/{settings.team}/"
    for figure in figures:
        path = str(figure.get("path", ""))
        if not path.startswith(prefix):
            raise OutputError(f"figure path must start with {prefix}: {path}")
        absolute = settings.workspace_root / path
        if not absolute.exists():
            raise OutputError(f"figure file does not exist: {path}")
