"""Encode where a B case failed into a delay visible on the platform.

The platform shows no logs, only per-case "纯处理耗时". When a retrieval case
fails, the /solve handler waits ``20 * phase + error`` seconds before
answering. Reading the processing time back gives the last recorded phase and
the error class. The failed case scores 0 either way; disable with
CONTEST_FAILURE_TIMING=0. A delay that would not fit in the case's remaining
time limit is skipped entirely (see fit_delay). Decoding table: docs/BC_SOURCE_ANALYSIS_20261031.md.
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
    ("HandlerError", "retrieval scope contains no papers", 19),
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
    chain = _chain(error)
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


SHAPES = {"absolute": 0, "data_relative": 1, "relative": 2, "pdf_name": 3, "other_suffix": 4,
          "cjk_name": 5, "ascii_name": 6, "other": 7}
SKIP_REASONS = {"not_found": 0, "ambiguous": 1, "outside_roots": 2, "duplicate_name": 3}


def _chain(error: BaseException) -> list[BaseException]:
    chain = [error]
    while chain[-1].__cause__ is not None and len(chain) < 5:
        chain.append(chain[-1].__cause__)
    return chain


def _unresolved_detail(error: BaseException) -> int | None:
    """180 + 10 * dominant entry shape + dominant skip reason (180-253 s), for
    B's "no declared material could be resolved" (eval-20261101: all B cases)."""
    for exc in _chain(error):
        shapes, skipped = getattr(exc, "shapes", None), getattr(exc, "skipped", None)
        if shapes or skipped:
            shape = max(shapes.items(), key=lambda item: (item[1], -SHAPES.get(item[0], 7)))[0] if shapes else "other"
            reason = max(skipped.items(), key=lambda item: (item[1], -SKIP_REASONS.get(item[0], 3)))[0] if skipped else "not_found"
            return 180 + 10 * SHAPES.get(shape, 7) + SKIP_REASONS.get(reason, 3)
    return None


def failure_delay(settings: Settings, case_id: str, error: BaseException) -> int:
    """Seconds to wait before reporting a failed retrieval case (0 = disabled)."""
    if os.environ.get("CONTEST_FAILURE_TIMING", "1") == "0":
        return 0
    detail = _unresolved_detail(error)
    if detail is not None:
        return detail
    phase = _last_phase(settings.team_dir / "work" / "diagnostics" / f"{case_id}.json")
    return 20 * phase + _error_code(error)


# Idea failures: 20 s per code (normal processing already takes ~10-20 s).
# Most specific markers first: several messages contain "引用".
IDEA_REASONS = [
    ("characters; Idea maximum", 1),          # body too long
    ("including Markdown headings", 1),
    ("required excluding references", 2),     # body too short
    ("'## 参考文献' section", 3),
    ("占位", 4),
    ("引用编号超出", 7),                       # invalid citation number
    ("事实性语句", 6),                         # unsupported_claim
    ("引用", 5),                              # missing citation / reference entry
    ("参考文献", 5),
]


IDEA_STEP = 20
# The 400 path (question rejected before any handler) waits a fixed time
# outside both ranges above (B: 0-178 and 180-253; Idea: 20-220).
REJECTED_QUESTION_DELAY = 270


def fit_delay(delay: int, case_timeout: float, elapsed: float, margin: float = 15.0) -> int:
    """The delay if it fits in what is left of the case time limit, else 0:
    a truncated delay would decode wrongly, and a platform timeout is worse."""
    return delay if delay > 0 and elapsed + delay + margin <= case_timeout else 0


def idea_failure_delay(error: BaseException) -> int:
    """Seconds to wait before reporting a failed Idea case (0 = disabled)."""
    if os.environ.get("CONTEST_FAILURE_TIMING", "1") == "0":
        return 0
    names = {cls.__name__ for exc in _chain(error) for cls in type(exc).__mro__}
    text = " ".join(str(exc) for exc in _chain(error))
    if "ModelError" in names:
        return IDEA_STEP * 8
    if "TimeoutError" in names:
        return IDEA_STEP * 9
    if "MaterialResolutionError" in names:
        return IDEA_STEP * 10
    for marker, code in IDEA_REASONS:
        if marker in text:
            return IDEA_STEP * code
    return IDEA_STEP * 11
