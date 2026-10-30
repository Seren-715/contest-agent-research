"""Minimal gateway probe. Reads the same env vars as the agent and sends a
tiny chat completion. It never prints the API key.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import load_settings  # noqa: E402
from core.llm import ModelClient, ModelError  # noqa: E402


def main() -> int:
    settings = load_settings()
    missing = [
        name
        for name, value in (
            ("PLATFORM_LLM_URL", settings.llm_base_url),
            ("PLATFORM_API_KEY", settings.llm_api_key),
            ("AGENT_MODEL_NAME", settings.llm_model),
        )
        if not value
    ]
    if missing:
        print("missing:", ", ".join(missing))
        return 2
    client = ModelClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout=settings.request_timeout,
    )
    try:
        text = client.complete(
            [{"role": "user", "content": "只回复 OK"}],
            temperature=0.0,
            max_tokens=64,
        )
    except ModelError as exc:
        print("gateway failed:", exc)
        return 1
    print("gateway ok; model:", settings.llm_model, "reply:", text.strip()[:80])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
