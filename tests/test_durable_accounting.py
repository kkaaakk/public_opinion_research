"""Regression contracts for replay-safe errors and physical usage authority."""

import pytest
from agents.items import ModelResponse
from agents.testing import assistant_message
from agents.usage import Usage

from open_deep_research.budget import ModelBudgetExceeded, model_bill
from open_deep_research.llm.errors import ModelInvocationError, is_token_limit_exceeded
from open_deep_research.web.run_data import RunJSON, dumps
from open_deep_research.workflow.execution import failure_kind


def test_native_response_round_trip_preserves_replay_items_and_usage_details():
    value = ModelResponse(
        output=[assistant_message("evidence")],
        usage=Usage(
            requests=1,
            input_tokens=7,
            output_tokens=3,
            total_tokens=10,
            input_tokens_details={"cached_tokens": 2, "cache_write_tokens": 1},
            output_tokens_details={"reasoning_tokens": 2},
        ),
        response_id="resp-1",
        raw_usage={"input_tokens": 7},
    )
    restored = RunJSON().deserialize(RunJSON().serialize(value))
    assert restored == value
    assert restored.to_input_items() == value.to_input_items()
    assert restored.usage.input_tokens_details.cache_write_tokens == 1


def test_failed_call_replay_retains_context_fallback_and_safe_usage():
    from open_deep_research.runtime.sdk_runtime import _ReasoningStopped

    value = ModelInvocationError(
        "BadRequestError",
        context_limit=True,
        budget_usage={"model_calls": 1, "input_tokens": 4},
    )
    restored = RunJSON().deserialize(RunJSON().serialize(value))
    assert isinstance(restored, ModelInvocationError)
    assert is_token_limit_exceeded(restored, "openai:fixture")
    assert restored.budget_usage == value.budget_usage
    assert failure_kind(restored) == "model_error"
    assert isinstance(
        RunJSON().deserialize(
            RunJSON().serialize(ModelBudgetExceeded("private reason"))
        ),
        ModelBudgetExceeded,
    )
    assert isinstance(
        RunJSON().deserialize(RunJSON().serialize(_ReasoningStopped("budget guard"))),
        _ReasoningStopped,
    )


class Bills:
    def __init__(self, usage):
        self.usage = usage

    def execute(self, query, params):
        assert "model_attempts" in str(query) and params == {"r": "run"}
        return self

    def scalars(self):
        return self.usage


def test_known_costs_are_counted_once_with_provider_details():
    actual = {
        "model_calls": 1,
        "input_tokens": 7,
        "output_tokens": 3,
        "total_tokens": 10,
        "input_token_details": {"cache_read": 2},
        "output_token_details": {"reasoning": 1},
    }
    result, pending, complete = model_bill(Bills([dumps(actual)]), "run")
    assert result["model_calls"] == 1 and result["input_tokens"] == 7
    assert result["input_token_details"] == {"cache_read": 2}
    assert result["output_token_details"] == {"reasoning": 1}
    assert pending == 0 and complete


def test_lost_and_partial_provider_bills_remain_counted_and_incomplete():
    result, pending, complete = model_bill(
        Bills(
            [
                dumps({"model_calls": 2, "input_tokens": 7}),
                None,
                dumps({"model_calls": 1, "usage_complete": False}),
            ]
        ),
        "run",
    )
    assert pending == 1 and not complete
    assert result["model_calls"] == 4 and result["input_tokens"] == 7


@pytest.mark.parametrize(
    "steps,expected",
    [
        (
            [
                ("por.stage_started", False),
                ("por.llm", True),
                ("por.llm", False),
                ("por.llm", True),
            ],
            4,
        ),
        ([("por.stage_started", False), ("por.llm", False), ("por.llm", False)], 3),
        (
            [
                ("por.stage_started", False),
                ("por.llm", False),
                ("por.stage_result", True),
            ],
            3,
        ),
        (
            [
                ("por.stage_started", False),
                ("por.llm", False),
                ("por.stage_result", False),
            ],
            4,
        ),
        (
            [
                ("por.stage_started", False),
                ("por.llm", False),
                ("por.stage_result", False),
                ("por.publish_stage", True),
                ("por.stage_failed", False),
            ],
            4,
        ),
        (
            [
                ("por.stage_started", False),
                ("por.llm", True),
                ("por.llm", False),
                ("por.stage_result", False),
                ("por.stage_started", False),
            ],
            6,
        ),
        ([("child-role", False), ("child-review", True)], 2),
    ],
)
def test_retry_preserves_completed_phases_and_prior_calls(steps, expected, monkeypatch):
    from types import SimpleNamespace

    from open_deep_research.workflow import dbos_workflow

    client = SimpleNamespace(
        list_workflow_steps=lambda _id: [
            dict(
                function_id=i + 1,
                function_name=name,
                error=RuntimeError("failed") if failed else None,
            )
            for i, (name, failed) in enumerate(steps)
        ]
    )
    status = SimpleNamespace(
        workflow_id="report",
        name="por.report",
        app_version="current",
        updated_at=2,
        completed_at=3,
    )
    monkeypatch.setattr(dbos_workflow, "native_client", lambda: client)
    assert dbos_workflow.failure_target(status)["start_step"] == expected
