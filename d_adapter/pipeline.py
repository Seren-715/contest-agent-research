"""D-class end-to-end pipeline for one case.

Priority order is fixed: produce a complete model source, optionally run a very
short public self-test, then always attempt the platform's formal entry. The
formal run is the only path to a trusted receipt, so local self-test and repair
must never consume the time reserved for it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from core.budget import Deadline
from core.config import D_CLIENT_TIMEOUT, Settings, case_budget_seconds
from core.execution import check_active
from core.io import write_json
from core.llm import ModelClient
from d_adapter import gateway, selftest
from d_adapter.gateway import GeneratedSource, GenerationError
from tools import run_experiment_client


class DAdapterError(RuntimeError):
    pass


def _clear_stale_sources(work_dir: Path) -> None:
    """Do not let a source from an earlier run satisfy this session's provenance."""
    for pattern in (
        "implementation.py",
        "implementation-repair-*.py",
        "implementation-alt.py",
        "implementation-crosscheck.py",
        "generation-response.json",
        "generation-response-repair-*.json",
        "generation-response-alt.json",
        "generation-response-crosscheck.json",
        "generation-meta*.json",
    ):
        for path in work_dir.glob(pattern):
            try:
                path.unlink()
            except OSError:
                pass


# Seconds that must remain (beyond the formal reserve) to start a second
# independent candidate, and to ask the model to settle a disagreement.
CROSSCHECK_START_SECONDS = 300.0
CROSSCHECK_SETTLE_SECONDS = 180.0
# Run-to-run measurements legitimately differ between two correct sources.
_TIMING_KEY = re.compile(r"time|elapsed|latency|duration|seconds|_ms$|_s$", re.IGNORECASE)


def _canonical(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return float("%.9g" % value)
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items() if not _TIMING_KEY.search(str(key))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return repr(value)


def _differences(left: Any, right: Any, path: str = "$", limit: int = 12) -> list[str]:
    """Paths where two run_experiment outputs differ (timing keys ignored)."""
    out: list[str] = []

    def walk(a: Any, b: Any, where: str) -> None:
        if len(out) >= limit:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b), key=str):
                if key not in a or key not in b:
                    out.append(f"{where}.{key}: 只在实现{'A' if key in a else 'B'}中出现")
                else:
                    walk(a[key], b[key], f"{where}.{key}")
            return
        if isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                out.append(f"{where}: 长度 A={len(a)} B={len(b)}")
            for index, (x, y) in enumerate(zip(a, b)):
                walk(x, y, f"{where}[{index}]")
            return
        if a != b:
            show = lambda v: json.dumps(v, ensure_ascii=False)[:160]  # noqa: E731
            out.append(f"{where}: A={show(a)} B={show(b)}")

    walk(_canonical(left), _canonical(right), path)
    return out


def _sample_outputs(report: Any) -> Optional[dict[str, Any]]:
    if report is None or not report.ok or report.skipped:
        return None
    outputs = {sample.name: sample.result for sample in report.samples if not sample.skipped}
    return outputs or None


def _crosscheck(question: dict[str, Any], settings: Settings, client: ModelClient, case_id: str,
                materials: Path, work_dir: Path, deadline: Deadline, formal_reserve: float,
                generated: GeneratedSource, report: Any) -> tuple[GeneratedSource, Any, dict[str, Any]]:
    """Second independent source from the same round materials; if the two
    disagree on the round's samples, the model settles it against the
    materials. Task-agnostic: nothing here knows any task's method."""
    info: dict[str, Any] = {"status": "not_run"}
    first = _sample_outputs(report)
    if first is None:
        info["status"] = "skipped_no_passing_samples"
        return generated, report, info
    if deadline.remaining() < formal_reserve + CROSSCHECK_START_SECONDS:
        info["status"] = "skipped_budget"
        return generated, report, info
    task_input = str(question.get("input", ""))
    try:
        alt = gateway.generate(client, case_id, task_input, materials, work_dir, deadline,
                               max_tokens=settings.d_max_tokens, reserve=formal_reserve,
                               name="alt", temperature=0.6)
        alt_report = selftest.run_selftest(case_id, alt.path, settings, work_dir / "alt", deadline,
                                           max_samples=settings.d_selftest_max_samples)
    except Exception as exc:
        check_active()
        info.update(status="alt_failed", error=str(exc)[:300])
        return generated, report, info
    second = _sample_outputs(alt_report)
    if second is None:
        info["status"] = "alt_selftest_failed"
        return generated, report, info
    diffs: list[str] = []
    for name in sorted(set(first) & set(second)):
        diffs.extend(f"[{name}] {line}" for line in _differences(first[name], second[name]))
    info["differences"] = diffs[:12]
    if not diffs:
        info["status"] = "agree"
        return generated, report, info
    if deadline.remaining() < formal_reserve + CROSSCHECK_SETTLE_SECONDS:
        info["status"] = "disagree_no_budget"
        return generated, report, info
    try:
        settled = gateway.adjudicate(
            client, case_id, task_input, materials, work_dir,
            [("A", generated.path.read_text(encoding="utf-8")), ("B", alt.path.read_text(encoding="utf-8"))],
            "\n".join(diffs[:12]), deadline, max_tokens=settings.d_max_tokens, reserve=formal_reserve,
        )
        settled_report = selftest.run_selftest(case_id, settled.path, settings, work_dir / "crosscheck",
                                               deadline, max_samples=settings.d_selftest_max_samples)
    except Exception as exc:
        check_active()
        info.update(status="settle_failed", error=str(exc)[:300])
        return generated, report, info
    if _sample_outputs(settled_report) is None:
        info["status"] = "settled_selftest_failed"
        return generated, report, info
    info["status"] = "settled"
    return settled, settled_report, info


def _write_diagnostics(work_dir: Path, payload: dict[str, Any]) -> None:
    check_active()
    try:
        write_json(work_dir / "pipeline.json", payload)
    except Exception:
        pass


def _selftest_summary(report: Any) -> dict[str, Any]:
    if report is None:
        return {"ran": False}
    return {
        "ran": True,
        "ok": bool(report.ok),
        "skipped": bool(report.skipped),
        "samples": [
            {
                "name": sample.name,
                "stage": sample.stage,
                "ok": sample.ok,
                "skipped": sample.skipped,
                "errors": sample.errors[:3],
            }
            for sample in report.samples
        ],
    }


def run_case(question: dict[str, Any], settings: Settings, client: ModelClient) -> dict[str, Any]:
    check_active()
    case_id = str(question.get("case_id", "")).strip()
    if not case_id:
        raise DAdapterError("D question is missing case_id")

    materials = settings.d_materials_root / case_id
    if not materials.exists():
        raise DAdapterError(
            f"D materials not found for {case_id}: {materials}. "
            "The platform mounts them under /data/experiments/<case_id>/ in the official run."
        )

    work_dir = settings.team_dir / "work" / case_id
    work_dir.mkdir(parents=True, exist_ok=True)
    _clear_stale_sources(work_dir)
    deadline = Deadline(case_budget_seconds("Reproduction", settings))
    # Short local-test budgets may be smaller than the production reserve.
    formal_reserve = min(settings.d_formal_reserve, max(0.0, deadline.seconds - 5.0))

    generated: GeneratedSource | None = None
    repair_attempt = 0
    response_path = work_dir / "generation-response.json"

    try:
        generated = gateway.generate(
            client,
            case_id,
            str(question.get("input", "")),
            materials,
            work_dir,
            deadline,
            max_tokens=settings.d_max_tokens,
            reserve=formal_reserve,
        )
    except GenerationError as exc:
        # Truncated or multi-version responses must be regenerated, not patched.
        if not response_path.exists() or deadline.remaining() <= settings.d_formal_reserve + 15:
            raise
        try:
            previous = json.loads(response_path.read_text(encoding="utf-8"))
            repair_attempt = 1
            generated = gateway.repair(
                client,
                case_id,
                str(question.get("input", "")),
                materials,
                work_dir,
                previous,
                f"source extraction failed: {exc}",
                deadline,
                attempt=repair_attempt,
                max_tokens=settings.d_max_tokens,
                reserve=formal_reserve,
            )
        except Exception:
            raise exc

    # Self-test is advisory. Run at most a couple of public samples and stop at
    # the first failure; only repair when there is comfortably enough time left.
    report = None
    if settings.d_selftest and deadline.remaining() > settings.d_formal_reserve + 10:
        try:
            report = selftest.run_selftest(
                case_id,
                generated.path,
                settings,
                work_dir,
                deadline,
                max_samples=settings.d_selftest_max_samples,
            )
        except Exception:
            report = None

        while (
            report is not None
            and not report.ok
            and repair_attempt < settings.d_repair_attempts
            and deadline.remaining() > formal_reserve + 5
        ):
            repair_attempt += 1
            try:
                previous = json.loads(generated.response_path.read_text(encoding="utf-8"))
                repaired = gateway.repair(
                    client,
                    case_id,
                    str(question.get("input", "")),
                    materials,
                    work_dir,
                    previous,
                    report.error_text(),
                    deadline,
                    attempt=repair_attempt,
                    max_tokens=settings.d_max_tokens,
                    reserve=formal_reserve,
                )
            except Exception:
                # A failed repair must not consume the formal-experiment budget.
                break
            generated = repaired
            try:
                report = selftest.run_selftest(
                    case_id,
                    generated.path,
                    settings,
                    work_dir,
                    deadline,
                    max_samples=settings.d_selftest_max_samples,
                )
            except Exception:
                report = None

    if generated is None:
        raise DAdapterError(f"no complete model-generated source for {case_id}")

    crosscheck: dict[str, Any] = {"status": "disabled"}
    if settings.d_selftest and settings.d_crosscheck:
        generated, report, crosscheck = _crosscheck(
            question, settings, client, case_id, materials, work_dir, deadline, formal_reserve,
            generated, report,
        )

    _write_diagnostics(
        work_dir,
        {
            "case_id": case_id,
            "budget_seconds": settings.case_budget,
            "remaining_before_formal": round(deadline.remaining(), 1),
            "repair_attempts": repair_attempt,
            "source_sha256": generated.sha256,
            "selftest": _selftest_summary(report),
            "crosscheck": crosscheck,
        },
    )

    # The official client does the source-credential exchange and the trusted run.
    # Attempt it only while time remains; never replay an unknown formal result.
    check_active()
    formal_timeout = deadline.timeout(D_CLIENT_TIMEOUT, label="formal experiment")
    try:
        submission = run_experiment_client(
            case_id,
            str(generated.path),
            timeout_seconds=formal_timeout,
        )
    except Exception as exc:
        _write_diagnostics(
            work_dir,
            {
                "case_id": case_id,
                "budget_seconds": settings.case_budget,
                "remaining_before_formal": round(deadline.remaining(), 1),
                "formal_timeout": round(formal_timeout, 1),
                "formal_error": str(exc)[:800],
                "source_sha256": generated.sha256,
                "selftest": _selftest_summary(report),
            },
        )
        raise
    return submission
