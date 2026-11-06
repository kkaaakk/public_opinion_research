"""Regression contracts for replay-safe errors and physical usage authority."""

import pytest
from agents.items import ModelResponse
from agents.testing import assistant_message
from agents.usage import Usage

from open_deep_research.budget import ModelBudgetExceeded
from open_deep_research.llm.errors import ModelInvocationError, is_token_limit_exceeded
from open_deep_research.web.run_data import RunJSON
from open_deep_research.workflow.business_steps import failure_kind
from open_deep_research.workflow.run_facts import RunFacts


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


def test_replayed_failed_hook_cannot_double_physical_cost_or_token_details(monkeypatch):
    repo = object.__new__(RunFacts)
    actual = {
        "model_calls": 1,
        "input_tokens": 7,
        "output_tokens": 3,
        "total_tokens": 10,
        "input_token_details": {"cache_read": 2},
        "output_token_details": {"reasoning": 1},
    }
    monkeypatch.setattr(repo, "physical_usage", lambda _unit: (actual, True))
    restored = repo.accounted_usage(
        "role",
        {
            "model_calls": 2,
            "input_tokens": 14,
            "input_token_details": {"cache_read": 4},
        },
        {},
    )
    assert restored["model_calls"] == 1 and restored["input_tokens"] == 7
    assert restored["input_token_details"] == {"cache_read": 2}
    assert restored["output_token_details"] == {"reasoning": 1}
    assert repo.failed_usage("role", actual)["model_calls"] == 0


def test_unreceipted_prior_business_cost_is_retained_without_recounting(monkeypatch):
    repo = object.__new__(RunFacts)
    monkeypatch.setattr(
        repo,
        "physical_usage",
        lambda _unit: ({"model_calls": 2, "input_tokens": 7}, True),
    )
    carry = repo.failed_usage("review", {"model_calls": 3, "input_tokens": 9})
    assert carry["model_calls"] == 1 and carry["input_tokens"] == 2
    restored = repo.accounted_usage(
        "review", {"model_calls": 4, "input_tokens": 16}, carry
    )
    assert restored["model_calls"] == 3 and restored["input_tokens"] == 9


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
                ("por.stage_result", False),
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
def test_retry_preserves_completed_phases_and_prior_calls(steps, expected):
    from types import SimpleNamespace

    from open_deep_research.web.durable import RunClient

    client = object.__new__(RunClient)
    client.client = SimpleNamespace(
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
        workflow_id="report", app_version="current", updated_at=2, completed_at=3
    )
    assert client._failure_target(status, "business")["start_step"] == expected
