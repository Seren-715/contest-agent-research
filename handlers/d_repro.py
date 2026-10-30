"""D / Reproduction: delegate to the source-provenance pipeline."""

from __future__ import annotations

from typing import Any

from core.config import Settings
from core.llm import ModelClient
from d_adapter.pipeline import run_case


class ReproductionHandler:
    def handle(self, question: dict[str, Any], settings: Settings, client: ModelClient) -> dict[str, Any]:
        return run_case(question, settings, client)
