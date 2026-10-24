"""Regression tests for section plans and per-section model-call allocation."""

import asyncio

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from open_deep_research.budget import (
    can_spend_model_call,
    merge_budget_usage,
    remaining_model_calls,
)
from open_deep_research.configuration import Configuration
from open_deep_research.state import Section, Sections, report_reducer, runtime_reducer
from open_deep_research.workflow import intake, reporting, shared


@pytest.fixture
def fake_model(monkeypatch):
    model = GenericFakeChatModel(
        messages=iter(AIMessage(content="written section") for _ in range(30))
    )
    monkeypatch.setattr(shared, "configurable_model", model)
    return model


def _sections(research_count=5):
    return [
        Section(name="Introduction", description="Monitoring scope", research=False),
        *[
            Section(
                name=f"Evidence {index}",
                description=f"Distinct research objective {index}",
                research=True,
                agent_role="public_signal",
            )
            for index in range(research_count)
        ],
        Section(name="Conclusion", description="Recommended posture", research=False),
    ]


def _state(sections, used_calls=0):
    return {
        "workflow": {"brief": "Investigate brand risk"},
        "report": {"sections": sections, "completed_sections": []},
        "agents": {"public_signal": {"report": "collected public evidence"}},
        "runtime": {"budget": {"model_calls": used_calls}},
    }


def _config(**overrides):
    return {
        "configurable": {
            "planner_model": "fixture:model",
            "section_writer_model": "fixture:model",
            "final_report_model": "fixture:model",
            "research_graph_enabled": False,
            "rag_memory_write_enabled": False,
            "allow_plan_feedback": False,
            "budget_enabled": True,
            "max_model_calls": 10,
            "reserve_final_report_call": True,
            **overrides,
        }
    }


def _stub_plan(monkeypatch, sections):
    # Keep real chat-model callbacks so the planner's call is counted.
    monkeypatch.setattr(
        intake,
        "structured_output_chain",
        lambda model, schema, **kwargs: model
        | RunnableLambda(lambda response: {"parsed": Sections(sections=sections)}),
    )


@pytest.mark.parametrize("research_count", [3, 5, 7])
@pytest.mark.parametrize(
    "budget_options",
    [
        {"budget_enabled": False, "max_model_calls": 1},
        {"budget_enabled": True, "max_model_calls": None},
        # Exactly one call remains for the planner before the final-report reserve.
        {"budget_enabled": True, "max_model_calls": 4},
    ],
)
def test_planner_preserves_every_section(
    monkeypatch, fake_model, research_count, budget_options
):
    sections = _sections(research_count)
    _stub_plan(monkeypatch, sections)

    command = asyncio.run(
        intake.section_plan_step(_state([], used_calls=2), _config(**budget_options))
    )

    assert command.goto == "research_phase"
    assert command.update["report"]["sections"] == sections
    # Return only this node's delta, not prior usage plus this call.
    assert command.update["runtime"]["budget"]["model_calls"] == 1


def test_plan_feedback_receives_the_complete_plan(monkeypatch, fake_model):
    sections = _sections()
    _stub_plan(monkeypatch, sections)
    command = asyncio.run(
        intake.section_plan_step(_state([]), _config(allow_plan_feedback=True))
    )
    assert command.needs_plan_feedback is True
    assert [s.model_dump() for s in command.update["report"]["sections"]] == [
        s.model_dump() for s in sections
    ]
    assert command.update["report"]["sections"] == sections


def test_planner_still_falls_back_when_no_call_is_available(fake_model):
    command = asyncio.run(intake.section_plan_step(_state([], used_calls=9), _config()))

    assert command.goto == "research_phase"
    assert len(command.update["report"]["sections"]) == 1
    assert command.update["runtime"]["budget"]["model_calls"] == 0


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


@pytest.mark.parametrize(
    "options,used,expected_calls",
    [
        ({}, 7, 2),
        ({"reserve_final_report_call": False}, 7, 3),
        ({}, 9, 0),
        ({}, 12, 0),
        ({"budget_enabled": False, "max_model_calls": 1}, 7, 5),
        ({"max_model_calls": None}, 7, 5),
    ],
)
def test_research_writer_limits_parallel_calls_and_keeps_all_sections(
    fake_model, options, used, expected_calls
):
    sections = _sections()
    state = _state(sections, used_calls=used)

    result = asyncio.run(reporting.section_writer(state, _config(**options)))

    completed = result["report"]["completed_sections"]
    assert result["runtime"]["budget"]["model_calls"] == expected_calls
    assert [s.name for s in completed] == [s.name for s in sections if s.research]
    assert all(s.status == "done" and s.content for s in completed)
    assert all(s.content == "written section" for s in completed[:expected_calls])
    assert all(
        "collected public evidence" in s.content for s in completed[expected_calls:]
    )
    assert all(s.status == "pending" for s in sections if not s.research)


@pytest.mark.parametrize(
    "options,used,expected_calls",
    [
        ({}, 8, 1),
        ({"reserve_final_report_call": False}, 8, 2),
        ({}, 9, 0),
        ({}, 12, 0),
        ({"budget_enabled": False, "max_model_calls": 1}, 8, 2),
        ({"max_model_calls": None}, 8, 2),
    ],
)
def test_final_section_writer_limits_parallel_calls(
    fake_model, options, used, expected_calls
):
    sections = _sections()
    result = asyncio.run(
        reporting.write_final_sections(
            _state(sections, used_calls=used), _config(**options)
        )
    )

    completed = result.get("report", {}).get("completed_sections", [])
    assert result["runtime"]["budget"]["model_calls"] == expected_calls
    assert [s.name for s in completed] == ["Introduction", "Conclusion"][
        :expected_calls
    ]
    assert all(s.status == "done" for s in completed)
    assert all(s.status == "pending" for s in sections if s.research)
    if expected_calls < 2:
        assert result["runtime"]["budget"]["degradation_reasons"]


def test_reporting_uses_post_research_usage_and_preserves_final_call(
    monkeypatch, fake_model
):
    sections = _sections()
    _stub_plan(monkeypatch, sections)
    state = _state([], used_calls=3)
    config = _config(max_model_calls=12)

    async def run_stages():
        command = await intake.section_plan_step(state, config)
        state["report"] = report_reducer(state["report"], command.update["report"])
        state["runtime"] = runtime_reducer(state["runtime"], command.update["runtime"])
        # Research is shared by all sections and consumes budget after planning.
        state["runtime"] = runtime_reducer(
            state["runtime"], {"budget": {"model_calls": 4}}
        )
        assert state["runtime"]["budget"]["model_calls"] == 8

        result = await reporting.section_writer(state, config)
        state["report"] = report_reducer(state["report"], result["report"])
        state["runtime"] = runtime_reducer(state["runtime"], result["runtime"])
        assert result["runtime"]["budget"]["model_calls"] == 3
        assert state["runtime"]["budget"]["model_calls"] == 11
        assert len(state["report"]["completed_sections"]) == 5

        final_sections_result = await reporting.write_final_sections(state, config)
        state["runtime"] = runtime_reducer(
            state["runtime"], final_sections_result["runtime"]
        )
        assert final_sections_result["runtime"]["budget"]["model_calls"] == 0

        final_result = await reporting.compile_final_report(state, config)
        usage = merge_budget_usage(
            state["runtime"]["budget"], final_result["runtime"]["budget"]
        )
        assert final_result["runtime"]["budget"]["model_calls"] == 1
        assert usage["model_calls"] == 12
        assert state["report"]["sections"] == sections
        assert final_result["report"]["final"]

    asyncio.run(run_stages())
