"""Local self-test for a generated D-class implementation.

Runs the platform's public sample requests through the case's `worker.py` with
the generated source copied verbatim as `implementation.py`, then validates the
internal result shape. This is the "reproduction gate" equivalent: a real
subprocess, real stdout/stderr, no simulated scores.

If a sample fails, the pipeline feeds the full error back to the same model and
asks for a complete repaired implementation; the source is never patched
locally.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from core.budget import Deadline
from core.config import Settings
from core.io import write_json


class SelftestError(RuntimeError):
    pass


@dataclass
class SampleResult:
    name: str
    stage: str
    ok: bool
    errors: list[str] = field(default_factory=list)
    skipped: bool = False
    stdout_tail: str = ""
    stderr_tail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "stage": self.stage,
            "ok": self.ok,
            "skipped": self.skipped,
            "errors": self.errors,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


@dataclass
class SelftestReport:
    case_id: str
    ok: bool
    samples: list[SampleResult]
    skipped: bool = False

    def error_text(self) -> str:
        lines: list[str] = []
        for sample in self.samples:
            if sample.ok or sample.skipped:
                continue
            lines.append(f"[{sample.name}] " + "; ".join(sample.errors))
        text = "\n".join(lines)
        return text[:4000]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "ok": self.ok,
            "skipped": self.skipped,
            "samples": [sample.to_dict() for sample in self.samples],
        }


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _samples_root(settings: Settings) -> Optional[Path]:
    candidates: list[Path] = []
    if settings.d_samples_root is not None:
        candidates.append(settings.d_samples_root)
    candidates.extend(
        [
            settings.d_materials_root / "公开样例请求",
            settings.d_materials_root.parent / "公开样例请求",
            Path("/app/d-class-samples/公开样例请求"),
            Path("vendor/d-class/公开样例请求"),
        ]
    )
    for path in candidates:
        if path.exists():
            return path
    return None


def _sample_paths(case_id: str, root: Path) -> list[Path]:
    paths: list[Path] = []
    base = root / f"{case_id}.json"
    if base.exists():
        paths.append(base)
    sub = root / case_id
    if sub.is_dir():
        paths.extend(sorted(sub.glob("*.json")))
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _prepare_sandbox(material_dir: Path, implementation: Path, sandbox: Path) -> None:
    if sandbox.exists():
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True)
    for name in ("worker.py", "artifacts.py", "font.py", "protocol.json"):
        source = material_dir / name
        if not source.exists():
            raise SelftestError(f"missing D material file: {source}")
        shutil.copy2(source, sandbox / name)
    shutil.copy2(implementation, sandbox / "implementation.py")


def _parse_stdout(stdout: str) -> dict[str, Any]:
    for line in reversed([item.strip() for item in stdout.splitlines() if item.strip()]):
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise SelftestError("worker did not print a JSON result object")


def _row_ids(rows: Any, label: str, errors: list[str]) -> list[str]:
    ids: list[str] = []
    if not isinstance(rows, list):
        errors.append(f"{label}: rankings is not a list")
        return ids
    for row in rows:
        if isinstance(row, dict):
            identifier, score = row.get("id"), row.get("score")
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            identifier, score = row[0], row[1]
        else:
            errors.append(f"{label}: row is not {{id,score}} or [id,score]")
            continue
        if not isinstance(identifier, str):
            errors.append(f"{label}: row id is not a string")
        else:
            ids.append(identifier)
        if not _is_num(score):
            errors.append(f"{label}: row score is not finite")
    return ids


def _normalize_rankings(rankings: Any, query_ids: list[str]) -> Optional[dict[str, Any]]:
    if isinstance(rankings, dict):
        return rankings
    if isinstance(rankings, list):
        # The protocol accepts [{query_id, rankings}] for dev rankings.
        if all(isinstance(item, dict) and "query_id" in item and "rankings" in item for item in rankings):
            return {str(item["query_id"]): item["rankings"] for item in rankings}
        # Or a complete array in dev query order.
        if len(rankings) == len(query_ids) and all(isinstance(item, list) for item in rankings):
            return dict(zip(query_ids, rankings))
    return None


def _validate_rankings(rankings: Any, doc_ids: list[str], query_ids: list[str], label: str, errors: list[str]) -> None:
    normalized = _normalize_rankings(rankings, query_ids)
    if normalized is None:
        errors.append(f"{label}: must be a query_id -> rankings object (or an accepted equivalent)")
        return
    for query_id in query_ids:
        rows = normalized.get(query_id)
        if rows is None:
            errors.append(f"{label}: missing query {query_id}")
            continue
        ids = _row_ids(rows, f"{label}/{query_id}", errors)
        if len(ids) != len(doc_ids) or set(ids) != set(doc_ids):
            errors.append(f"{label}/{query_id}: must cover every document exactly once")


def _validate_d01(request: dict[str, Any], result: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    data = request.get("data") or {}
    texts = data.get("texts")
    count = len(texts) if isinstance(texts, list) else 0
    baseline = result.get("baseline")
    if not isinstance(baseline, list) or len(baseline) != count:
        errors.append("baseline must be a list aligned with data.texts")
    elif not all(isinstance(item, str) for item in baseline):
        errors.append("baseline labels must be strings")

    candidate = result.get("candidate")
    if candidate is None:
        errors.append("candidate is null (target model not implemented)")
        return errors
    if not isinstance(candidate, list) or len(candidate) != count:
        errors.append("candidate must be a list aligned with data.texts")
        return errors
    if not all(isinstance(item, str) for item in candidate):
        errors.append("candidate labels must be strings")

    details = result.get("details")
    if not isinstance(details, dict):
        errors.append("details must be an object with the actual model")
        return errors
    classes, vocabulary = details.get("classes"), details.get("vocabulary")
    idf, weights = details.get("idf"), details.get("weights")
    if not isinstance(classes, list) or not classes or not all(isinstance(item, str) for item in classes):
        errors.append("details.classes must be a non-empty string list")
    if not isinstance(vocabulary, list) or not all(isinstance(item, str) for item in vocabulary):
        errors.append("details.vocabulary must be a string list")
    if isinstance(idf, list):
        if not isinstance(vocabulary, list) or len(idf) != len(vocabulary):
            errors.append("details.idf list must align with vocabulary")
    elif isinstance(idf, dict):
        if not isinstance(vocabulary, list) or any(item not in idf for item in vocabulary):
            errors.append("details.idf dict must contain every vocabulary token")
    else:
        errors.append("details.idf must be a list or token->value dict")
    if not isinstance(weights, list) or not isinstance(classes, list) or len(weights) != len(classes):
        errors.append("details.weights must be class_count x (vocabulary_size+1)")
    elif isinstance(vocabulary, list):
        for row in weights:
            if not isinstance(row, list) or len(row) != len(vocabulary) + 1:
                errors.append("details.weights row width must be vocabulary_size+1")
                break
            if not all(_is_num(value) for value in row):
                errors.append("details.weights must contain finite numbers")
                break
    return errors


def _validate_d02(request: dict[str, Any], result: dict[str, Any], stage: str) -> list[str]:
    errors: list[str] = []
    data = request.get("data") or {}
    documents = data.get("documents") or []
    doc_ids = [item.get("id") for item in documents if isinstance(item, dict) and isinstance(item.get("id"), str)]
    test_queries = data.get("test_queries") or []
    test_ids = [item.get("id") for item in test_queries if isinstance(item, dict) and isinstance(item.get("id"), str)]
    dev_queries = data.get("dev_queries") or []
    dev_ids = [item.get("id") for item in dev_queries if isinstance(item, dict) and isinstance(item.get("id"), str)]

    if stage == "rank":
        _validate_rankings(result.get("candidate"), doc_ids, test_ids, "candidate", errors)
        return errors

    _validate_rankings(result.get("baseline"), doc_ids, test_ids, "baseline", errors)
    candidate = result.get("candidate")
    if candidate is None:
        errors.append("candidate is null (target model not implemented)")
    else:
        _validate_rankings(candidate, doc_ids, test_ids, "candidate", errors)

    details = result.get("details")
    if not isinstance(details, dict):
        errors.append("details must contain selected and search")
        return errors
    selected = details.get("selected")
    if not isinstance(selected, dict) or not all(key in selected for key in ("k1", "b", "title_weight")):
        errors.append("details.selected must contain k1, b, title_weight")
    search = details.get("search")
    if not isinstance(search, list) or len(search) != 27:
        errors.append("details.search must contain exactly 27 grid rows")
        return errors
    for index, row in enumerate(search):
        if not isinstance(row, dict):
            errors.append(f"details.search[{index}] is not an object")
            continue
        config = row.get("config")
        if not isinstance(config, dict) or not all(key in config for key in ("k1", "b", "title_weight")):
            errors.append(f"details.search[{index}].config is incomplete")
        _validate_rankings(row.get("rankings"), doc_ids, dev_ids, f"details.search[{index}]", errors)
        for key in ("recall_at_5_percent", "mrr_at_10"):
            if not _is_num(row.get(key)):
                errors.append(f"details.search[{index}].{key} is not finite")
        group = row.get("group_metrics")
        if not isinstance(group, dict):
            errors.append(f"details.search[{index}].group_metrics is missing")
            continue
        for key in ("worst_mrr_at_10", "macro_mrr_at_10", "macro_recall_at_5_percent"):
            if not _is_num(group.get(key)):
                errors.append(f"details.search[{index}].group_metrics.{key} is not finite")
        if not isinstance(group.get("groups"), dict):
            errors.append(f"details.search[{index}].group_metrics.groups is missing")
    return errors


def _validate_d05_run(run: Any, request: dict[str, Any], label: str, errors: list[str]) -> None:
    data = request.get("data") or {}
    jobs = {item["id"]: item for item in data.get("jobs", []) if isinstance(item, dict) and "id" in item}
    flows = list((data.get("flow_weights") or {}).keys())
    horizon = data.get("horizon_ms")
    if not isinstance(run, dict):
        errors.append(f"{label}: must be a trace object")
        return
    trace, served, dropped = run.get("trace"), run.get("served_ms"), run.get("dropped")
    if not isinstance(trace, list):
        errors.append(f"{label}.trace must be a list")
        trace = []
    if not isinstance(served, dict):
        errors.append(f"{label}.served_ms must be an object")
        served = {}
    if not isinstance(dropped, list):
        errors.append(f"{label}.dropped must be a list")
        dropped = []

    trace_ids: list[str] = []
    for index, row in enumerate(trace):
        if not isinstance(row, dict):
            errors.append(f"{label}.trace[{index}] is not an object")
            continue
        for key in ("id", "server", "dispatch_ms", "setup_ms", "start_ms", "end_ms", "wait_ms"):
            if key not in row:
                errors.append(f"{label}.trace[{index}] missing {key}")
        identifier = row.get("id")
        if isinstance(identifier, str):
            trace_ids.append(identifier)
        for key in ("dispatch_ms", "setup_ms", "start_ms", "end_ms", "wait_ms"):
            if not _is_num(row.get(key)):
                errors.append(f"{label}.trace[{index}].{key} is not finite")
        job = jobs.get(identifier)
        if job and all(_is_num(row.get(key)) for key in ("start_ms", "end_ms", "wait_ms", "setup_ms")):
            if abs(row["start_ms"] - (row["dispatch_ms"] + row["setup_ms"])) > 1e-6:
                errors.append(f"{label}.trace[{index}] start != dispatch + setup")
            if abs(row["wait_ms"] - (row["start_ms"] - job["arrival_ms"])) > 1e-6:
                errors.append(f"{label}.trace[{index}] wait != start - arrival")
            if abs(row["end_ms"] - (row["start_ms"] + job["service_ms"])) > 1e-6:
                errors.append(f"{label}.trace[{index}] end != start + service")
            if _is_num(job.get("deadline_ms")) and row["start_ms"] >= job["deadline_ms"]:
                errors.append(f"{label}.trace[{index}] started at/after deadline")
            if _is_num(horizon) and row["end_ms"] > horizon:
                errors.append(f"{label}.trace[{index}] ends after horizon")

    dropped_ids: list[str] = []
    for index, row in enumerate(dropped):
        if not isinstance(row, dict):
            errors.append(f"{label}.dropped[{index}] is not an object")
            continue
        identifier = row.get("id")
        if isinstance(identifier, str):
            dropped_ids.append(identifier)
        if row.get("reason") not in ("deadline", "horizon"):
            errors.append(f"{label}.dropped[{index}].reason must be deadline|horizon")

    all_ids = trace_ids + dropped_ids
    if len(all_ids) != len(set(all_ids)):
        errors.append(f"{label}: a job appears more than once in trace/dropped")
    if set(all_ids) != set(jobs):
        errors.append(f"{label}: trace+dropped must cover every input job exactly once")
    if dropped_ids != sorted(dropped_ids):
        errors.append(f"{label}.dropped must be sorted by id")
    if set(served) != set(flows):
        errors.append(f"{label}.served_ms must contain every declared flow")
    for flow, value in served.items():
        if not _is_num(value) or value < 0:
            errors.append(f"{label}.served_ms[{flow}] must be a finite non-negative number")


def _validate_d05(request: dict[str, Any], result: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    _validate_d05_run(result.get("baseline"), request, "baseline", errors)
    candidate = result.get("candidate")
    if candidate is None:
        errors.append("candidate is null (target model not implemented)")
    else:
        _validate_d05_run(candidate, request, "candidate", errors)
    return errors


def validate(case_id: str, stage: str, request: dict[str, Any], result: dict[str, Any]) -> list[str]:
    if not isinstance(result, dict):
        return ["worker result is not a JSON object"]
    if case_id == "D-01":
        return _validate_d01(request, result)
    if case_id == "D-02":
        return _validate_d02(request, result, stage)
    if case_id == "D-05":
        return _validate_d05(request, result)
    return []


def run_sample(
    case_id: str,
    implementation: Path,
    sample_path: Path,
    material_dir: Path,
    work_dir: Path,
    timeout: float,
) -> SampleResult:
    try:
        sample = json.loads(sample_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return SampleResult(sample_path.stem, "unknown", False, [f"cannot read sample: {exc}"])
    stage = str(sample.get("stage", "run"))
    payload = sample.get("payload") if isinstance(sample.get("payload"), dict) else {}
    sandbox = work_dir / "selftest" / f"{case_id}-{sample_path.stem}"
    try:
        _prepare_sandbox(material_dir, implementation, sandbox)
    except Exception as exc:
        return SampleResult(sample_path.stem, stage, False, [str(exc)])

    seconds = max(1, int(timeout))
    # Windows has no SIGALRM; the parent subprocess timeout still bounds it.
    code = (
        "import runpy,signal; "
        f"getattr(signal, 'alarm', lambda seconds: None)({seconds}); "
        "runpy.run_path('worker.py', run_name='__main__')"
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONHASHSEED": "42",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-S", "-c", code],
            cwd=str(sandbox),
            input=json.dumps(sample, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=timeout + 2,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return SampleResult(sample_path.stem, stage, False, [f"worker timed out after {timeout:.0f}s"])
    except Exception as exc:
        return SampleResult(sample_path.stem, stage, False, [f"worker failed to start: {exc}"])

    if proc.returncode != 0:
        return SampleResult(
            sample_path.stem,
            stage,
            False,
            [f"worker exit code {proc.returncode}"],
            stdout_tail=proc.stdout[-1000:],
            stderr_tail=proc.stderr[-2000:],
        )
    try:
        result = _parse_stdout(proc.stdout)
    except Exception as exc:
        return SampleResult(
            sample_path.stem,
            stage,
            False,
            [str(exc)],
            stdout_tail=proc.stdout[-1000:],
            stderr_tail=proc.stderr[-2000:],
        )
    errors = validate(case_id, stage, payload, result)
    return SampleResult(
        sample_path.stem,
        stage,
        not errors,
        errors,
        stdout_tail=proc.stdout[-1000:],
        stderr_tail=proc.stderr[-2000:],
    )


def run_selftest(
    case_id: str,
    implementation: Path,
    settings: Settings,
    work_dir: Path,
    deadline: Deadline,
    max_samples: Optional[int] = None,
) -> SelftestReport:
    material_dir = settings.d_materials_root / case_id
    if not material_dir.exists():
        raise SelftestError(f"D material directory not found: {material_dir}")
    root = _samples_root(settings)
    if root is None:
        report = SelftestReport(case_id, True, [], skipped=True)
        write_json(work_dir / "selftest.json", report.to_dict())
        return report
    paths = _sample_paths(case_id, root)
    if not paths:
        report = SelftestReport(case_id, True, [], skipped=True)
        write_json(work_dir / "selftest.json", report.to_dict())
        return report
    if max_samples is not None:
        paths = paths[: max(1, max_samples)]

    results: list[SampleResult] = []
    reserve = settings.d_formal_reserve + 5.0
    for path in paths:
        remaining = deadline.remaining()
        if remaining < reserve:
            results.append(
                SampleResult(
                    path.stem,
                    "run",
                    False,
                    ["self-test stopped to reserve formal experiment time"],
                    skipped=True,
                )
            )
            continue
        timeout = min(settings.d_selftest_timeout, max(3.0, remaining - reserve - 2.0))
        sample = run_sample(case_id, implementation, path, material_dir, work_dir, timeout)
        results.append(sample)
        # One concrete failure is enough to ask the model for a complete repair;
        # running every boundary sample would eat the formal-experiment budget.
        if not sample.ok and not sample.skipped:
            break

    ok = all(sample.ok for sample in results if not sample.skipped)
    report = SelftestReport(case_id, ok, results)
    write_json(work_dir / "selftest.json", report.to_dict())
    return report
