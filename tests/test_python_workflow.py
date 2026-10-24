"""Verify deterministic scheduling, State ownership and failure accounting offline."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from reference_workflow import (
    PythonWorkflow,
    WorkflowExecutionError,
    failure_kind,
)

from open_deep_research.budget import (
    ModelBudgetExceeded,
    budget_capture,
    capture_budget_usage,
)
from open_deep_research.configuration import Configuration
from open_deep_research.state import ResearchReview, ResearchTask, Section
from open_deep_research.workflow import get_workflow, intake, reporting, research
from open_deep_research.workflow.python_state import (
    apply_patch,
    copy_state,
    merge_research_patches,
    role_snapshot,
)

ROLES = ("public_signal", "internal_knowledge", "risk_assessment", "response_strategy")


def config(**updates):
    return {
        "configurable": {
            "business_agent_runtime": "openai_agents",
            "workflow_backend": "python",
            "research_graph_enabled": False,
            "rag_query_image_enabled": False,
            "allow_clarification": False,
            "enabled_business_agents": list(ROLES),
            "max_research_rounds": 4,
            **updates,
        }
    }


def initial():
    return {
        "messages": [HumanMessage(content="brand recall", id="input")],
        "workflow": {"brief": "brand recall"},
        "agents": {
            role: {"memory": [{"content": f"private-{role}"}]} for role in ROLES
        },
        "research": {"run_id": "python-run"},
        "report": {
            "sections": [Section(name="Evidence", description="brand evidence")]
        },
        "runtime": {
            "budget": {
                "model_calls": 7,
                "input_tokens": 70,
                "output_tokens": 7,
                "total_tokens": 77,
            }
        },
    }


def gap(name, role="public_signal"):
    return ResearchTask(
        task_id=name,
        target_role=role,
        objective=f"resolve {name}",
        evidence_needed="dated evidence",
        reason="changes risk",
    )


def install_steps(monkeypatch, reviews=(), *, slower="public_signal"):
    events = []
    arrivals = set()
    barrier = asyncio.Event()
    script = iter(reviews)

    async def agent(*, role, state, config):
        round = state["workflow"]["round"]
        tasks = state["workflow"].get("pending_tasks", [])
        task_ids = [
            task.task_id if isinstance(task, ResearchTask) else task["task_id"]
            for task in tasks
        ]
        assert all(
            "memory" not in state["agents"][other] for other in ROLES if other != role
        )
        if role in ROLES[:2] and round == 1:
            arrivals.add(role)
            if len(arrivals) == 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), 1)
            if role == slower:
                await asyncio.sleep(0)
        if role == "risk_assessment":
            assert events[-1][0] == "review"
        if role == "response_strategy":
            assert "risk_assessment" in state["agents"]["risk_assessment"]["report"]
            assert events[-1][0] == "risk_assessment"
        events.append((role, round, task_ids))
        return {
            "agents": {
                role: {
                    "report": f"{role}:{round}",
                    "memory": [{"content": f"{role}:{round}"}],
                    "rolling_summary": f"summary-{role}:{round}",
                }
            },
            "workflow": {"round": round, "completed_tasks": tasks},
            "runtime": {
                "budget": {"model_calls": 2, "tool_calls": 1, "input_tokens": 10}
            },
        }

    async def review(state, config):
        assert all(state["agents"][role].get("report") for role in ROLES[:2])
        events.append(("review", state["workflow"]["round"], []))
        return {
            "workflow": {
                "review": next(script, ResearchReview(research_complete=True)),
                "pending_tasks": {"type": "override", "value": []},
            },
            "runtime": {"budget": {"model_calls": 1}},
        }

    async def sections(state, config):
        return {
            "report": {
                "completed_sections": [
                    Section(
                        name="Evidence",
                        description="brand evidence",
                        content="complete evidence",
                        status="done",
                    )
                ]
            }
        }

    async def final_sections(state, config):
        return {}

    async def final(state, config, **kwargs):
        assert all(state["agents"][role].get("report") for role in ROLES)
        report = "\n".join(state["agents"][role]["report"] for role in ROLES)
        return {"report": {"final": report}, "messages": [AIMessage(content=report)]}

    monkeypatch.setattr(research, "run_business_agent", agent)
    monkeypatch.setattr(research, "research_review", review)
    monkeypatch.setattr(reporting, "section_writer", sections)
    monkeypatch.setattr(reporting, "write_final_sections", final_sections)
    monkeypatch.setattr(reporting, "compile_final_report", final)
    return events


@pytest.mark.parametrize("slower", ROLES[:2])
def test_true_parallel_join_order_and_private_state(monkeypatch, slower):
    async def run():
        events = install_steps(monkeypatch, slower=slower)
        state = initial()
        saved = deepcopy(state)
        with budget_capture() as usage:
            result = await PythonWorkflow().ainvoke(
                state, config(), start_at="research"
            )
        assert state == saved
        assert [e[0] for e in events[2:]] == [
            "review",
            "risk_assessment",
            "response_strategy",
        ]
        assert list(result["agents"]) == list(ROLES)
        assert result["runtime"]["budget"]["model_calls"] == 16
        assert usage["model_calls"] == 9
        assert usage["tool_calls"] == 4
        assert result["runtime"]["budget"]["input_tokens"] == 110
        assert all(len(result["agents"][r]["memory"]) == 2 for r in ROLES)
        assert result["report"]["completed_sections"][0].content == "complete evidence"
        assert all(role in result["report"]["final"] for role in ROLES)

    asyncio.run(run())


@pytest.mark.parametrize("limit,expected", [(1, 1), (2, 2), (3, 3), (4, 3)])
def test_multiple_gap_rounds_limit_and_early_stop(monkeypatch, limit, expected):
    async def run():
        reviews = [
            ResearchReview(
                research_complete=False,
                next_tasks=[gap("date"), gap("approval", "internal_knowledge")],
            ),
            ResearchReview(
                research_complete=False, next_tasks=[gap("date"), gap("more")]
            ),
            ResearchReview(research_complete=True, next_tasks=[gap("unused")]),
        ]
        events = install_steps(monkeypatch, reviews)
        result = await PythonWorkflow().ainvoke(
            initial(), config(max_research_rounds=limit), start_at="research"
        )
        assert result["workflow"]["round"] == expected
        assert sum(e[0] == "review" for e in events) == expected
        tasks = [t.task_id for t in result["workflow"]["completed_tasks"]]
        assert tasks == (
            []
            if expected == 1
            else ["date", "approval"]
            if expected == 2
            else ["date", "approval", "more"]
        )
        if expected > 1:
            assert ("public_signal", 2, ["date"]) in events
            assert ("internal_knowledge", 2, ["approval"]) in events
        if expected == 3:
            assert ("public_signal", 3, ["more"]) in events
            assert not any(e[0] == "internal_knowledge" and e[1] == 3 for e in events)
        assert result["workflow"]["pending_tasks"] == []

    asyncio.run(run())


def test_duplicate_or_invalid_gaps_terminate_without_research_replay(monkeypatch):
    async def run():
        task = gap("date")
        events = install_steps(
            monkeypatch,
            [
                ResearchReview(research_complete=False, next_tasks=[task, task]),
                ResearchReview(research_complete=False, next_tasks=[task]),
            ],
        )
        result = await PythonWorkflow().ainvoke(
            initial(), config(), start_at="research"
        )
        assert [e[0] for e in events].count("public_signal") == 2
        assert result["workflow"]["completed_tasks"] == [task]
        assert result["workflow"]["round"] == 2

    asyncio.run(run())


@pytest.mark.parametrize("failure_count", [1, 2])
def test_single_and_simultaneous_agent_failures_cancel_and_account(
    monkeypatch, failure_count
):
    async def run():
        install_steps(monkeypatch)
        entered = set()
        barrier = asyncio.Event()
        cleaned = []

        async def fail(*, role, state, config):
            entered.add(role)
            if len(entered) == 2:
                barrier.set()
            await barrier.wait()
            capture_budget_usage({"model_calls": 1, "input_tokens": 4})
            try:
                if role == "public_signal" or failure_count == 2:
                    raise ValueError(f"failed-{role}")
                await asyncio.Event().wait()
            finally:
                cleaned.append(role)

        monkeypatch.setattr(research, "run_business_agent", fail)
        with budget_capture() as usage, pytest.raises(WorkflowExecutionError) as caught:
            await PythonWorkflow().ainvoke(initial(), config(), start_at="research")
        error = caught.value
        assert set(cleaned) == set(ROLES[:2])
        assert list(error.failures) == list(ROLES[:2])
        assert (
            sum(isinstance(e, ValueError) for e in error.failures.values())
            == failure_count
        )
        assert error.state["runtime"]["budget"]["model_calls"] == 9
        assert error.budget_usage["model_calls"] == usage["model_calls"] == 2
        assert "final" not in error.state["report"]
        assert not any(
            t.get_name() in ROLES
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task()
        )

    asyncio.run(run())


@pytest.mark.parametrize("repeats", [1, 3])
def test_external_and_repeated_cancel_wait_for_child_settlement(monkeypatch, repeats):
    async def run():
        install_steps(monkeypatch)
        started = set()
        entered = asyncio.Event()
        cleanup_started = asyncio.Event()
        cleanup_done = asyncio.Event()
        finished = []

        async def role_call(*, role, state, config):
            capture_budget_usage({"model_calls": 1})
            started.add(role)
            if len(started) == 2:
                entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await cleanup_done.wait()
                finished.append(role)

        monkeypatch.setattr(research, "run_business_agent", role_call)
        task = asyncio.create_task(
            PythonWorkflow().ainvoke(initial(), config(), start_at="research")
        )
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.wait_for(cleanup_started.wait(), 1)
        for _ in range(repeats - 1):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        cleanup_done.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert set(finished) == set(ROLES[:2])
        assert caught.value.workflow_state["runtime"]["budget"]["model_calls"] == 9
        assert caught.value.budget_usage["model_calls"] == 2

    asyncio.run(run())


def test_timeout_propagates_and_keeps_completed_sibling_delta(monkeypatch):
    async def run():
        install_steps(monkeypatch)
        closed = []

        async def role_call(*, role, state, config):
            if role == "public_signal":
                return {
                    "agents": {role: {"report": "successful partial"}},
                    "runtime": {"budget": {"model_calls": 2}},
                }
            capture_budget_usage({"model_calls": 1})
            try:
                await asyncio.Event().wait()
            finally:
                closed.append(role)

        monkeypatch.setattr(research, "run_business_agent", role_call)
        with pytest.raises(WorkflowExecutionError) as caught:
            await PythonWorkflow().ainvoke(
                initial(),
                config(workflow_step_timeout_seconds=0.05),
                start_at="research",
            )
        assert caught.value.kinds == {"internal_knowledge": "timeout"}
        assert caught.value.budget_usage["model_calls"] == 3
        assert (
            caught.value.state["agents"]["public_signal"]["report"]
            == "successful partial"
        )
        assert closed == ["internal_knowledge"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "stage",
    [
        "research_review",
        "risk_assessment",
        "response_strategy",
        "section_writer",
        "compile_final_report",
    ],
)
def test_later_failure_does_not_replay_successful_tools(monkeypatch, stage):
    async def run():
        events = install_steps(monkeypatch)

        async def fail(state, config, **kwargs):
            capture_budget_usage({"model_calls": 1})
            raise RuntimeError("later failure")

        if stage in ROLES:
            original = research.run_business_agent

            async def role_call(*, role, state, config):
                return (
                    await fail(state, config)
                    if role == stage
                    else await original(role=role, state=state, config=config)
                )

            monkeypatch.setattr(research, "run_business_agent", role_call)
        else:
            monkeypatch.setattr(
                research if stage == "research_review" else reporting, stage, fail
            )
        with pytest.raises(WorkflowExecutionError) as caught:
            await PythonWorkflow().ainvoke(initial(), config(), start_at="research")
        assert sum(e[0] == "public_signal" for e in events) == 1
        assert sum(e[0] == "internal_knowledge" for e in events) == 1
        assert caught.value.state["runtime"]["budget"]["tool_calls"] >= 2
        assert "final" not in caught.value.state["report"]

    asyncio.run(run())


def test_parallel_merges_are_completion_order_independent_and_preserve_contracts():
    patches = {
        role: {
            "agents": {
                role: {
                    "report": {"type": "override", "value": f"new-{role}"},
                    "memory": [{"content": role}],
                    "rolling_summary": role,
                }
            },
            "workflow": {
                "round": 2,
                "completed_tasks": [gap(role, role), gap(role, role)],
            },
            "research": {
                "run_id": "python-run",
                "working_contexts": {role: {"goal": role}},
                "working_context_scopes": {role: {"round": 2}},
            },
            "report": {
                "completed_sections": [
                    Section(name=role, description=role, content=role)
                ]
            },
            "runtime": {"budget": {"model_calls": 2, "input_tokens": 10}},
        }
        for role in ROLES[:2]
    }
    left, right = copy_state(initial()), copy_state(initial())
    merge_research_patches(left, patches)
    merge_research_patches(right, dict(reversed(list(patches.items()))))
    assert left == right
    assert len(left["workflow"]["completed_tasks"]) == 2
    assert left["runtime"]["budget"]["model_calls"] == 11
    assert list(left["research"]["working_contexts"]) == list(ROLES[:2])
    apply_patch(left, {"agents": {"public_signal": {"report": "additional"}}})
    assert (
        "Additional role research report" in left["agents"]["public_signal"]["report"]
    )
    assert left["agents"]["internal_knowledge"]["report"] == "new-internal_knowledge"


@pytest.mark.parametrize(
    "bad_patch",
    [
        {"agents": {"internal_knowledge": {"memory": []}}},
        {"agents": {"type": "override", "value": {}}},
        {"workflow": {"brief": "overwrite"}},
        {"workflow": {"completed_tasks": [gap("other", "internal_knowledge")]}},
        {"research": {"run_id": "another-run"}},
        {"research": {"working_contexts": {"internal_knowledge": {}}}},
    ],
)
def test_invalid_parallel_writes_are_rejected_atomically(bad_patch):
    state = copy_state(initial())
    saved = deepcopy(state)
    with pytest.raises(ValueError):
        merge_research_patches(state, {"public_signal": bad_patch})
    assert state == saved


def test_conflicting_shared_report_replacements_fail_atomically():
    state = copy_state(initial())
    with pytest.raises(ValueError, match="Conflicting"):
        merge_research_patches(
            state, {role: {"report": {"final": role}} for role in ROLES[:2]}
        )
    assert "final" not in state["report"]


def test_message_identity_and_snapshot_isolation():
    state = copy_state(initial())
    apply_patch(state, {"messages": [HumanMessage(content="edited", id="input")]})
    assert len(state["messages"]) == 1
    assert state["messages"][0].content == "edited"
    snapshot = role_snapshot(state, "public_signal")
    snapshot["agents"]["public_signal"]["memory"][0]["content"] = "changed"
    assert (
        state["agents"]["public_signal"]["memory"][0]["content"]
        == "private-public_signal"
    )
    apply_patch(state, {"messages": [RemoveMessage(id="input")]})
    assert state["messages"] == []


@pytest.mark.parametrize(
    "updates", [{"business_agent_runtime": "legacy"}, {"allow_plan_feedback": True}]
)
def test_python_incompatible_paths_rejected_before_io(updates):
    with pytest.raises(ValueError):
        asyncio.run(PythonWorkflow().ainvoke(initial(), config(**updates)))


def test_backend_selection_rejects_retired_in_process_factory():
    for cfg in ({}, config()):
        with pytest.raises(ValueError, match="durable Run API"):
            get_workflow(cfg)
    assert "business_agent_runtime" not in Configuration.model_fields
    assert "workflow_backend" not in Configuration.model_fields
    with pytest.raises(ValueError):
        Configuration(workflow_backend="unknown")
    with pytest.raises(ValueError):
        Configuration(workflow_step_timeout_seconds=0)


@pytest.mark.parametrize(
    "error,kind",
    [
        (ModelBudgetExceeded(), "budget_exhausted"),
        (TimeoutError(), "timeout"),
        (asyncio.CancelledError(), "cancelled"),
    ],
)
def test_failure_kinds(error, kind):
    assert failure_kind(error) == kind


def test_python_workflow_does_not_compile_or_invoke_legacy_graph(monkeypatch):
    import builtins

    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith("langgraph"):
            raise AssertionError("retired engine import")
        return original(name, *args, **kwargs)

    async def run():
        install_steps(monkeypatch)
        monkeypatch.setattr(builtins, "__import__", guarded)
        assert (
            await PythonWorkflow().ainvoke(initial(), config(), start_at="research")
        )["report"]["final"]

    asyncio.run(run())


def test_parallel_section_failure_drains_sibling_and_keeps_all_attempts(monkeypatch):
    async def run():
        entered = asyncio.Event()
        cleaned = []

        class Writer:
            def with_config(self, config):
                return self

        async def invoke(model, messages, **kwargs):
            capture_budget_usage({"model_calls": 1, "input_tokens": 3})
            if messages[0].content == "slow":
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.append("slow")
            await entered.wait()
            raise RuntimeError("section failed after request")

        monkeypatch.setattr(reporting.shared, "configurable_model", Writer())
        monkeypatch.setattr(reporting, "ainvoke_model_with_budget", invoke)
        with pytest.raises(RuntimeError) as caught:
            await reporting._write_sections_parallel(
                [
                    Section(name="slow", description=""),
                    Section(name="fail", description=""),
                ],
                model_name="fixture:writer",
                model_config={},
                prompt_for=lambda s: s.name,
                log_label="fixture",
                failure_reason_for=lambda s: s.name,
                completion_reason_for=str,
            )
        assert cleaned == ["slow"]
        assert caught.value.budget_usage["model_calls"] == 2
        assert caught.value.budget_usage["input_tokens"] == 6

    asyncio.run(run())


def test_failed_planner_preserves_its_nested_capture(monkeypatch):
    async def run():
        calls = []

        async def enrich(state, config):
            return {}

        async def decision(state, config):
            return intake.IntakeDecision(goto="plan_report_sections")

        async def invoke(model, messages, **kwargs):
            calls.append("planner")
            capture_budget_usage({"model_calls": 1, "input_tokens": 9})
            raise RuntimeError("planner failed")

        monkeypatch.setattr(
            intake.shared,
            "configurable_model",
            SimpleNamespace(with_config=lambda _: object()),
        )
        monkeypatch.setattr(intake, "enrich_query_images", enrich)
        monkeypatch.setattr(intake, "clarification_step", decision)
        monkeypatch.setattr(intake, "research_brief_step", decision)
        monkeypatch.setattr(intake, "structured_output_chain", lambda *a, **k: object())
        monkeypatch.setattr(intake, "ainvoke_model_with_budget", invoke)
        with pytest.raises(WorkflowExecutionError) as caught:
            await PythonWorkflow().ainvoke(initial(), config())
        assert calls == ["planner"]
        assert caught.value.budget_usage["model_calls"] == 1
        assert caught.value.budget_usage["input_tokens"] == 9

    asyncio.run(run())


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("known_capacity", [False, True])
def test_final_report_error_keeps_legacy_contract_and_fails_python(
    monkeypatch, strict, known_capacity
):
    async def run():
        async def invoke(*args, **kwargs):
            capture_budget_usage({"model_calls": 1, "input_tokens": 2})
            raise RuntimeError("context length exceeded")

        monkeypatch.setattr(
            reporting.shared,
            "configurable_model",
            SimpleNamespace(with_config=lambda _: object()),
        )
        monkeypatch.setattr(reporting, "ainvoke_model_with_budget", invoke)
        monkeypatch.setattr(reporting, "is_token_limit_exceeded", lambda *a: True)
        monkeypatch.setattr(
            reporting,
            "get_model_token_limit",
            lambda *a: 32_768 if known_capacity else None,
        )
        state = copy_state(initial())
        state["report"]["sections"] = []
        if strict:
            with pytest.raises(
                RuntimeError, match="Final report generation failed"
            ) as caught:
                await reporting.compile_final_report(
                    state, config(), fail_on_error=True
                )
            assert caught.value.budget_usage["model_calls"] == (
                4 if known_capacity else 1
            )
        else:
            patch = await reporting.compile_final_report(state, config())
            assert patch["report"]["final"].startswith("Error generating final report")
            assert patch["runtime"]["budget"]["model_calls"] == (
                4 if known_capacity else 1
            )

    asyncio.run(run())


def test_memory_write_failure_preserves_successful_report_cost(monkeypatch):
    async def run():
        async def fail(*args):
            raise RuntimeError("memory I/O failed")

        monkeypatch.setattr(reporting, "maybe_persist_chat_memory", fail)
        with pytest.raises(RuntimeError) as caught:
            await reporting._persist_report(
                copy_state(initial()),
                config(),
                "report",
                {"model_calls": 3, "input_tokens": 20},
            )
        assert caught.value.budget_usage["model_calls"] == 3
        assert caught.value.budget_usage["input_tokens"] == 20

    asyncio.run(run())
