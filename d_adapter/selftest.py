"""Local self-test for a generated D-class implementation.

Runs this round's sample request (the case's own `sample.json`, plus mounted
sample requests written for exactly this round's protocol) through the case's
`worker.py` with the generated source copied verbatim as `implementation.py`.
Checks are task-agnostic: the worker must exit cleanly and print a non-empty
JSON object. Formal tasks may differ from practice tasks, and the rules forbid
task-specific semantic validators in the image (D rules update).

If a sample fails, the pipeline feeds the full error back to the same model and
asks for a complete repaired implementation; the source is never patched
locally.
"""

from __future__ import annotations

import json
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
            # The traceback is the repair signal: send it to the model too.
            if sample.stderr_tail.strip():
                lines.append("stderr:\n" + sample.stderr_tail.strip())
        text = "\n".join(lines)
        return text[-4000:] if len(text) > 4000 else text

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "ok": self.ok,
            "skipped": self.skipped,
            "samples": [sample.to_dict() for sample in self.samples],
        }


def _samples_root(settings: Settings) -> Optional[Path]:
    candidates: list[Path] = []
    if settings.d_samples_root is not None:
        candidates.append(settings.d_samples_root)
    # Only the round's own mount (or an explicit setting): practice requests
    # baked into the image may not match the formal task.
    candidates.extend(
        [
            settings.d_materials_root / "公开样例请求",
            settings.d_materials_root.parent / "公开样例请求",
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


def validate(case_id: str, stage: str, request: dict[str, Any], result: Any) -> list[str]:
    """Task-agnostic checks only (D rules update: no semantic validators in
    the image). The official worker already rejects NaN/Infinity output; the
    result must be a non-empty JSON object. Whether its fields follow the
    round's protocol is for the model to implement from the materials."""
    if not isinstance(result, dict):
        return ["worker result is not a JSON object"]
    if not result:
        return ["worker result is an empty JSON object"]
    return []


def _round_samples(case_id: str, material_dir: Path, settings: Settings) -> list[tuple[str, dict[str, Any]]]:
    """Worker messages for this round: the case's own sample.json first, then
    any mounted sample requests written for exactly this round's protocol."""
    try:
        protocol = json.loads((material_dir / "protocol.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    samples: list[tuple[str, dict[str, Any]]] = []
    try:
        own = json.loads((material_dir / "sample.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        own = None
    if isinstance(own, dict) and isinstance(own.get("data"), dict):
        payload = dict(own)
        payload.setdefault("protocol", protocol)
        samples.append(("sample", {"stage": "rank" if payload.get("phase") == "rank" else "run",
                                   "payload": payload}))
    root = _samples_root(settings)
    for path in _sample_paths(case_id, root) if root is not None else []:
        try:
            message = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        payload = message.get("payload") if isinstance(message, dict) else None
        # A request without this exact protocol belongs to another task version.
        if isinstance(payload, dict) and payload.get("protocol") == protocol and payload.get("case_id") == case_id:
            samples.append((path.stem, message))
    return samples


def run_sample(
    case_id: str,
    implementation: Path,
    name: str,
    sample: dict[str, Any],
    material_dir: Path,
    work_dir: Path,
    timeout: float,
) -> SampleResult:
    stage = str(sample.get("stage", "run"))
    payload = sample.get("payload") if isinstance(sample.get("payload"), dict) else {}
    sandbox = work_dir / "selftest" / f"{case_id}-{name}"
    try:
        _prepare_sandbox(material_dir, implementation, sandbox)
    except Exception as exc:
        return SampleResult(name, stage, False, [str(exc)])

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
        return SampleResult(name, stage, False, [f"worker timed out after {timeout:.0f}s"])
    except Exception as exc:
        return SampleResult(name, stage, False, [f"worker failed to start: {exc}"])

    if proc.returncode != 0:
        return SampleResult(
            name,
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
            name,
            stage,
            False,
            [str(exc)],
            stdout_tail=proc.stdout[-1000:],
            stderr_tail=proc.stderr[-2000:],
        )
    errors = validate(case_id, stage, payload, result)
    return SampleResult(
        name,
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
    paths = _round_samples(case_id, material_dir, settings)
    if not paths:
        report = SelftestReport(case_id, True, [], skipped=True)
        write_json(work_dir / "selftest.json", report.to_dict())
        return report
    if max_samples is not None:
        paths = paths[: max(1, max_samples)]

    results: list[SampleResult] = []
    reserve = settings.d_formal_reserve + 5.0
    for name, message in paths:
        remaining = deadline.remaining()
        if remaining < reserve:
            results.append(
                SampleResult(
                    name,
                    "run",
                    False,
                    ["self-test stopped to reserve formal experiment time"],
                    skipped=True,
                )
            )
            continue
        timeout = min(settings.d_selftest_timeout, max(3.0, remaining - reserve - 2.0))
        sample = run_sample(case_id, implementation, name, message, material_dir, work_dir, timeout)
        results.append(sample)
        # One concrete failure is enough to ask the model for a complete repair;
        # running every boundary sample would eat the formal-experiment budget.
        if not sample.ok and not sample.skipped:
            break

    ok = all(sample.ok for sample in results if not sample.skipped)
    report = SelftestReport(case_id, ok, results)
    write_json(work_dir / "selftest.json", report.to_dict())
    return report
