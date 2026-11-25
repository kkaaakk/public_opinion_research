"""Budget contracts for final report synthesis."""

import asyncio

import pytest
from sdk_model_support import Reply, install_sequence

from open_deep_research.budget import can_spend_model_call, remaining_model_calls
from open_deep_research.configuration import Configuration
from open_deep_research.workflow import reporting
from open_deep_research.workflow.python_state import apply_patch


@pytest.mark.parametrize(
    "budget_enabled,limit,reserve,used,expected",
    [
        (True, 10, True, 7, 2),
        (True, 10, False, 7, 3),
        (True, 10, True, 9, 0),
        (True, 10, True, 12, 0),
        (False, 1, True, 7, None),
        (True, None, True, 7, None),
    ],
)
def test_remaining_model_calls_matches_single_call_guard(
    budget_enabled, limit, reserve, used, expected
):
    config = Configuration(
        budget_enabled=budget_enabled,
        max_model_calls=limit,
        reserve_final_report_call=reserve,
    )
    usage = {"model_calls": used}

    assert (
        remaining_model_calls(config, usage, reserve_final_report_call=True) == expected
    )
    assert can_spend_model_call(config, usage, reserve_final_report_call=True) == (
        expected is None or expected > 0
    )
    assert remaining_model_calls(config, usage) == (
        None if expected is None else max(0, limit - used)
    )


def test_reporting_uses_the_reserved_call_and_remaining_output_budget(monkeypatch):
    requests = install_sequence(monkeypatch, Reply("# Final report", 11, 7))
    state = {
        "workflow": {"brief": "Investigate brand risk"},
        "agents": {"public_signal": {"report": "collected public evidence"}},
        "runtime": {"budget": {"model_calls": 9, "output_tokens": 20}},
    }
    config = {
        "configurable": {
            "final_report_model": "openai:fixture-model",
            "final_report_model_max_tokens": 200,
            "budget_enabled": True,
            "max_model_calls": 10,
            "max_output_tokens": 120,
            "reserve_final_report_call": True,
        }
    }

    result = asyncio.run(reporting.generate_final_report(state, config))
    assert result["runtime"]["budget"]["model_calls"] == 1
    [request] = requests
    assert request.max_tokens == 100
    assert "collected public evidence" in request.prompt
    apply_patch(state, result, source="generate_final_report")
    assert state["report"]["final"].startswith("# Final report")
    assert state["runtime"]["budget"]["model_calls"] == 10
    assert state["runtime"]["budget"]["output_tokens"] == 27
