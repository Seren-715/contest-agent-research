"""Runtime configuration for the contest agent.

All paths are resolved against CONTEST_WORKSPACE_ROOT. The platform mounts the
team workspace at /output in the official run, so set CONTEST_WORKSPACE_ROOT=/output
(or leave the default and let the platform place the process there).
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


CASE_TIMEOUTS = {
    "Chat": 540.0, "Literature_Retrieval": 540.0,
    "Survey": 900.0, "Reproduction": 900.0, "Idea": 900.0,
}
D_EXECUTION_TIMEOUT = 840.0
D_RPC_TIMEOUT = 870.0
D_CLIENT_TIMEOUT = 880.0

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _agent_config() -> dict[str, Any]:
    """Optional baked-in config for the official image.

    Runtime environment variables always win. The contest guide asks teams to
    write the gateway URL/key/model into the image; this file is that fallback.
    Never commit the real file.
    """
    path = Path(os.environ.get("AGENT_CONFIG_PATH", "/app/agent_config.json"))
    try:
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
    except Exception:
        pass
    return {}


@dataclass(frozen=True)
class Settings:
    team: str
    workspace_root: Path
    data_dir: Path
    team_dir: Path
    expert_dir: Path
    figure_dir: Path
    llm_base_url: Optional[str]
    llm_api_key: Optional[str]
    llm_model: Optional[str]
    mock_llm: bool
    request_timeout: float
    case_budget: float
    d_client: Path
    d_materials_root: Path
    d_samples_root: Optional[Path] = None
    d_selftest: bool = True
    d_repair_attempts: int = 1
    d_selftest_timeout: float = 8.0
    d_selftest_max_samples: int = 2
    d_max_tokens: int = 64000
    d_formal_reserve: float = 70.0
    case_timeout: Optional[float] = None
    finalize_reserve: float = 10.0

    def ensure_dirs(self) -> None:
        for path in (self.team_dir, self.expert_dir, self.figure_dir):
            path.mkdir(parents=True, exist_ok=True)


def _positive_seconds(value: float, label: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be positive finite seconds")
    return value


def case_timeout_seconds(function: str, settings: Optional[Settings] = None) -> float:
    """Outer /solve limit; a deployment override may shorten, never extend it."""
    settings = settings or load_settings()
    official = CASE_TIMEOUTS[function]
    if settings.case_timeout is None:
        return official
    return min(official, _positive_seconds(settings.case_timeout, "case timeout"))


def case_budget_seconds(function: str, settings: Optional[Settings] = None) -> float:
    """Internal work leaves time for final validation, file writes and response."""
    settings = settings or load_settings()
    limit = min(case_timeout_seconds(function, settings),
                _positive_seconds(settings.case_budget, "case budget"))
    reserve = settings.finalize_reserve
    if not math.isfinite(reserve) or reserve < 0:
        raise ValueError("finalize reserve must be non-negative finite seconds")
    return max(0.0, limit - reserve)


def load_settings() -> Settings:
    # The D client and the platform judge use RESEARCH_WORKSPACE_TEAM as the
    # team identifier; prefer it over TEAM_ID (which may be a platform user id).
    team = (
        _env("RESEARCH_WORKSPACE_TEAM")
        or _env("TEAM_ID")
        or _env("CONTEST_TEAM")
        or "team"
    )
    configured_root = _env("CONTEST_WORKSPACE_ROOT")
    if configured_root:
        workspace_root = Path(configured_root).expanduser().resolve()
    elif Path("/output").exists():
        # The contest platform mounts the writable result workspace at /output.
        workspace_root = Path("/output")
    else:
        workspace_root = Path(".").resolve()
    data_dir = workspace_root / "data"
    baked = _agent_config()
    d_materials_root = Path(_env("CONTEST_D_MATERIALS_ROOT", "/data/experiments"))
    d_samples_root = Path(_env("CONTEST_D_SAMPLES_ROOT", str(d_materials_root / "公开样例请求")))
    return Settings(
        team=team,
        workspace_root=workspace_root,
        data_dir=data_dir,
        team_dir=data_dir / team,
        expert_dir=data_dir / "expert",
        figure_dir=data_dir / "figure" / team,
        llm_base_url=_env("PLATFORM_LLM_URL") or baked.get("base_url") or _env("LLM_BASE_URL"),
        llm_api_key=_env("PLATFORM_API_KEY") or baked.get("api_key") or _env("LLM_API_KEY"),
        llm_model=(
            _env("AGENT_MODEL_NAME")
            or _env("PLATFORM_LLM_MODEL")
            or baked.get("model")
            or _env("LLM_MODEL")
        ),
        mock_llm=_env("CONTEST_MOCK_LLM", "0") == "1",
        request_timeout=_positive_seconds(float(_env("CONTEST_REQUEST_TIMEOUT", "300") or "300"), "request timeout"),
        case_budget=_positive_seconds(float(_env("CONTEST_CASE_BUDGET", "900") or "900"), "case budget"),
        d_client=Path(_env("CONTEST_D_CLIENT", "/data/experiments/run_experiment.py")),
        d_materials_root=d_materials_root,
        d_samples_root=d_samples_root,
        d_selftest=_env("CONTEST_D_SELFTEST", "1") != "0",
        d_repair_attempts=int(_env("CONTEST_D_REPAIR_ATTEMPTS", "1") or "1"),
        d_selftest_timeout=float(_env("CONTEST_D_SELFTEST_TIMEOUT", "8") or "8"),
        d_selftest_max_samples=int(_env("CONTEST_D_SELFTEST_MAX_SAMPLES", "2") or "2"),
        d_max_tokens=int(_env("CONTEST_D_MAX_TOKENS", "64000") or "64000"),
        d_formal_reserve=float(_env("CONTEST_D_FORMAL_RESERVE", "70") or "70"),
        case_timeout=(float(_env("CONTEST_CASE_TIMEOUT_MS")) / 1000.0
                      if _env("CONTEST_CASE_TIMEOUT_MS") is not None else None),
        finalize_reserve=float(_env("CONTEST_FINALIZE_RESERVE", "10") or "10"),
    )
