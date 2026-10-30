import pytest

from scripts.evaluate_survey import evaluate, evaluate_case


FACTS = [{"id": "method", "paper": "a.pdf", "topic": "method"},
         {"id": "result", "paper": "a.pdf", "topic": "result"},
         {"id": "limit", "paper": "b.pdf", "topic": "limit"}]


def annotation(**kwargs):
    result = {"covered_fact_ids": ["method"], "severe_error_ids": [],
              "claims": [{"supported": True}], "cited_papers": ["A.pdf"]}
    result.update(kwargs)
    return result


def test_few_correct_claims_do_not_imply_high_fact_coverage():
    result = evaluate_case(FACTS, annotation())
    assert result["supported_fraction_all_claims"] == 1
    assert result["fact_recall"] == pytest.approx(1 / 3)
    assert result["formula_score_on_supplied_gold"] == 5
    assert result["source_citation_coverage"] == 0.5
    assert result["fact_recall_by_topic"]["result"] == 0


def test_unknown_is_not_automatically_error_or_verified_support():
    result = evaluate_case(FACTS, annotation(claims=[{"supported": True}, {"supported": None}]))
    assert result["F"] == 0
    assert result["supported_fraction_all_claims"] == 0.5
    assert result["precision_among_verified_claims"] == 1
    assert result["claims"]["unknown"] == 1


def test_severe_error_penalty_is_distinct_from_omission():
    result = evaluate_case(FACTS, annotation(severe_error_ids=["e1", "e1"]))
    assert result["formula_score_on_supplied_gold"] == 2
    assert result["F"] == 1
    with pytest.raises(ValueError, match="absent from gold"):
        evaluate_case(FACTS, annotation(covered_fact_ids=["invented"]))


def test_missing_report_counts_zero_without_fabricated_claim_judgements():
    result = evaluate([{"case_id": "C-1", "facts": FACTS}], [])
    assert result["macro_formula_score"] == 0
    assert result["missing_annotations"] == ["C-1"]
    assert result["cases"][0]["supported_fraction_all_claims"] is None
