"""Small per-case phase receipts; never store prompts, URLs or credentials."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from core.config import Settings
from core.execution import ExecutionCancelled
from core.files import atomic_write_text


def record_stage(settings: Settings, case_id: str, phase: str, **details: Any) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", case_id):
        return
    path = settings.team_dir / "work" / "diagnostics" / f"{case_id}.json"
    # This helper is diagnostic only; failure must not replace the real error.
    try:
        events = []
        if phase != "start" and path.exists():
            events = json.loads(path.read_text(encoding="utf-8")).get("events", [])
        events.append({"phase": phase, **details})
        atomic_write_text(path, json.dumps({"case_id": case_id, "events": events[-32:]}, ensure_ascii=False, indent=2))
    except ExecutionCancelled:
        raise
    except (OSError, ValueError, TypeError):
        logging.getLogger(__name__).warning("case_diagnostics_unavailable case_id=%s phase=%s", case_id, phase)
