"""Evaluate independently annotated C reports, without model-as-judge claims.

Gold: [{"case_id":"C-01", "facts":[{"id":"f1", "paper":"a.pdf", "topic":"method"}]}]
Annotations: [{"case_id":"C-01", "covered_fact_ids":["f1"], "severe_error_ids":[],
              "claims":[{"supported":true}, {"supported":null}], "cited_papers":["a.pdf"]}]
Covered facts must be checked against the source by the annotator. This script
does not infer entailment, discover hidden facts, or reproduce citation grading.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def evaluate_case(facts: list[dict[str, str]], annotation: dict[str, Any]) -> dict[str, Any]:
    ids = [fact["id"] for fact in facts]
    if not ids or any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("gold facts require nonempty unique ids")
    covered = set(annotation["covered_fact_ids"])
    if covered - set(ids):
        raise ValueError("covered fact is absent from gold")
    errors = annotation["severe_error_ids"]
    if any(not isinstance(value, str) or not value for value in errors):
        raise ValueError("severe errors require nonempty ids")
    claims = [claim["supported"] for claim in annotation["claims"]]
    if any(value is not None and not isinstance(value, bool) for value in claims):
        raise ValueError("claim support must be true, false or null")
    supported = sum(value is True for value in claims)
    rejected = sum(value is False for value in claims)
    unknown = sum(value is None for value in claims)
    cited = {name.lower() for name in annotation["cited_papers"]}
    papers = {fact["paper"].lower() for fact in facts}
    topics = {fact["topic"] for fact in facts}
    return {
        "covered": len(covered), "K": len(ids), "F": len(set(errors)),
        "fact_recall": len(covered) / len(ids),
        "formula_score_on_supplied_gold": max(0.0, min(15.0, 15.0 * len(covered) / len(ids) - 3 * len(set(errors)))),
        "claims": {"supported": supported, "unsupported": rejected, "unknown": unknown},
        "supported_fraction_all_claims": supported / len(claims) if claims else None,
        "precision_among_verified_claims": supported / (supported + rejected) if supported + rejected else None,
        "source_citation_coverage": len(papers & cited) / len(papers) if papers else None,
        "fact_recall_by_paper": {
            name: sum(fact["id"] in covered for fact in facts if fact["paper"].lower() == name)
            / sum(fact["paper"].lower() == name for fact in facts) for name in sorted(papers)
        },
        "fact_recall_by_topic": {
            name: sum(fact["id"] in covered for fact in facts if fact["topic"] == name)
            / sum(fact["topic"] == name for fact in facts) for name in sorted(topics)
        },
    }


def evaluate(gold: list[dict[str, Any]], annotations: list[dict[str, Any]]) -> dict[str, Any]:
    expected = {item["case_id"]: item["facts"] for item in gold}
    actual = {item["case_id"]: item for item in annotations}
    if not expected or len(expected) != len(gold) or len(actual) != len(annotations):
        raise ValueError("nonempty gold and unique case ids required")
    if actual.keys() - expected.keys():
        raise ValueError("annotation case is absent from gold")
    empty = {"covered_fact_ids": [], "severe_error_ids": [], "claims": [], "cited_papers": []}
    cases = [{"case_id": key, **evaluate_case(facts, actual.get(key, empty))} for key, facts in expected.items()]
    return {
        "scope": "human source-checked annotations and supplied gold; not hidden platform scores",
        "cases": cases,
        "macro_fact_recall": sum(case["fact_recall"] for case in cases) / len(cases),
        "macro_formula_score": sum(case["formula_score_on_supplied_gold"] for case in cases) / len(cases),
        "missing_annotations": [key for key in expected if key not in actual],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--annotations", required=True, type=Path)
    args = parser.parse_args()
    result = evaluate(json.loads(args.gold.read_text(encoding="utf-8")),
                      json.loads(args.annotations.read_text(encoding="utf-8")))
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
