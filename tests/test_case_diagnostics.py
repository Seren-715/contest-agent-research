import json

import pytest

from core.solve import SolveError, solve_task


def test_failure_receipt_distinguishes_material_failure_without_credentials(settings):
    with pytest.raises(SolveError):
        solve_task({"case_id": "C-01", "function": "Survey", "input": "Review",
                    "materials": ["data/论文库/missing.pdf"]}, settings)
    path = settings.team_dir / "work" / "diagnostics" / "C-01.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    assert receipt["events"][0]["gateway_key_configured"] is False
    assert receipt["events"][-1]["error_type"] == "MaterialResolutionError"
    assert receipt["events"][-1]["reason"] == "declared material not found"
    assert "model_generation" not in [event["phase"] for event in receipt["events"]]


def test_phase_receipt_never_saves_config_values(settings):
    from dataclasses import replace
    secret = "sensitive-test-value"
    configured = replace(settings, llm_api_key=secret, llm_base_url="https://private-test.invalid", llm_model="test")
    with pytest.raises(SolveError):
        solve_task({"case_id": "C-01", "function": "Survey", "input": "Review",
                    "materials": ["missing.pdf"]}, configured)
    text = (settings.team_dir / "work" / "diagnostics" / "C-01.json").read_text(encoding="utf-8")
    assert secret not in text and "private-test.invalid" not in text
    assert json.loads(text)["events"][0]["gateway_key_configured"] is True
