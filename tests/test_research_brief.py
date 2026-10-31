"""New Plan/Brief/report contracts through real structured-output and budget paths."""

import asyncio
from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError
from test_token_accounting import (
    SMALL_USAGE,
    ScriptedChat,
    _chat_result,
    _script,
    _text_step,
    _tool_step,
)

from open_deep_research.budget import budget_capture, estimate_text_tokens
from open_deep_research.workflow import intake, reporting, shared
from open_deep_research.workflow.brief import (
    PlanDecision,
    PlanQuestion,
    brief_metadata,
    validate_answers,
    validate_brief,
)

BRIEF = "# Brand recall research\n\n## Objective\nEstablish reliable recall facts.\n\n## Scope\nBrand, last 30 days, geography unspecified.\n\n## Core questions\nWhat happened and which sources support it?\n\n## Constraints\nDistinguish allegations from facts; cite sources.\n\n## Completion criteria\nAnswer the question or explain missing evidence; match risk and strategy."
CFG = {
    "configurable": {
        "research_model": "openai:gpt-4.1",
        "final_report_model": "openai:gpt-4.1",
        "research_graph_enabled": False,
        "max_structured_output_retries": 1,
        "max_model_calls": 20,
    }
}


def ready(brief=BRIEF):
    return dict(action="ready", questions=[], brief_md=brief)


def ask():
    return dict(
        action="ask_user",
        brief_md="",
        questions=[
            dict(id="brand", text="Which brand?", kind="single", options=["A", "B"])
        ],
    )


@pytest.mark.parametrize("decision", [ready(), ask()])
def test_one_plan_model_request_and_actual_usage(monkeypatch, decision):
    _script(_tool_step("PlanDecision", decision, SMALL_USAGE))
    monkeypatch.setattr(shared, "configurable_model", ScriptedChat())
    with budget_capture() as usage:
        result = asyncio.run(
            intake.plan_research(
                {"messages": [HumanMessage(content="Brand recall")]}, CFG
            )
        )
    assert result["decision"] == decision
    assert (
        usage["model_calls"] == result["patch"]["runtime"]["budget"]["model_calls"] == 1
    )
    assert usage["total_tokens"] == 6


@pytest.mark.parametrize(
    "second,valid",
    [(BRIEF, True), (BRIEF + "长" * 10000, False)],
    ids=["compacted", "still-too-long"],
)
def test_overlimit_compacts_at_most_once_without_truncating(monkeypatch, second, valid):
    long = BRIEF + "长" * 10000
    assert estimate_text_tokens(long) > 2500
    _script(
        _tool_step("PlanDecision", ready(long), SMALL_USAGE),
        _tool_step("PlanDecision", ready(second), SMALL_USAGE),
    )
    monkeypatch.setattr(shared, "configurable_model", ScriptedChat())
    with budget_capture() as usage:
        if valid:
            result = asyncio.run(intake.plan_research({}, CFG))
            assert result["patch"]["workflow"]["brief"] == BRIEF
        else:
            with pytest.raises(ValueError, match="after one compaction"):
                asyncio.run(intake.plan_research({}, CFG))
    assert usage["model_calls"] == 2 and usage["total_tokens"] == 12


@pytest.mark.parametrize(
    "workflow,cfg",
    [
        ({"clarification_complete": True}, CFG),
        ({}, {"configurable": {**CFG["configurable"], "allow_clarification": False}}),
    ],
)
def test_no_further_questions_after_answer_skip_or_disabled(monkeypatch, workflow, cfg):
    _script(_tool_step("PlanDecision", ask(), SMALL_USAGE))
    monkeypatch.setattr(shared, "configurable_model", ScriptedChat())
    with pytest.raises(ValueError, match="disabled or resolved"):
        asyncio.run(intake.plan_research({"workflow": workflow}, cfg))


@pytest.mark.parametrize(
    "decision",
    [
        dict(action="ask_user", questions=[], brief_md=""),
        dict(action="ready", questions=[], brief_md="raw"),
        dict(action="ready", questions=ask()["questions"], brief_md=BRIEF),
    ],
)
def test_invalid_decisions_fail_closed(decision):
    with pytest.raises(ValidationError):
        PlanDecision.model_validate(decision)


def test_versions_bind_exact_content_and_revision():
    assert brief_metadata(BRIEF, 1, "run") != brief_metadata(BRIEF, 2, "run")
    assert (
        brief_metadata(BRIEF, 1, "run")["content_hash"]
        == brief_metadata(BRIEF, 2, "run")["content_hash"]
    )
    assert (
        brief_metadata(BRIEF, 1, "other")["version"]
        != brief_metadata(BRIEF, 1, "run")["version"]
    )
    with pytest.raises(ValueError):
        validate_brief(BRIEF + "长" * 10000)
    assert validate_brief(BRIEF) == BRIEF


def test_choices_and_skip_are_validated():
    questions = ask()["questions"]
    assert validate_answers(questions, {"brand": "A"}, False) == {"brand": "A"}
    assert validate_answers(questions, {}, True) == {}
    for answers, skip in [
        ({"other": "A"}, False),
        ({"brand": "C"}, False),
        ({"brand": "A"}, True),
        ({}, False),
    ]:
        with pytest.raises(ValueError):
            validate_answers(questions, answers, skip)
    with pytest.raises(ValidationError):
        PlanQuestion(id="q", text=" ", kind="text", options=[])


@pytest.mark.parametrize("with_evidence", [True, False])
def test_report_is_one_call_includes_entire_brief_and_role_evidence(
    monkeypatch, with_evidence
):
    from langchain_core.callbacks import BaseCallbackHandler

    prompts = []

    class Capture(BaseCallbackHandler):
        def on_chat_model_start(self, serialized, messages, **kwargs):
            prompts.append(str(messages))

    _script(
        _text_step(
            "# Report\nQuestion answered with source https://example.org and evidence E1",
            SMALL_USAGE,
        )
    )
    monkeypatch.setattr(
        shared, "configurable_model", ScriptedChat(callbacks=[Capture()])
    )
    state = {
        "workflow": {"brief": BRIEF},
        "agents": {
            role: {"report": role + " evidence E1 https://example.org"}
            for role in (
                "public_signal",
                "internal_knowledge",
                "risk_assessment",
                "response_strategy",
            )
        }
        if with_evidence
        else {},
    }
    original = deepcopy(state)
    with budget_capture() as usage:
        result = asyncio.run(reporting.generate_final_report(state, CFG))
    assert usage["model_calls"] == 1 and usage["total_tokens"] == 6
    assert "# Report" in result["report"]["final"] and state == original
    assert "Completion criteria" in prompts[0]
    assert (
        ("risk_assessment evidence" in prompts[0])
        if with_evidence
        else ("No evidence was collected" in prompts[0])
    )


def test_invalid_model_decision_is_a_billed_failure(monkeypatch):
    _script(_tool_step("PlanDecision", ready("not Markdown"), SMALL_USAGE))
    monkeypatch.setattr(shared, "configurable_model", ScriptedChat())
    with budget_capture() as usage:
        with pytest.raises(Exception):
            asyncio.run(intake.plan_research({}, CFG))
    assert usage["model_calls"] == 1 and usage["total_tokens"] == 6


def test_invalid_compaction_cannot_hide_extra_structured_retries(monkeypatch):
    _script(
        _tool_step("PlanDecision", ready(BRIEF + "长" * 10000), SMALL_USAGE),
        _tool_step("PlanDecision", ready("invalid compacted brief"), SMALL_USAGE),
    )
    monkeypatch.setattr(shared, "configurable_model", ScriptedChat())
    cfg = deepcopy(CFG)
    cfg["configurable"]["max_structured_output_retries"] = 3
    with budget_capture() as usage:
        with pytest.raises(Exception):
            asyncio.run(intake.plan_research({}, cfg))
    assert usage["model_calls"] == 2 and usage["total_tokens"] == 12


def test_multiple_choice_answers_remain_bounded_to_declared_options():
    questions = [
        dict(id="sources", text="Sources?", kind="multiple", options=["News", "Social"])
    ]
    assert validate_answers(questions, {"sources": ["News", "Social"]}, False) == {
        "sources": ["News", "Social"]
    }
    for answer in (["News", "News"], ["Unknown"], "News"):
        with pytest.raises(ValueError):
            validate_answers(questions, {"sources": answer}, False)


@pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens"])
def test_report_text_blocks_preserve_text_and_reject_output_limit(
    monkeypatch, stop_reason
):
    _script(
        lambda: _chat_result(
            AIMessage(
                content=[
                    {
                        "type": "text",
                        "text": "# Report\nEvidence E1 supports this finding.",
                    }
                ],
                response_metadata={"stop_reason": stop_reason},
                usage_metadata=SMALL_USAGE,
            )
        )
    )
    monkeypatch.setattr(shared, "configurable_model", ScriptedChat())
    with budget_capture() as usage:
        if stop_reason == "max_tokens":
            with pytest.raises(ValueError, match="empty or incomplete"):
                asyncio.run(
                    reporting.generate_final_report({"workflow": {"brief": BRIEF}}, CFG)
                )
        else:
            result = asyncio.run(
                reporting.generate_final_report({"workflow": {"brief": BRIEF}}, CFG)
            )
            assert result["report"]["final"].startswith(
                "# Report\nEvidence E1 supports this finding."
            )
    assert usage["model_calls"] == 1 and usage["total_tokens"] == 6
