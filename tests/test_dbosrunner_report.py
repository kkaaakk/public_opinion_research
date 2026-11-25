"""Report execution, publication, and native retry contracts."""

import asyncio
from contextlib import nullcontext
from inspect import unwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dbos_support import retry_observed_failure
from sdk_model_support import Reply, install_inference

from open_deep_research import budget
from open_deep_research.llm.generation import ModelOutputError
from open_deep_research.state import ResearchReview, ResearchRoleResult
from open_deep_research.web.observations import stage_errors, stage_results
from open_deep_research.workflow import dbos_workflow as engine
from open_deep_research.workflow import transactions


@pytest.mark.parametrize("finish", ["stop", "length"])
def test_research_workflow_publishes_inline_report(monkeypatch, finish):
    run_id = "run"
    research_id = f"{run_id}:research"
    report_step = "generate_final_report"
    brief = "Approved Brief: assess the recall and recommend next steps"
    journal = {}
    frames, requests, bills = [], [], []
    workflow = SimpleNamespace(
        workflow_id=research_id,
        name="por.research",
        status="PENDING",
        updated_at=1,
        completed_at=None,
    )

    def list_workflows(*, name, status=None, **kwargs):
        return (
            [workflow]
            if workflow.name in name and status in (None, workflow.status)
            else []
        )

    def get_event(workflow_id, key, _timeout):
        return journal.get((workflow_id, key))

    async def set_event(key, value):
        journal[research_id, key] = value

    async def start_role(_function, _run_id, unit_id, role, packet):
        result = ResearchRoleResult(
            role=role,
            round=packet["workflow"]["round"],
            report=f"{role} evidence",
        )
        return SimpleNamespace(
            workflow_id=unit_id,
            get_result=AsyncMock(return_value={"research_result": result.model_dump()}),
        )

    client = SimpleNamespace(list_workflows=list_workflows, get_event=get_event)
    monkeypatch.setattr(
        engine,
        "DBOS",
        SimpleNamespace(
            workflow_id=research_id,
            start_workflow_async=start_role,
            wait_first_async=AsyncMock(side_effect=lambda handles: handles[0]),
            get_event_async=AsyncMock(side_effect=get_event),
            set_event_async=set_event,
        ),
    )
    monkeypatch.setattr(engine, "SetWorkflowID", nullcontext)
    monkeypatch.setattr(engine, "run_generation", lambda _run_id: 0)
    monkeypatch.setattr(
        engine,
        "load_request",
        AsyncMock(
            return_value={"data": {"config": {"configurable": {}}, "initial": {}}}
        ),
    )
    # Review is a bounded model call inside por.research, not its own Workflow.
    monkeypatch.setattr(
        engine.research,
        "research_review",
        AsyncMock(
            return_value={
                "workflow": {
                    "review": ResearchReview(
                        research_complete=True,
                        unresolved_claims=["Unverified date"],
                    )
                },
                "runtime": {"budget": {}},
            }
        ),
    )
    monkeypatch.setattr(
        engine, "domain_engine", lambda: SimpleNamespace(begin=nullcontext)
    )
    # Execute each stage transaction's business logic, not its database plumbing:
    # the checkpoint and commit semantics are covered by the native contracts.
    monkeypatch.setattr(
        transactions,
        "run_transaction",
        lambda _name, call, *args: call(*args),
    )
    monkeypatch.setattr(
        transactions,
        "business_datasource",
        lambda: SimpleNamespace(sql_session=lambda: None),
    )
    monkeypatch.setattr(
        transactions,
        "stage_event",
        lambda _db, scope, kind, **payload: frames.append((scope.name, kind, payload)),
    )
    monkeypatch.setattr(budget, "start_model_attempt", lambda *_args: None)
    monkeypatch.setattr(
        budget, "finish_model_attempt", lambda _run, _id, usage: bills.append(usage)
    )

    def respond(request):
        scope = engine.stage_context.get()
        requests.append(scope.name)
        if scope.name == "risk_assessment_agent":
            return "Risk assessment result"
        if scope.name == "response_strategy_agent":
            assert "Risk assessment result" in request.messages[-1]["content"]
            return "Response strategy result"

        assert scope.name == report_step and not scope.reserve_report
        assert scope.stage_id == f"{research_id}:{report_step}"
        assert stage_results(client, run_id)["current_step"] == report_step
        assert request.tools == [] and request.schema_name is None
        for evidence in (
            brief,
            "public_signal evidence",
            "internal_knowledge evidence",
            "Unverified date",
            "Risk assessment result",
            "Response strategy result",
        ):
            assert evidence in request.prompt
        return Reply("# Recall report", status=finish)

    install_inference(monkeypatch, respond)
    run = unwrap(engine.research_workflow)(
        run_id,
        {"messages": [], "workflow": {"brief": brief}},
    )
    if finish == "length":
        with pytest.raises(ModelOutputError, match="truncated"):
            asyncio.run(run)
        workflow.status = "ERROR"
        [error] = stage_errors(client, run_id)
        assert error["step"] == report_step and error["retry_safe"]
        assert stage_results(client, run_id)["report"] == ""
        assert [kind for step, kind, _ in frames if step == report_step] == [
            "step_started",
            "status",
            "step_failed",
        ]
    else:
        state = asyncio.run(run)
        workflow.status = "SUCCESS"
        assert state["report"] == {"final": "# Recall report"}
        assert state["messages"][-1].content == "# Recall report"
        assert state["messages"][-1].id
        assert state["runtime"]["budget"]["model_calls"] == 3
        view = stage_results(client, run_id)
        assert view["report"] == state["report"]["final"]
        assert view["completed_steps"] == [
            "risk_assessment_agent",
            "response_strategy_agent",
            "research_review",
            report_step,
        ]
        report_frames = [
            (kind, payload) for step, kind, payload in frames if step == report_step
        ]
        assert [kind for kind, _ in report_frames] == [
            "step_started",
            "status",
            "step_completed",
            "report",
        ]
        assert report_frames[-1][1] == {"content": view["report"]}
    assert requests == ["risk_assessment_agent", "response_strategy_agent", report_step]
    assert len(bills) == 3
    assert bills[-1]["model_calls"] == 1 and bills[-1]["total_tokens"] == 10


@pytest.mark.integration
@pytest.mark.parametrize(
    "first_reply", [Reply(""), Reply("Partial synthesis", status="length")]
)
def test_incomplete_synthesis_retries_last_response_without_repeating_roles(
    native_case, monkeypatch, first_reply
):
    case = native_case
    calls = []

    def responses(request):
        if (
            request.schema_name is None
            and "Create the complete enterprise" in request.prompt
        ):
            calls.append(1)
            if len(calls) == 1:
                return first_reply
        return None

    install_inference(monkeypatch, responses)
    run = case.create()
    failed = case.wait(
        run,
        lambda r: r["status"] == "failed"
        and r["events_complete"],
    )
    assert failed["retry_safe"] and len(calls) == 1
    assert failed["current_step"] == "generate_final_report" and not failed["report"]
    retry_observed_failure(case, run, "replace-incomplete-response", failed)
    done = case.wait(
        run,
        lambda r: r["status"] == "completed"
        and r["events_complete"],
    )
    assert len(calls) == 2 and len(case.models) == 2
    assert done["usage"]["model_calls"] == failed["usage"]["model_calls"] + 1
    assert done["completed_steps"].count("generate_final_report") == 1
