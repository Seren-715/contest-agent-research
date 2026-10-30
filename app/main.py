"""Local development server that mimics the AgentHack envelope.

In the official image, `agent.py` + AgentRuntime provide /health and /solve.
This FastAPI app exists only so the team can test locally without the base image.

Envelope:
    POST /solve {"task_id": "...", "input": {...}}
    -> {"task_id": "...", "status": "success", "output": {...}}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException

from core.config import Settings, load_settings
from core.dispatch import DispatchError, load_dispatch, load_question, resolve_question_file
from core.solve import SolveError, solve_task

VERSION = "0.1.0"

app = FastAPI(title="Contest Agent (local dev)", version=VERSION)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"question must be a JSON object: {path}")
    return value


def _extract_question(body: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Support the local convenience forms: question / question_file / dispatch_path."""
    question = body.get("question")
    if isinstance(question, str):
        question = json.loads(question)
    if isinstance(question, dict):
        return dict(question)

    if "question_file" in body:
        path = Path(str(body["question_file"]))
        if not path.is_absolute():
            path = settings.workspace_root / path
        return load_question(path, body.get("case_id"))

    if "dispatch_path" in body and "case_id" in body:
        dispatch = load_dispatch(Path(str(body["dispatch_path"])))
        path = resolve_question_file(dispatch, str(body["case_id"]), settings.workspace_root)
        return load_question(path, str(body["case_id"]))

    return body


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "healthy", "healthy": True, "version": VERSION}


@app.get("/ready")
def ready() -> dict[str, Any]:
    settings = load_settings()
    settings.ensure_dirs()
    return {
        "ready": True,
        "team": settings.team,
        "workspace_root": str(settings.workspace_root),
        "team_dir": str(settings.team_dir),
    }


@app.post("/solve")
def solve(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    settings = load_settings()
    task_id = body.get("task_id")

    if isinstance(body.get("input"), dict):
        task_input = body["input"]
    elif any(key in body for key in ("question", "question_file", "dispatch_path")):
        try:
            task_input = _extract_question(body, settings)
        except (ValueError, DispatchError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        task_input = body

    try:
        result = solve_task(task_input, settings)
    except SolveError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"task_id": task_id or result.get("case_id"), "status": "success", "output": result}


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "contest-agent", "version": VERSION, "endpoints": "/health /ready /solve"}
