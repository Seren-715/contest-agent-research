"""Local-only stand-ins for the official AgentHack SDK.

Inside `agenthack/base:1.1.1` the real `agenthack` package is installed and this
module is never used. Locally, it lets imports and unit tests run without the
base image. The real SDK owns /health, /solve, telemetry and @tool recording.
"""

from __future__ import annotations

from typing import Any, Callable, Optional


def tool(name: Optional[str] = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        return fn

    return decorator


class BaseAgent:
    async def solve(self, task_input: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class AgentRuntime:
    def __init__(self, agent: BaseAgent) -> None:
        self.agent = agent

    def run(self) -> None:
        raise SystemExit(
            "agenthack SDK is not installed locally. Use `uv run uvicorn app.main:app` "
            "for local HTTP testing, or build/run inside agenthack/base:1.1.1."
        )
