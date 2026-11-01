"""Encode where a B case failed into a delay visible on the platform.

The platform shows no logs, only per-case "纯处理耗时". When a retrieval case
fails, the /solve handler waits ``20 * phase + error`` seconds before
answering. Reading the processing time back gives the last recorded phase and
the error class. The failed case scores 0 either way; disable with
CONTEST_FAILURE_TIMING=0. Decoding table: docs/BC_SOURCE_ANALYSIS_20261031.md.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from core.config import Settings

# Phases recorded by handlers/b_retrieval.py, in pipeline order.
PHASES = {
    "start": 1,
    "material_resolution": 2,
    "materials_resolved": 3,
    "pdf_headers_checked": 4,
    "evidence_ready": 5,
    "query_expanded": 6,
    "model_screen": 7, "model_screen_failed": 7, "screened": 7,
    "model_rerank": 8, "model_rerank_failed": 8,
}

# (exception class name, message prefix) -> code; first match wins.
ERRORS = [
    ("MaterialResolutionError", "declared material not found", 1),
    ("MaterialResolutionError", "ambiguous", 2),
    ("MaterialResolutionError", "material outside allowed data roots", 3),
    ("MaterialResolutionError", "materials must contain", 4),
    ("MaterialResolutionError", "material path contains parent traversal", 5),
    ("MaterialResolutionError", "descriptive material scope", 6),
    ("MaterialResolutionError", "no declared material could be resolved", 7),
    ("MaterialResolutionError", "", 8),
    ("HandlerError", "retrieval materials contain no recognizable PDFs", 9),
    ("HandlerError", "retrieval materials have no extractable evidence", 10),
    ("HandlerError", "retrieval failed before any valid rerank response", 11),
    ("HandlerError", "", 12),
    ("ModelError", "", 13),
    ("TimeoutError", "", 14),
]
FALLBACK_ERRORS = {"OSError": 15, "PermissionError": 15, "FileNotFoundError": 15, "ValueError": 16,
                   "TypeError": 17, "AttributeError": 17, "KeyError": 17, "IndexError": 17}


def _last_phase(path: Path) -> int:
    try:
        events = json.loads(path.read_text(encoding="utf-8")).get("events", [])
    except (OSError, ValueError, AttributeError):
        return 0
    codes = [PHASES[event.get("phase")] for event in events if isinstance(event, dict) and event.get("phase") in PHASES]
    return codes[-1] if codes else 0


def _error_code(error: BaseException) -> int:
    # SolveError wraps the handler's exception; the cause carries the reason.
    chain = [error]
    while chain[-1].__cause__ is not None and len(chain) < 5:
        chain.append(chain[-1].__cause__)
    for exc in reversed(chain):
        names = [cls.__name__ for cls in type(exc).__mro__]
        message = str(exc)
        for name, prefix, code in ERRORS:
            if name in names and message.startswith(prefix):
                return code
    for exc in reversed(chain):
        for cls in type(exc).__mro__:
            if cls.__name__ in FALLBACK_ERRORS:
                return FALLBACK_ERRORS[cls.__name__]
    return 18


def failure_delay(settings: Settings, case_id: str, error: BaseException) -> int:
    """Seconds to wait before reporting a failed retrieval case (0 = disabled)."""
    if os.environ.get("CONTEST_FAILURE_TIMING", "1") == "0":
        return 0
    phase = _last_phase(settings.team_dir / "work" / "diagnostics" / f"{case_id}.json")
    return 20 * phase + _error_code(error)
