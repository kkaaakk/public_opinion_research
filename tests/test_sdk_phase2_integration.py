"""Exercise Phase 2 with the real SDK Runner and compiled LangGraph workflow."""

import asyncio
import json
import re
from collections import Counter
from contextlib import asynccontextmanager

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, StateGraph
from openai.types.responses import ResponseReasoningItem
from test_public_opinion_research_loop import _initial_state
from test_sdk_context import _payload, _Store
from test_sdk_runtime import ROLES, TOKENS, _runtime, _tool

from open_deep_research.budget import (
    ainvoke_model_with_budget,
    budget_capture,
    model_budget_admission,
)
from open_deep_research.configuration import Configuration
from open_deep_research.public_opinion_agents import get_public_opinion_agent_spec
from open_deep_research.research_graph import (
    GraphExtractionOutput,
    ResearchWorkspace,
    WorkingContextDelta,
    create_context_strategy,
)
from open_deep_research.research_graph.models import RollingCompactOutput
from open_deep_research.research_graph.store import (
    clear_research_graph_store_cache,
    create_research_graph_store,
)
from open_deep_research.research_graph.workspace import TaskDescriptor
from open_deep_research.runtime import business_agent
from open_deep_research.runtime.sdk_context import ResearchGraphSettlementError
from open_deep_research.runtime.sdk_runtime import SDKContextLimitExceeded
from open_deep_research.state import (
    DeepResearchState,
    ResearchReview,
    ResearchTask,
    Section,
    research_reducer,
)
from open_deep_research.workflow import reporting, research, shared


class Services:
    """Replace inference only, emitting real LangChain callbacks and usage."""

    def __init__(
        self,
        *,
        summary_error=False,
        context_failures=0,
        summary="Decisions retained; continue the assigned research.",
    ):
        self.calls = Counter()
        self.summary = summary
        self.summary_error = summary_error
        self.context_failures = context_failures
        self.prompts = []
        self.raw = FakeMessagesListChatModel(
            responses=[
                AIMessage(
                    content="service output",
                    response_metadata={"model_name": "fixture-service"},
                    usage_metadata={
                        "input_tokens": 7,
                        "output_tokens": 3,
                        "total_tokens": 10,
                    },
                )
            ]
        )

    def with_config(self, config):
        return self

    async def ainvoke(self, messages, config=None):
        text = str(messages[0].content)
        self.prompts.append(text)
        self.calls["report"] += 1
        raw = await self.raw.ainvoke(messages, config)
        if "Role:" in text:
            role = text.split("Role: ", 1)[1].splitlines()[0]
            return raw.model_copy(
                update={
                    "content": f"{role} GRAPH REPORT; risk decision: escalate brand recall"
                }
            )
        return raw.model_copy(
            update={"content": "FINAL brand recall report, sourced and reviewed"}
        )

    def with_structured_output(self, schema, include_raw=False):
        async def respond(messages, config=None):
            self.calls[schema.__name__] += 1
            text = str(messages[0].content)
            self.prompts.append(text)
            raw = await self.raw.ainvoke(messages, config)
            if schema is GraphExtractionOutput:
                ids = list(
                    dict.fromkeys(re.findall(r'"source_id":\s*"(SRC_[^"]+)"', text))
                )
                parsed = GraphExtractionOutput(
                    evidences=[
                        {
                            "source_id": sid,
                            "statement": f"Verified brand recall evidence {sid}",
                        }
                        for sid in ids
                    ]
                )
            elif schema is WorkingContextDelta:
                if self.context_failures:
                    self.context_failures -= 1
                    raise ValueError("context fixture failed after usage")
                ids = sorted(set(re.findall(r"EVD_[a-f0-9]{20}", text)))
                parsed = WorkingContextDelta(
                    active_evidence_ids=ids,
                    confirmed_findings_add=[
                        {
                            "summary": "brand recall evidence committed",
                            "evidence_ids": ids,
                        }
                    ]
                    if ids
                    else [],
                    conflicts_add=[
                        {
                            "summary": "brand recall timing remains disputed",
                            "evidence_ids": ids,
                        }
                    ]
                    if ids
                    else [],
                    open_gaps_add=[
                        {"summary": "confirm brand recall date", "evidence_ids": ids}
                    ]
                    if ids
                    else [],
                    recent_progress="Working Context refreshed from committed evidence",
                )
            elif schema is RollingCompactOutput:
                if self.summary_error:
                    raise ValueError("summary fixture failed after usage")
                parsed = RollingCompactOutput(rolling_summary=self.summary)
            else:
                raise AssertionError(schema)
            return {"raw": raw, "parsed": parsed, "parsing_error": None}

        return RunnableLambda(respond)


def workspace(
    tmp_path,
    role="public_signal",
    *,
    services=None,
    store=None,
    config=None,
    run_id="phase2-run",
    research_state=None,
    round=1,
    task_id=None,
):
    config = config or Configuration(
        research_graph_enabled=True,
        research_graph_backend="memory",
        research_model="openai:fixture",
        research_graph_transcript_dir=str(tmp_path),
        recent_raw_steps=0,
        max_structured_output_retries=1,
    )
    services = services or Services()
    return ResearchWorkspace(
        strategy=create_context_strategy(
            get_public_opinion_agent_spec(role).context_strategy,
            graph_enabled=config.research_graph_enabled,
        ),
        research_state=research_state or {},
        role=role,
        configurable=config,
        model_factory=lambda *_: services,
        store=store if store is not None else _Store(),
        run_id=run_id,
        research_round=round,
        task=TaskDescriptor(
            task_id or f"task-{role}-{round}",
            "Investigate brand recall evidence",
            "brand recall sources",
        ),
    )


def scripted_tool_runtime(
    ws, *, role="public_signal", steps=None, tool=None, agent_state=None
):
    async def search(query, limit=3):
        return _payload(query)

    tool = tool or _tool(role, coroutine=search)
    return _runtime(
        role,
        config=ws.configurable,
        workspace=ws,
        tools=[tool],
        agent_state=agent_state,
        model=ScriptedModel(
            steps
            or [
                [function_call(tool.name, {"query": "brand"}, call_id="lookup")],
                [assistant_message("SDK analysis")],
            ],
            default_usage=TOKENS,
        ),
    )


def run_workflow_fixture(monkeypatch, tmp_path, backend="langgraph"):
    """Run identical inference/data through either real workflow implementation."""

    async def scenario():
        clear_research_graph_store_cache()
        services = Services()
        producer_barrier = asyncio.Event()
        tool_barrier = asyncio.Event()
        entered = set()
        tool_entered = set()
        events = []
        role_calls = Counter()
        tool_calls = []
        models = []
        run_id = "phase2-full-business-run"
        gap = ResearchTask(
            task_id="recall-date-gap",
            target_role="public_signal",
            objective="Find missing brand recall date",
            evidence_needed="brand dated recall notice",
            reason="affects escalation",
            priority="high",
        )

        class Reviewer(Services):
            def with_structured_output(self, schema, include_raw=False):
                async def review(messages, config=None):
                    text = str(messages[0].content)
                    # This is the real Review node reading the committed Store.
                    assert "[Evidence]" in text
                    assert "public_signal" in text and "internal_knowledge" in text
                    assert "private-" not in text
                    events.append("review")
                    n = events.count("review")
                    if n == 1:
                        assert "https://example.test/public_signal-1" in text
                        assert "https://example.test/internal_knowledge-1" in text
                    else:
                        assert "https://example.test/public_signal-2" in text
                    raw = await services.raw.ainvoke(messages, config)
                    return {
                        "raw": raw,
                        "parsed": ResearchReview(
                            research_complete=n > 1, next_tasks=[gap] if n == 1 else []
                        ),
                        "parsing_error": None,
                    }

                return RunnableLambda(review)

        @asynccontextmanager
        async def sdk_context(*args, **kwargs):
            current = {}

            async def begin(call):
                role = call.system_instructions.split("<Role>\n", 1)[1].split(
                    "\n</Role>", 1
                )[0]
                current["role"] = role
                role_calls[role] += 1
                n = role_calls[role]
                events.append(f"start-{role}-{n}")
                text = json.dumps(call.input)
                assert f"private-{role}" in text
                assert all(
                    f"private-{other}" not in text for other in ROLES if other != role
                )
                if role in ROLES[:2] and n == 1:
                    entered.add(role)
                    if len(entered) == 2:
                        producer_barrier.set()
                    await asyncio.wait_for(producer_barrier.wait(), 3)
                if role == "public_signal" and n == 2:
                    assert gap.task_id in text and "round 2" in text
                    assert "internal_knowledge-1" in text
                if role == "risk_assessment":
                    assert events.count("review") == 2
                    assert "public_signal-2" in text and "internal_knowledge-1" in text
                    assert (
                        "timing remains disputed" in text
                        and "confirm brand recall date" in text
                    )
                if role == "response_strategy":
                    assert "end-risk_assessment-1" in events
                    assert "risk_assessment GRAPH REPORT" in text
                    assert "analysis, not original Graph evidence" in text
                    assert "public_signal-2" in text and "[Evidence]" in text
                    assert all(tool.name != "social_lookup" for tool in call.tools)
                if role in ROLES[:2]:
                    names = (
                        ["web_search", "social_lookup"]
                        if role == "public_signal"
                        else ["rag_search", "knowledge__lookup"]
                    )
                    return [
                        function_call(
                            name, {"query": f"{role}-{n}"}, call_id=f"{name}-{n}"
                        )
                        for name in names
                    ]
                return [assistant_message(f"{role} SDK analysis")]

            def end(call):
                role = current["role"]
                assert "Persisted to Research Graph" in json.dumps(call.input)
                events.append(f"end-{role}-{role_calls[role]}")
                return [assistant_message("producer SDK done")]

            async def first(call):
                result = await begin(call)
                if current["role"] not in ROLES[:2]:
                    events.append(f"end-{current['role']}-1")
                return result

            model = ScriptedModel(
                [ModelStep.respond(first), ModelStep.respond(end)], default_usage=TOKENS
            )
            models.append(model)
            yield model

        async def tools(config, role, spec):
            async def search(query, limit=3):
                tool_calls.append((role, query))
                if query.endswith("-1"):
                    tool_entered.add(
                        (role, len([value for value in tool_calls if value[0] == role]))
                    )
                    if len(tool_entered) == 4:
                        tool_barrier.set()
                    await asyncio.wait_for(tool_barrier.wait(), 3)
                return _payload(query)

            if role == "public_signal":
                return [
                    _tool(role, name="web_search", coroutine=search),
                    _tool(
                        role,
                        name="social_lookup",
                        domain="social_media",
                        coroutine=search,
                    ),
                ]
            if role == "internal_knowledge":
                return [
                    _tool(role, name="rag_search", coroutine=search),
                    _tool(
                        role, name="knowledge__lookup", domain="rag", coroutine=search
                    ),
                ]
            return []

        monkeypatch.setattr(business_agent, "sdk_model", sdk_context)
        monkeypatch.setattr(business_agent, "_business_agent_tools", tools)
        monkeypatch.setattr(business_agent, "_CONFIGURABLE_MODEL", services)
        # Keep the actual compiled research subgraph and all three report nodes.
        reviewer = Reviewer()

        class SharedModel:
            def with_config(self, config):
                return reviewer if config["model"] == "fixture:review" else services

        monkeypatch.setattr(shared, "configurable_model", SharedModel())
        config = {
            "configurable": {
                "business_agent_runtime": "openai_agents",
                "research_graph_enabled": True,
                "research_graph_backend": "memory",
                "research_graph_transcript_dir": str(tmp_path),
                "research_model": "fixture:review",
                "enabled_business_agents": list(ROLES),
                "recent_raw_steps": 0,
                "max_research_rounds": 2,
                "max_structured_output_retries": 1,
                "max_model_calls": 100,
                "budget_enabled": True,
                "reserve_final_report_call": True,
            }
        }
        builder = StateGraph(DeepResearchState)
        builder.add_node("research", research.research_phase)
        builder.add_node("sections", reporting.section_writer)
        builder.add_node("final_sections", reporting.write_final_sections)
        builder.add_node("final", reporting.compile_final_report)
        builder.add_edge(START, "research")
        builder.add_edge("research", "sections")
        builder.add_edge("sections", "final_sections")
        builder.add_edge("final_sections", "final")
        builder.add_edge("final", END)
        state = _initial_state()
        state["research"]["run_id"] = run_id
        state["workflow"]["brief"] = "Investigate brand recall evidence"
        state["agents"] = {
            role: {"memory": [{"content": f"private-{role}"}]} for role in ROLES
        }
        state["runtime"]["budget"] = {
            "model_calls": 7,
            "input_tokens": 70,
            "output_tokens": 7,
            "total_tokens": 77,
        }
        state["report"]["sections"] = [
            Section(
                name="Evidence and response",
                description="brand recall evidence and risk",
                agent_role=",".join(ROLES),
            )
        ]
        if backend == "python":
            from open_deep_research.workflow.python_workflow import PythonWorkflow

            result = await PythonWorkflow().ainvoke(state, config, start_at="research")
        else:
            result = await builder.compile().ainvoke(state, config)
        assert "FINAL brand recall report" in result["report"]["final"]
        assert role_calls == Counter(
            {
                "public_signal": 2,
                "internal_knowledge": 1,
                "risk_assessment": 1,
                "response_strategy": 1,
            }
        )
        assert result["workflow"]["completed_tasks"][0].task_id == gap.task_id
        assert set(result["research"]["working_contexts"]) == set(ROLES)
        assert (
            result["research"]["working_context_scopes"]["public_signal"][
                "research_round"
            ]
            == 2
        )
        assert all(
            result["research"]["working_contexts"][role]["active_evidence_ids"]
            for role in ROLES
        )
        assert len(result["agents"]["public_signal"]["memory"]) == 3
        cfg = Configuration.from_runnable_config(config)
        store = create_research_graph_store(cfg, run_id=run_id)
        sources = [node for node in store.nodes.values() if node.node_type == "Source"]
        # Same URL from two same-turn tools resolves to one persisted source.
        assert len(sources) == 3
        assert {node.role for node in sources} == set(ROLES[:2])
        assert len(tool_calls) == 6
        assert result["runtime"]["budget"]["tool_calls"] == 6
        # 7 initial + 8 SDK + 3 extraction + 5 context + 5 role reports + 2 review + 1 section.
        assert result["runtime"]["budget"]["model_calls"] == 31
        assert result["runtime"]["budget"]["input_tokens"] == 70 + 8 * 10 + 16 * 7
        assert result["runtime"]["budget"]["output_tokens"] == 7 + 8 * 4 + 16 * 3
        assert all(node.run_id == run_id for node in store.nodes.values())
        evidence = sorted(
            (node.node_id, node.node_type, node.role, node.task_id)
            for node in store.nodes.values()
        )
        clear_research_graph_store_cache()
        return result, evidence, events, tool_calls

    return asyncio.run(scenario())


def test_compiled_workflow_graph_handoff_gap_followup_and_final_report(
    monkeypatch, tmp_path
):
    run_workflow_fixture(monkeypatch, tmp_path)


@pytest.mark.parametrize("role", ROLES)
def test_short_history_does_not_roll_and_private_memory_is_role_local(tmp_path, role):
    services = Services()
    ws = workspace(tmp_path, role, services=services)
    runtime = scripted_tool_runtime(
        ws,
        role=role,
        agent_state={
            "memory": [{"content": "private retained"}],
            "rolling_summary": "previous progress",
        },
    )
    result = asyncio.run(runtime.run("brand task"))
    assert services.calls["RollingCompactOutput"] == 0
    assert result.rolling_summary == "previous progress"
    for call in runtime.model.calls:
        text = json.dumps(call.input)
        assert "private retained" in text
        assert text.count("<SDK Rolling Summary>") == 1
        assert text.count("<SDK Research Context>") == 1
    assert runtime.harness.last_settlement.succeeded


def long_runtime(
    tmp_path, *, role="risk_assessment", summary_error=False, budget=None, recent=1
):
    services = Services(summary_error=summary_error)
    config = Configuration(
        research_graph_enabled=False,
        research_model="deepseek:deepseek-chat",
        research_graph_context_capacity_tokens=10_000,
        recent_raw_steps=recent,
        max_react_tool_calls=20,
        max_structured_output_retries=1,
        budget_enabled=budget is not None,
        max_model_calls=budget,
    )
    ws = workspace(tmp_path, role, services=services, config=config)

    async def search(query, limit=3):
        return f"RAW-{query} " + "important analysis and decision details " * 180

    tool = _tool(role, coroutine=search)
    steps = [
        [
            ResponseReasoningItem(
                id=f"r-{n}",
                type="reasoning",
                summary=[],
                content=[{"type": "reasoning_text", "text": f"REASON-{n}"}],
            ),
            function_call(tool.name, {"query": f"step-{n}"}, call_id=f"step-{n}"),
        ]
        for n in range(8)
    ]
    steps.append([assistant_message("long research completed")])
    runtime = scripted_tool_runtime(
        ws,
        role=role,
        tool=tool,
        steps=steps,
        agent_state={"memory": [{"content": "PRIVATE-ANCHOR"}]},
    )
    return runtime, services


@pytest.mark.parametrize(
    "role", ["risk_assessment", "response_strategy", "public_signal"]
)
def test_rolling_compaction_continues_repeatedly_with_native_reasoning_pairs(
    tmp_path, role
):
    runtime, services = long_runtime(tmp_path, role=role)
    result = asyncio.run(runtime.run("CURRENT-TASK-ANCHOR"))
    assert "long research completed" in result.report
    assert services.calls["RollingCompactOutput"] >= 2
    assert result.budget["model_calls"] == 9 + services.calls["RollingCompactOutput"]
    assert (
        result.budget["input_tokens"]
        == 9 * 10 + 7 * services.calls["RollingCompactOutput"]
    )
    assert result.budget["tool_calls"] == 8
    for call in runtime.model.calls:
        text = json.dumps(call.input)
        assert "CURRENT-TASK-ANCHOR" in text and "PRIVATE-ANCHOR" in text
        assert text.count("<SDK Rolling Summary>") <= 1
        calls = {
            item["call_id"]
            for item in call.input
            if item.get("type") == "function_call"
        }
        outputs = {
            item["call_id"]
            for item in call.input
            if item.get("type") == "function_call_output"
        }
        assert calls == outputs
        reasoning_ids = {
            item["id"].removeprefix("r-")
            for item in call.input
            if item.get("type") == "reasoning"
        }
        assert reasoning_ids == {cid.removeprefix("step-") for cid in calls}
    assert all(
        result.observation.startswith("RAW-")
        for result in runtime.harness.results.values()
    )
    assert runtime.harness.last_settlement.succeeded


def test_rolling_failure_retains_raw_history_and_actual_usage(tmp_path):
    runtime, services = long_runtime(tmp_path, summary_error=True)
    with pytest.raises(SDKContextLimitExceeded, match="failed safely") as error:
        asyncio.run(runtime.run("CURRENT-TASK"))
    assert services.calls["RollingCompactOutput"] == 1
    assert runtime.workspace.rolling_summary == ""
    assert runtime.harness._compacted_turns == set()
    assert runtime.harness.last_settlement.succeeded
    assert error.value.budget_usage["model_calls"] == len(runtime.model.calls) + 1
    assert all(
        record.observation.startswith("RAW-")
        for record in runtime.harness.results.values()
    )
    assert any(kind == "rolling_compact_failed" for kind, _ in runtime.harness.events)


@pytest.mark.parametrize(
    "status", ["unknown", "pending", "running", "failed", "error", "cancelled"]
)
def test_non_success_business_status_never_enters_graph(tmp_path, status):
    async def search(query, limit=3):
        return json.dumps({"status": status, "text": "not accepted as evidence"})

    ws = workspace(tmp_path)
    runtime = scripted_tool_runtime(ws, tool=_tool(coroutine=search))
    result = asyncio.run(runtime.run("brand task"))
    assert not ws.store.nodes and not ws.receipts
    assert runtime.harness.results["lookup"].status in {"failed", "cancelled"}
    assert result.budget["tool_calls"] == 1
    assert runtime.harness.last_settlement.succeeded


@pytest.mark.parametrize("failure", ["write", "context", "receipt"])
def test_failed_graph_stage_resumes_without_tool_or_extractor_replay(tmp_path, failure):
    async def scenario():
        services = Services(context_failures=1 if failure == "context" else 0)

        class Store(_Store):
            def write_delta(self, delta):
                receipt = super().write_delta(delta)
                return (
                    receipt.model_copy(update={"node_ids": []})
                    if failure == "receipt" and self.writes == 1
                    else receipt
                )

        store = Store()
        store.fail = failure == "write"
        ws = workspace(tmp_path, services=services, store=store)

        def second(call):
            assert "Official brake investigation" in json.dumps(call.input)
            assert not ws.receipts
            assert ws.pending_ingestions
            store.fail = False
            return [assistant_message("completed")]

        runtime = scripted_tool_runtime(
            ws,
            steps=[
                [function_call("web_search", {"query": "brand"}, call_id="lookup")],
                ModelStep.respond(second),
            ],
        )
        result = await runtime.run("brand task")
        assert services.calls["GraphExtractionOutput"] == 1
        assert runtime.harness.last_settlement.succeeded
        assert not ws.pending_ingestions
        assert ws.working_context.active_evidence_ids
        assert runtime.harness.results["lookup"].consumed
        assert result.budget["tool_calls"] == 1
        assert result.budget["model_calls"] == (6 if failure == "context" else 5)

    asyncio.run(scenario())


def test_working_context_scope_and_reducer_do_not_leak_runs_or_mutate_parent(tmp_path):
    first = workspace(tmp_path, run_id="run-one")
    first.working_context.recent_progress = "only run one"
    first.working_context.open_gaps = ["run-one gap"]
    patch = first.state_update()["research"]
    other = workspace(tmp_path, run_id="run-two", research_state=patch)
    assert other.working_context.recent_progress == ""
    followup = workspace(
        tmp_path, run_id="run-one", research_state=patch, round=2, task_id="gap-task"
    )
    followup.working_context.open_gaps.append("new gap")
    assert patch["working_contexts"]["public_signal"]["open_gaps"] == ["run-one gap"]
    merged = research_reducer(patch, other.state_update()["research"])
    assert merged["run_id"] == "run-two"
    assert merged["working_contexts"]["public_signal"]["recent_progress"] == ""
    assert merged["working_context_scopes"]["public_signal"]["run_id"] == "run-two"


def test_parallel_same_source_is_canonical_and_runs_stay_isolated(tmp_path):
    async def scenario():
        store = _Store()
        services = Services()
        first = workspace(tmp_path, store=store, services=services)
        second = workspace(
            tmp_path, "internal_knowledge", store=store, services=services
        )
        results = await asyncio.gather(
            scripted_tool_runtime(first).run("brand"),
            scripted_tool_runtime(second, role="internal_knowledge").run("brand"),
        )
        assert services.calls["GraphExtractionOutput"] == 1
        assert (
            len([node for node in store.nodes.values() if node.node_type == "Source"])
            == 1
        )
        assert all(
            receipt.has_complete_node_references
            for ws in (first, second)
            for receipt in ws.receipts.values()
        )
        assert sum(result.budget["tool_calls"] for result in results) == 2
        isolated = workspace(
            tmp_path,
            "risk_assessment",
            store=store,
            services=services,
            run_id="other-run",
        )
        model = ScriptedModel([[assistant_message("no evidence in other run")]])
        runtime = _runtime(
            "risk_assessment",
            config=isolated.configurable,
            workspace=isolated,
            tools=[],
            model=model,
        )
        await runtime.run("brand")
        assert "https://example.test/brand" not in json.dumps(model.calls[0].input)

    asyncio.run(scenario())


def test_nested_retry_budget_guard_preserves_final_reserve_and_counts_actual_attempts():
    from open_deep_research.budget import ModelBudgetExceeded, structured_output_chain

    class Invalid(Services):
        def with_structured_output(self, schema, include_raw=False):
            async def respond(messages, config=None):
                await self.raw.ainvoke(messages, config)
                return {"raw": None, "parsed": None, "parsing_error": None}

            return RunnableLambda(respond)

    async def scenario():
        config = Configuration(
            budget_enabled=True, max_model_calls=2, reserve_final_report_call=True
        )
        with budget_capture() as usage, model_budget_admission(config, lambda: usage):
            with pytest.raises(ModelBudgetExceeded):
                await ainvoke_model_with_budget(
                    structured_output_chain(
                        Invalid(), WorkingContextDelta, max_attempts=2
                    ),
                    [AIMessage(content="prompt")],
                )
        assert usage["model_calls"] == 1
        assert usage["input_tokens"] == 7

    asyncio.run(scenario())


def test_partial_batch_write_retry_keeps_original_receipts_and_extraction(tmp_path):
    async def scenario():
        class Partial(_Store):
            def write_delta(self, delta):
                if self.writes == 1:
                    self.writes += 1
                    raise OSError("second write failed")
                return super().write_delta(delta)

        services = Services()
        store = Partial()
        config = Configuration(
            research_graph_enabled=True,
            research_graph_backend="memory",
            research_graph_transcript_dir=str(tmp_path),
            recent_raw_steps=0,
            research_graph_extraction_batch_tokens=120,
            max_structured_output_retries=1,
        )
        ws = workspace(tmp_path, services=services, store=store, config=config)

        async def search(query, limit=3):
            return json.dumps(
                {
                    "type": "research_raw_search",
                    "results": [
                        json.loads(_payload(label))["results"][0]
                        for label in ("one", "two")
                    ],
                }
            )

        def second(call):
            assert not ws.receipts
            assert "https://example.test/one" in json.dumps(call.input)
            assert "https://example.test/two" in json.dumps(call.input)
            assert (
                len(
                    [
                        node
                        for node in store.nodes.values()
                        if node.node_type == "Source"
                    ]
                )
                == 1
            )
            return [assistant_message("done")]

        runtime = scripted_tool_runtime(
            ws,
            tool=_tool(coroutine=search),
            steps=[
                [
                    function_call(
                        "web_search", {"query": "two sources"}, call_id="lookup"
                    )
                ],
                ModelStep.respond(second),
            ],
        )
        result = await runtime.run("brand")
        assert services.calls["GraphExtractionOutput"] == 2
        assert store.writes == 3
        assert (
            len([node for node in store.nodes.values() if node.node_type == "Source"])
            == 2
        )
        assert len(next(iter(ws.receipts.values())).source_ids) == 2
        assert runtime.harness.last_settlement.succeeded
        assert result.budget["tool_calls"] == 1
        assert result.budget["model_calls"] == 6

    asyncio.run(scenario())


def test_permanent_invalid_receipt_is_pending_and_never_compacted(tmp_path):
    class Invalid(_Store):
        def write_delta(self, delta):
            return (
                super()
                .write_delta(delta)
                .model_copy(update={"source_ids": ["SRC_fabricated"]})
            )

    services = Services()
    ws = workspace(tmp_path, store=Invalid(), services=services)
    runtime = scripted_tool_runtime(ws)
    with pytest.raises(ResearchGraphSettlementError) as error:
        asyncio.run(runtime.run("brand"))
    assert error.value.context_settlement.pending_call_ids == ("lookup",)
    assert not ws.receipts and not runtime.harness._compacted
    assert not runtime.harness.results["lookup"].consumed
    assert services.calls["GraphExtractionOutput"] == 1
    assert services.calls["WorkingContextDelta"] == 0
    assert (
        "Official brake investigation" in runtime.harness.results["lookup"].observation
    )


def test_consumer_refresh_reads_new_commit_between_model_turns(tmp_path):
    async def scenario():
        store = _Store()
        seed = workspace(tmp_path, store=store)
        await scripted_tool_runtime(seed).run("brand")
        reader_ws = workspace(tmp_path, "risk_assessment", store=store)

        async def refresh(query, limit=3):
            writer_ws = workspace(tmp_path, store=store, round=2, task_id="followup")
            await scripted_tool_runtime(
                writer_ws,
                steps=[
                    [
                        function_call(
                            "web_search", {"query": "new-recall"}, call_id="lookup"
                        )
                    ],
                    [assistant_message("new evidence")],
                ],
            ).run("brand")
            return "consumer analysis only"

        runtime = scripted_tool_runtime(
            reader_ws,
            role="risk_assessment",
            tool=_tool("risk_assessment", coroutine=refresh),
        )
        await runtime.run("brand")
        first, second = (json.dumps(call.input) for call in runtime.model.calls)
        assert "new-recall" not in first
        assert "https://example.test/new-recall" in second
        assert reader_ws.metrics.as_dict()["context_manager_calls"] == 2
        assert "consumer analysis only" not in json.dumps(
            [node.model_dump() for node in store.nodes.values()]
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "exit_path", ["normal", "complete", "max_turns", "model_error", "cancel"]
)
def test_graph_budget_and_settlement_for_terminal_paths(tmp_path, exit_path):
    async def scenario():
        services = Services()
        ws = workspace(tmp_path, services=services)
        name = "ResearchComplete" if exit_path == "complete" else "web_search"

        async def evidence(query, limit=3):
            return _payload("terminal")

        tool = _tool(
            name=name,
            domain="core" if exit_path == "complete" else "web_search",
            coroutine=evidence,
        )

        async def final(call):
            if exit_path == "model_error":
                raise OSError("provider failed")
            if exit_path == "cancel":
                raise asyncio.CancelledError()
            return [assistant_message("done")]

        if exit_path == "max_turns":
            ws.configurable.max_react_tool_calls = 1
        runtime = scripted_tool_runtime(
            ws,
            tool=tool,
            steps=[
                [function_call(name, {"query": "brand"}, call_id="terminal")],
                ModelStep.respond(final),
            ],
        )
        if exit_path in {"model_error", "cancel"}:
            with pytest.raises(
                OSError if exit_path == "model_error" else asyncio.CancelledError
            ) as error:
                await runtime.run("brand")
            assert error.value.context_settlement.succeeded
            assert error.value.budget_usage["tool_calls"] == 1
            assert error.value.budget_usage["model_calls"] == 4
            assert len(ws.receipts) == 1
        else:
            result = await runtime.run("brand")
            assert result.budget["tool_calls"] == 1
            assert runtime.harness.last_settlement.succeeded
            assert bool(ws.receipts) == (exit_path != "complete")
            assert result.budget["model_calls"] == (
                5 if exit_path == "normal" else 4 if exit_path == "max_turns" else 2
            )
        assert all(
            record.status == "succeeded" for record in runtime.harness.results.values()
        )

    asyncio.run(scenario())


def test_no_compaction_bypasses_budget_and_final_report_reserve(tmp_path):
    runtime, services = long_runtime(tmp_path, budget=3)
    result = asyncio.run(runtime.run("brand"))
    assert len(runtime.model.calls) == 2
    assert services.calls["RollingCompactOutput"] == 0
    assert result.budget["model_calls"] == 2
    assert result.budget["degradation_reasons"]


def test_pending_evidence_is_pinned_under_rolling_pressure(tmp_path):
    services = Services()
    store = _Store()
    store.fail = True
    config = Configuration(
        research_graph_enabled=True,
        research_graph_backend="memory",
        research_graph_transcript_dir=str(tmp_path),
        research_graph_context_capacity_tokens=12_000,
        recent_raw_steps=1,
        max_structured_output_retries=1,
    )
    ws = workspace(tmp_path, services=services, store=store, config=config)

    async def huge(query, limit=3):
        return _payload(query) + "UNCOMMITTED" * 12_000

    runtime = scripted_tool_runtime(ws, tool=_tool(coroutine=huge))
    with pytest.raises(SDKContextLimitExceeded) as error:
        asyncio.run(runtime.run("brand"))
    assert error.value.context_settlement.pending_call_ids == ("lookup",)
    assert not ws.receipts
    assert services.calls["RollingCompactOutput"] == 0
    assert "UNCOMMITTED" in runtime.harness.results["lookup"].observation


def test_real_local_mcp_stdio_transport_is_collected_and_persisted(tmp_path):
    import sys

    from langchain_mcp_adapters.tools import load_mcp_tools
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    script = tmp_path / "fixture_mcp.py"
    script.write_text(
        """from mcp.server.fastmcp import FastMCP
server = FastMCP("phase2-fixture")
@server.tool()
def lookup(query: str) -> str:
    return "Local MCP brand recall evidence from synthetic source, query=" + query
if __name__ == "__main__":
    server.run()
""",
        encoding="utf-8",
    )

    async def scenario():
        async with stdio_client(
            StdioServerParameters(command=sys.executable, args=[str(script)])
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await load_mcp_tools(session)
                tool = next(tool for tool in tools if tool.name == "lookup")
                tool.name = "knowledge__lookup"
                tool.metadata = {"tool_domain": "rag"}
                ws = workspace(tmp_path, "internal_knowledge")
                runtime = scripted_tool_runtime(
                    ws,
                    role="internal_knowledge",
                    tool=tool,
                    steps=[
                        [
                            function_call(
                                tool.name, {"query": "brand"}, call_id="local-mcp"
                            )
                        ],
                        [assistant_message("MCP evidence consumed")],
                    ],
                )
                result = await runtime.run("brand")
                assert result.budget["tool_calls"] == 1
                assert runtime.harness.results["local-mcp"].status == "succeeded"
                assert len(ws.receipts) == 1
                assert any(
                    node.node_type == "Evidence" for node in ws.store.nodes.values()
                )
                assert runtime.harness.last_settlement.succeeded

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "provider,api,model_name",
    [
        ("openai", "responses", "gpt-fixture"),
        ("deepseek", "chat_completions", "deepseek-flash"),
        ("volcengine", "chat_completions", "ep-fixture"),
    ],
)
def test_rolling_with_official_models_and_mock_http_serialization(
    tmp_path, provider, api, model_name
):
    import httpx2
    from openai import AsyncOpenAI
    from test_sdk_models import _reply

    from open_deep_research.models.sdk import sdk_model

    async def scenario():
        runtime, services = long_runtime(tmp_path)
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            turn = len(requests)
            assert request.url.path.endswith(
                "/responses" if api == "responses" else "/chat/completions"
            )
            payload = _reply(api, 1 if turn <= 8 else 2, model_name)
            if turn <= 8:
                if api == "responses":
                    call = payload["output"][0]
                    call.update(
                        id=f"fc-{turn}", call_id=f"step-{turn}", name="rag_search"
                    )
                else:
                    message = payload["choices"][0]["message"]
                    message["tool_calls"][0]["id"] = f"step-{turn}"
                    message["tool_calls"][0]["function"]["name"] = "rag_search"
                    if provider == "deepseek":
                        message["reasoning_content"] = f"REASON-{turn}"
            return httpx2.Response(200, json=payload)

        async with AsyncOpenAI(
            api_key="fixture",
            max_retries=0,
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
        ) as client:
            async with sdk_model(
                f"{provider}:{model_name}", client=client, api=api
            ) as model:
                runtime.model = model
                result = await runtime.run("CURRENT-TASK")
            assert not client.is_closed()
        assert services.calls["RollingCompactOutput"] >= 2
        assert len(requests) == 9
        assert (
            result.budget["model_calls"] == 9 + services.calls["RollingCompactOutput"]
        )
        assert (
            result.budget["input_tokens"]
            == 9 * 12 + services.calls["RollingCompactOutput"] * 7
        )
        for request in requests:
            messages = request["input"] if api == "responses" else request["messages"]
            text = json.dumps(messages)
            assert text.count("<SDK Rolling Summary>") <= 1
            if api == "chat_completions":
                calls = {
                    call["id"] for msg in messages for call in msg.get("tool_calls", [])
                }
                outputs = {
                    msg["tool_call_id"] for msg in messages if msg["role"] == "tool"
                }
                assert calls == outputs
                if provider == "deepseek":
                    for msg in messages:
                        if msg.get("tool_calls"):
                            n = msg["tool_calls"][0]["id"].removeprefix("step-")
                            assert msg["reasoning_content"] == f"REASON-{n}"
            else:
                assert {
                    i["call_id"] for i in messages if i.get("type") == "function_call"
                } == {
                    i["call_id"]
                    for i in messages
                    if i.get("type") == "function_call_output"
                }

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "summary",
    ["", "will grow " * 5000, "Finding EVD_12345678901234567890 is verified."],
    ids=["empty", "oversized", "unknown_reference"],
)
def test_invalid_summary_fails_without_committing_history_window(tmp_path, summary):
    runtime, services = long_runtime(tmp_path)
    services.summary = summary
    with pytest.raises(SDKContextLimitExceeded, match="failed safely") as error:
        asyncio.run(runtime.run("brand"))
    assert runtime.workspace.rolling_summary == ""
    assert not runtime.harness._compacted_turns
    assert error.value.budget_usage["model_calls"] == len(runtime.model.calls) + 1
    assert runtime.harness.last_settlement.succeeded


def test_parallel_cancellation_settles_success_and_accounts_cancelled_sibling(tmp_path):
    async def scenario():
        ws = workspace(tmp_path)
        successful = asyncio.Event()
        blocked = asyncio.Event()

        async def search(query, limit=3):
            if query == "fast":
                successful.set()
                return _payload("fast")
            blocked.set()
            await asyncio.Event().wait()

        tool = _tool(coroutine=search)
        runtime = scripted_tool_runtime(
            ws,
            tool=tool,
            steps=[
                [
                    function_call(tool.name, {"query": "fast"}, call_id="fast"),
                    function_call(tool.name, {"query": "blocked"}, call_id="blocked"),
                ]
            ],
        )
        running = asyncio.create_task(runtime.run("brand"))
        await asyncio.wait_for(successful.wait(), 3)
        await asyncio.wait_for(blocked.wait(), 3)
        running.cancel()
        with pytest.raises(asyncio.CancelledError) as error:
            await running
        assert error.value.context_settlement.succeeded
        assert runtime.harness.results["fast"].status == "succeeded"
        assert runtime.harness.results["blocked"].status == "cancelled"
        assert len(ws.receipts) == 1
        assert error.value.budget_usage["model_calls"] == 3
        assert error.value.budget_usage["tool_calls"] == 2
        assert error.value.budget_usage["input_tokens"] == 10 + 2 * 7

    asyncio.run(scenario())


def test_graph_producer_rolls_while_preserving_committed_ids_and_recent_raw_step(
    tmp_path,
):
    services = Services()
    config = Configuration(
        research_graph_enabled=True,
        research_graph_backend="memory",
        research_graph_transcript_dir=str(tmp_path),
        research_graph_context_capacity_tokens=22_000,
        recent_raw_steps=1,
        max_react_tool_calls=40,
        max_structured_output_retries=1,
    )
    ws = workspace(tmp_path, services=services, config=config)
    steps = [
        [
            ResponseReasoningItem(
                id=f"r-{n}",
                type="reasoning",
                summary=[],
                content=[
                    {
                        "type": "reasoning_text",
                        "text": "decision about verified brand evidence " * 160,
                    }
                ],
            ),
            function_call("web_search", {"query": f"brand-{n}"}, call_id=f"graph-{n}"),
        ]
        for n in range(24)
    ]
    steps.append([assistant_message("graph long research completed")])
    runtime = scripted_tool_runtime(ws, steps=steps)
    result = asyncio.run(runtime.run("protected brand assignment"))
    assert runtime.sdk_result.final_output == "graph long research completed"
    assert "public_signal GRAPH REPORT" in result.report
    assert services.calls["RollingCompactOutput"] >= 2
    assert services.calls["GraphExtractionOutput"] == 24
    assert result.budget["tool_calls"] == 24
    assert (
        result.budget["model_calls"]
        == 25 + 24 * 2 + 1 + services.calls["RollingCompactOutput"]
    )
    for n, call in enumerate(runtime.model.calls[1:], 1):
        text = json.dumps(call.input)
        assert text.count("<SDK Research Context>") == 1
        assert text.count("<SDK Rolling Summary>") <= 1
        assert f"Official brake investigation brand-{n - 1}" in text
        assert "protected brand assignment" in text
    summary_ids = set(re.findall(r"(?:SRC|EVD)_[a-f0-9]{20}", result.rolling_summary))
    assert summary_ids and summary_ids.issubset(
        {node.node_id for node in ws.store.nodes.values()}
    )
    assert not ws.pending_ingestions
    assert runtime.harness.last_settlement.succeeded


def test_rolling_without_reported_usage_does_not_estimate_billed_tokens(tmp_path):
    runtime, services = long_runtime(tmp_path)
    services.raw = FakeMessagesListChatModel(
        responses=[
            AIMessage(content="summary", response_metadata={"model_name": "no-usage"})
        ]
    )
    result = asyncio.run(runtime.run("brand"))
    assert services.calls["RollingCompactOutput"] >= 2
    assert result.budget["model_calls"] == 9 + services.calls["RollingCompactOutput"]
    assert result.budget["input_tokens"] == 90
    assert result.budget["output_tokens"] == 36


def test_nested_calls_inside_one_tool_share_actual_budget_before_tool_exit(tmp_path):
    async def scenario():
        config = Configuration(
            research_graph_enabled=False,
            budget_enabled=True,
            max_model_calls=3,
            reserve_final_report_call=True,
        )
        model = FakeMessagesListChatModel(
            responses=[
                AIMessage(
                    content="nested",
                    response_metadata={"model_name": "nested"},
                    usage_metadata={
                        "input_tokens": 7,
                        "output_tokens": 3,
                        "total_tokens": 10,
                    },
                )
            ]
        )

        async def search(query, limit=3):
            await ainvoke_model_with_budget(model, [AIMessage(content="one")])
            await ainvoke_model_with_budget(model, [AIMessage(content="two")])
            pytest.fail(
                "Second nested call must be rejected before reaching a provider"
            )

        ws = workspace(tmp_path, config=config)
        runtime = scripted_tool_runtime(ws, tool=_tool(coroutine=search))
        result = await runtime.run("brand")
        assert len(runtime.model.calls) == 1
        assert result.budget["model_calls"] == 2
        assert result.budget["input_tokens"] == 17
        assert runtime.harness.results["lookup"].status == "failed"
        assert (
            "Nested model budget exhausted"
            in runtime.harness.results["lookup"].observation
        )

    asyncio.run(scenario())


def test_grouped_followup_tasks_keep_all_objectives_and_source_associations(tmp_path):
    state = _initial_state()
    state["workflow"]["round"] = 2
    state["workflow"]["pending_tasks"] = [
        ResearchTask(
            task_id=f"gap-{n}",
            target_role="public_signal",
            objective=f"Resolve brand gap {n}",
            evidence_needed=f"brand source {n}",
            reason="changes risk",
        )
        for n in range(2)
    ]
    descriptor = business_agent._task_descriptor(state, "public_signal", "phase2-run")
    assert descriptor.task_id == "gap-0"
    assert descriptor.related_task_ids == ("gap-0", "gap-1")
    assert (
        "brand gap 0" in descriptor.objective and "brand gap 1" in descriptor.objective
    )
    ws = workspace(tmp_path, round=2)
    ws.task = descriptor
    ws.scope = ws.scope.model_copy(update={"task_id": descriptor.task_id})
    runtime = scripted_tool_runtime(ws)
    asyncio.run(runtime.run("Resolve both brand gaps"))
    sources = [node for node in ws.store.nodes.values() if node.node_type == "Source"]
    assert sources[0].properties["metadata"]["research_task_ids"] == ["gap-0", "gap-1"]
    assert sources[0].research_round == 2
    assert sources[0].task_id == "gap-0"


def test_unknown_deployment_alias_has_a_bounded_application_window(tmp_path):
    config = Configuration(
        research_graph_enabled=False,
        research_model="volcengine:ep-custom",
        research_graph_context_capacity_tokens=None,
    )
    ws = workspace(tmp_path, config=config)
    runtime = scripted_tool_runtime(ws)
    with pytest.raises(SDKContextLimitExceeded, match="Protected task"):
        asyncio.run(runtime.run("protected task " * 50_000))
    assert not runtime.model.calls
    assert runtime.harness.last_settlement.succeeded
