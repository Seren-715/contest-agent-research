"""Offline paper-level evaluation against a complete, independently labelled gold.

Usage: python scripts/evaluate_retrieval.py --gold gold.json --predictions answers.json
Gold is a JSON list of {"case_id": "B-01", "relevant": ["a.pdf", ...]}.
Predictions are a list of normal B answers ({"case_id", "papers"}). Missing cases
score zero. This tool makes no calls and does not infer hidden platform labels.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable


def unique_names(names: Iterable[str]) -> list[str]:
    result = []
    seen = set()
    for name in names:
        if not isinstance(name, str) or not name:
            raise ValueError("filenames must be non-empty strings")
        key = name.lower()
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def evaluate_case(relevant: list[str], returned: list[str], cutoffs: tuple[int, ...] = (5, 20, 50)) -> dict[str, Any]:
    gold = set(unique_names(relevant))
    if not gold:
        raise ValueError("official formula requires a non-empty relevant set")
    ranked = unique_names(returned)
    hit_count = len(gold.intersection(ranked))
    false_count = len(set(ranked) - gold)
    return {
        "H": hit_count,
        "N": len(gold),
        "U": false_count,
        "returned": len(ranked),
        "paper_recall": hit_count / len(gold),
        "precision": hit_count / len(ranked) if ranked else 0.0,
        "official_score": max(0.0, min(15.0, 15.0 * hit_count / len(gold) - 0.75 * false_count)),
        "recall_at": {str(k): len(gold.intersection(ranked[:k])) / len(gold) for k in cutoffs},
    }


def evaluate(gold: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> dict[str, Any]:
    expected = {}
    for item in gold:
        case_id = item["case_id"]
        if case_id in expected:
            raise ValueError("duplicate gold case: " + case_id)
        expected[case_id] = item["relevant"]
    if not expected:
        raise ValueError("gold must contain cases")
    actual = {}
    for item in predictions:
        case_id = item["case_id"]
        if case_id in actual or case_id not in expected:
            raise ValueError("duplicate or unknown prediction case: " + case_id)
        actual[case_id] = [paper["filename"] for paper in item["papers"]]
    cases = [{"case_id": key, **evaluate_case(value, actual.get(key, []))} for key, value in expected.items()]
    return {
        "label_requirement": "complete relevant set; unlabelled returned papers count as U",
        "cases": cases,
        "macro_official_score": sum(item["official_score"] for item in cases) / len(cases),
        "macro_paper_recall": sum(item["paper_recall"] for item in cases) / len(cases),
        "missing_predictions": [key for key in expected if key not in actual],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(json.loads(args.gold.read_text(encoding="utf-8")), json.loads(args.predictions.read_text(encoding="utf-8")))
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
