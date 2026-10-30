"""Solve one assigned question locally and print the result JSON.

Usage:
    uv run python scripts/solve_one.py path/to/assigned/A-01.json

This is a development tool. The official platform calls POST /solve.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import load_settings  # noqa: E402
from core.io import write_result  # noqa: E402
from core.llm import ModelClient  # noqa: E402
from handlers.a_chat import ChatHandler  # noqa: E402
from handlers.b_retrieval import RetrievalHandler  # noqa: E402
from handlers.c_survey import SurveyHandler  # noqa: E402
from handlers.d_repro import ReproductionHandler  # noqa: E402
from handlers.e_idea import IdeaHandler  # noqa: E402

HANDLERS = {
    "Chat": ChatHandler(),
    "Literature_Retrieval": RetrievalHandler(),
    "Survey": SurveyHandler(),
    "Reproduction": ReproductionHandler(),
    "Idea": IdeaHandler(),
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("question", type=Path)
    args = parser.parse_args()

    settings = load_settings()
    settings.ensure_dirs()
    question = json.loads(args.question.read_text(encoding="utf-8"))
    function = question.get("function")
    if function not in HANDLERS:
        raise SystemExit(f"unsupported function: {function!r}")
    client = ModelClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout=settings.request_timeout,
        mock=settings.mock_llm,
    )
    result = HANDLERS[function].handle(question, settings, client)
    write_result(settings, question["case_id"], result)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
