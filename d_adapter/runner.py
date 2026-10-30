"""Call the platform-mounted D-class client.

Official path: /data/experiments/run_experiment.py. The client performs the
source-credential exchange and writes the final submission + figures. Never
replace it with a cached/local copy in the official run.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from core.config import D_CLIENT_TIMEOUT, Settings
from core.execution import check_active, current_execution


class RunnerError(RuntimeError):
    pass


def _parse_stdout(stdout: str) -> dict[str, Any]:
    for line in reversed([line.strip() for line in stdout.splitlines() if line.strip()]):
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "case_id" in value and "status" in value:
            return value
    raise RunnerError(f"client did not print a submission JSON object: {stdout[-1000:]!r}")


def run_platform(
    case_id: str,
    implementation: Path,
    settings: Settings,
    *,
    timeout: float,
) -> dict[str, Any]:
    check_active()
    if not math.isfinite(timeout) or timeout <= 0:
        raise TimeoutError("formal experiment has no remaining time")
    timeout = min(timeout, D_CLIENT_TIMEOUT)
    client = settings.d_client
    if not client.exists():
        raise RunnerError(
            f"D client not found: {client}. In local dev set CONTEST_D_CLIENT to the extracted "
            "run_experiment.py; in the official run use /data/experiments/run_experiment.py."
        )
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("RESEARCH_RESULT_ROOT", str(settings.workspace_root))
    env.setdefault("RESEARCH_WORKSPACE_TEAM", settings.team)
    # The current contest protocol is layered-d-v3 with source provenance.
    # The platform normally injects these; defaults keep local and official runs
    # on the same client path instead of silently falling back to the old flow.
    env.setdefault("RESEARCH_LAYERED_D", "layered-d-v3")
    env.setdefault("RESEARCH_D_SOURCE_PROVENANCE", "d-model-source-v1")
    # The agent is processing this exact case; a stale value injected at
    # container start must not make the client reject the active case.
    env["RESEARCH_EXPERIMENT_CASE"] = case_id
    control = current_execution()
    proc = None
    try:
        # Launch and registration must be indivisible with respect to cancel:
        # an unregistered client could otherwise write after the fallback.
        with control.publication() if control is not None else nullcontext():
            check_active()
            proc = subprocess.Popen(
                [sys.executable, str(client), "--case", case_id, "--implementation", str(implementation)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=env,
            )
            if control is not None:
                control.register_process(proc)
        check_active()
        stdout, stderr = proc.communicate(timeout=timeout)
        check_active()
    except subprocess.TimeoutExpired as exc:
        if proc is not None:
            proc.kill()
            proc.communicate()
        raise RunnerError(f"D client timed out after {timeout:.0f}s") from exc
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()
        if proc is not None and control is not None:
            control.unregister_process(proc)
    if proc.returncode != 0:
        raise RunnerError(
            f"D client failed (exit={proc.returncode}): {stderr.strip() or stdout.strip()}"
        )
    return _parse_stdout(stdout)
