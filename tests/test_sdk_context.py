"""Exercise the real SDK loop and existing Graph algorithms with offline models."""

import asyncio
import json
import re

import pytest
from agents import Agent, FileSearchTool, function_tool
from agents.exceptions import MaxTurnsExceeded, UserError
from agents.mcp import MCPServer
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from agents.tool_context import ToolContext
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from mcp.types import CallToolResult, GetPromptResult, ListPromptsResult, TextContent
from mcp.types import Tool as MCPTool
from openai.types.responses import ResponseFileSearchToolCall, ResponseReasoningItem

from open_deep_research.configuration import Configuration
from open_deep_research.research_graph import (
    GraphExtractionOutput,
    InMemoryResearchGraphStore,
    ResearchGraphConsumerStrategy,
    ResearchGraphProducerStrategy,
    ResearchWorkspace,
    WorkingContextDelta,
)
from open_deep_research.research_graph.workspace import TaskDescriptor
from open_deep_research.runtime.sdk_context import (
    ResearchGraphSettlementError,
    SDKContextHarness,
)

EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
SEARCH_SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string"}},
    "required": ["label"],
    "additionalProperties": False,
}


def _payload(label):
    return json.dumps(
        {
            "type": "research_raw_search",
            "results": [
                {
                    "url": f"https://example.test/{label}",
                    "title": f"Notice {label}",
                    "content": f"Official brake investigation {label}. " * 60,
                }
            ],
        }
    )


class _GraphModel:
    """Replace only graph model inference, keeping extraction/normalization real."""

    def __init__(self):
        self.extractions = []
        self.context_updates = []

    def with_structured_output(self, schema, include_raw=False):
        async def respond(messages):
            prompt = str(messages[0].content)
            if schema is GraphExtractionOutput:
                source_ids = list(
                    dict.fromkeys(re.findall(r'"source_id":\s*"([^"]+)"', prompt))
                )
                self.extractions.append(source_ids)
                parsed = GraphExtractionOutput(
                    evidences=[
                        {
                            "source_id": source_id,
                            "statement": f"Brake evidence from {source_id}",
                        }
                        for source_id in source_ids
                    ]
                )
            elif schema is WorkingContextDelta:
                self.context_updates.append(prompt)
                parsed = WorkingContextDelta(
                    recent_progress="graph updated with committed evidence"
                )
            else:
                raise AssertionError(f"Unexpected graph model schema: {schema}")
            return {
                "raw": AIMessage(
                    content="",
                    usage_metadata={
                        "input_tokens": 7,
                        "output_tokens": 3,
                        "total_tokens": 10,
                    },
                ),
                "parsed": parsed,
                "parsing_error": None,
            }

        return RunnableLambda(respond)


class _Store(InMemoryResearchGraphStore):
    def __init__(self):
        super().__init__()
        self.writes = 0
        self.fail = False

    def write_delta(self, delta):
        self.writes += 1
        if self.fail:
            raise OSError("fixture graph write failed")
        return super().write_delta(delta)


def _workspace(
    tmp_path,
    *,
    run_id="run-a",
    producer=True,
    store=None,
    strategy=None,
    recent_raw_steps=0,
    task_id=None,
):
    graph_model = _GraphModel()
    store = store if store is not None else _Store()
    role = "public_signal" if producer else "risk_assessment"
    workspace = ResearchWorkspace(
        strategy=strategy
        or (
            ResearchGraphProducerStrategy()
            if producer
            else ResearchGraphConsumerStrategy()
        ),
        research_state={},
        role=role,
        run_id=run_id,
        task=TaskDescriptor(
            task_id or f"task-{role}",
            "Investigate brake evidence",
            "Official brake notices",
        ),
        configurable=Configuration(
            research_graph_enabled=True,
            research_graph_backend="memory",
            research_graph_transcript_dir=str(tmp_path),
            recent_raw_steps=recent_raw_steps,
            research_graph_max_retrieved_nodes=12,
            research_graph_max_retrieved_edges=16,
        ),
        model_factory=lambda _name, _tokens: graph_model,
        store=store,
    )
    return workspace, store, graph_model


def _tool(
    harness, invoke=None, *, name="web_search", errors_as_results=True, is_success=None
):
    async def search(context, arguments):
        return _payload(json.loads(arguments)["label"])

    return harness.function_tool(
        name=name,
        description="Fixture research tool",
        params_json_schema=SEARCH_SCHEMA,
        invoke=invoke or search,
        errors_as_results=errors_as_results,
        is_success=is_success,
    )


def _context(call):
    return next(
        item["content"]
        for item in call.input
        if item.get("role") == "user"
        and "<SDK Research Context>" in str(item.get("content"))
    )


def _outputs(call):
    return {
        item["call_id"]: item["output"]
        for item in call.input
        if item.get("type") == "function_call_output"
    }


def test_parallel_sdk_loop_commits_one_batch_before_second_model(tmp_path):
    async def scenario():
        workspace, store, graph_model = _workspace(tmp_path)
        harness = SDKContextHarness(workspace)
        started = set()
        barrier = asyncio.Event()

        async def search(context, arguments):
            label = json.loads(arguments)["label"]
            started.add(label)
            if len(started) == 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), 1)
            return _payload(label)

        def second_model(call):
            assert store.writes == 1
            assert len(graph_model.extractions) == 1
            assert len(graph_model.extractions[0]) == 2
            assert "graph updated with committed evidence" in _context(call)
            assert all(
                text.startswith("[Research result compacted.")
                for text in _outputs(call).values()
            )
            assert set(_outputs(call)) == {"call-a", "call-b"}
            return [assistant_message("done")]

        reasoning = ResponseReasoningItem(
            id="reasoning-1", type="reasoning", summary=[]
        )
        model = ScriptedModel(
            [
                [
                    reasoning,
                    function_call("web_search", {"label": "a"}, call_id="call-a"),
                    function_call("web_search", {"label": "b"}, call_id="call-b"),
                ],
                ModelStep.respond(second_model),
            ]
        )
        original = [{"role": "user", "content": "Research brake safety"}]
        result = await harness.run(
            Agent(name="producer", model=model, tools=[_tool(harness, search)]),
            original,
        )
        assert result.final_output == "done"
        assert original == [{"role": "user", "content": "Research brake safety"}]
        assert harness.last_settlement.succeeded
        assert [event for event in harness.events if event[0] == "batch_start"] == [
            ("batch_start", "2")
        ]
        events = harness.events
        batch_index = events.index(("batch_start", "2"))
        assert all(
            events.index(("tool_end", call_id)) < batch_index
            for call_id in ("call-a", "call-b")
        )
        assert batch_index < events.index(("llm_start", "2"))
        assert events.index(("input_filter", "1")) < events.index(("llm_start", "1"))
        assert '"current_task": "Investigate brake evidence"' in _context(
            model.calls[0]
        )
        assert (
            sum(
                "<SDK Research Context>" in str(item.get("content", ""))
                for item in model.calls[1].input
            )
            == 1
        )
        assert any(item.get("type") == "reasoning" for item in model.calls[1].input)
        assert not any(
            "<SDK Research Context>" in str(item.get("content", ""))
            for item in result.to_input_list()
        )
        assert (
            len([node for node in store.nodes.values() if node.node_type == "Evidence"])
            == 2
        )
        assert len(graph_model.context_updates) == 1
        model.assert_complete()

    asyncio.run(scenario())


def test_failed_non_evidence_and_business_error_results_do_not_enter_graph(tmp_path):
    async def scenario():
        workspace, store, graph_model = _workspace(tmp_path)
        harness = SDKContextHarness(workspace)

        async def search(context, arguments):
            label = json.loads(arguments)["label"]
            if label == "bad":
                raise LookupError("fixture failure")
            if label == "business-error":
                return {"ok": False, "data": _payload(label)}
            return _payload(label)

        model = ScriptedModel(
            [
                [
                    function_call(name, {"label": label}, call_id=label)
                    for name, label in [
                        ("web_search", "good"),
                        ("web_search", "bad"),
                        ("web_search", "business-error"),
                        ("think_tool", "thought"),
                    ]
                ],
                [assistant_message("done")],
            ]
        )
        await harness.run(
            Agent(
                name="producer",
                model=model,
                tools=[
                    _tool(
                        harness,
                        search,
                        is_success=lambda result: not isinstance(result, dict)
                        or result.get("ok") is True,
                    ),
                    _tool(harness, name="think_tool"),
                ],
            ),
            "assignment",
        )
        sources = [node for node in store.nodes.values() if node.node_type == "Source"]
        assert (
            len(sources) == 1
            and sources[0].properties["url"] == "https://example.test/good"
        )
        assert len(graph_model.extractions[0]) == 1
        assert harness.results["bad"].status == "failed"
        assert harness.results["bad"].error_type == "LookupError"
        assert harness.results["business-error"].status == "failed"
        outputs = _outputs(model.calls[1])
        assert not outputs["bad"].startswith("[Research result compacted.")
        assert outputs["thought"] == _payload("thought")

    asyncio.run(scenario())


def test_unwrapped_function_tool_errors_are_not_assumed_successful(tmp_path):
    @function_tool(name_override="web_search")
    async def swallowed_failure() -> str:
        raise ValueError("not evidence")

    async def scenario():
        workspace, store, graph_model = _workspace(tmp_path)
        harness = SDKContextHarness(workspace)
        model = ScriptedModel(
            [
                [function_call("web_search", {}, call_id="error")],
                [assistant_message("done")],
            ]
        )
        await harness.run(
            Agent(name="producer", model=model, tools=[swallowed_failure]), "assignment"
        )
        assert harness.results["error"].status == "unobserved"
        assert not store.nodes and not graph_model.extractions

    asyncio.run(scenario())


def test_consumer_retrieves_before_first_request_and_does_not_produce(tmp_path):
    async def scenario():
        producer, store, _ = _workspace(tmp_path)
        harness = SDKContextHarness(producer)
        await harness.run(
            Agent(
                name="producer",
                model=ScriptedModel(
                    [
                        [
                            function_call(
                                "web_search", {"label": "seed"}, call_id="seed"
                            )
                        ],
                        [assistant_message("done")],
                    ]
                ),
                tools=[_tool(harness)],
            ),
            "assignment",
        )
        consumer, _, graph_model = _workspace(tmp_path, producer=False, store=store)
        reader = SDKContextHarness(consumer, context_max_chars=4096)
        before = {key: value.model_dump() for key, value in store.nodes.items()}

        def inspect(call):
            assert "https://example.test/seed" in _context(call)
            assert "[Evidence]" in _context(call)
            assert "role=risk_assessment" in _context(call)
            assert len(_context(call)) <= 4096
            return [assistant_message("evidence read")]

        model = ScriptedModel(
            [
                [
                    function_call(
                        "web_search",
                        {"label": "consumer-only"},
                        call_id="consumer-tool",
                    )
                ],
                ModelStep.respond(inspect),
            ]
        )

        # The first request must already contain the seed before any consumer tool runs.
        async def inspect_first(context, arguments):
            assert "https://example.test/seed" in _context(model.calls[0])
            return _payload("consumer-only")

        await reader.run(
            Agent(name="consumer", model=model, tools=[_tool(reader, inspect_first)]),
            "assess",
        )
        assert {key: value.model_dump() for key, value in store.nodes.items()} == before
        assert not graph_model.extractions and not graph_model.context_updates
        assert consumer.metrics.as_dict()["graph_retrieval_calls"] == 1
        assert not any(event[0] == "batch_start" for event in reader.events)
        assert _outputs(model.calls[1])["consumer-tool"] == _payload("consumer-only")

    asyncio.run(scenario())


@pytest.mark.parametrize("refresh_every", [0, 1])
def test_consumer_refresh_is_explicit(tmp_path, refresh_every):
    async def scenario():
        workspace, _, _ = _workspace(tmp_path, producer=False)
        harness = SDKContextHarness(workspace, consumer_refresh_every=refresh_every)
        model = ScriptedModel(
            [
                [function_call("web_search", {"label": "a"}, call_id="a")],
                [assistant_message("done")],
            ]
        )
        await harness.run(
            Agent(name="reader", model=model, tools=[_tool(harness)]), "assignment"
        )
        calls = workspace.metrics.as_dict()["graph_retrieval_calls"]
        assert calls == (1 if refresh_every == 0 else 2)
        assert not any(event[0] == "batch_start" for event in harness.events)

    asyncio.run(scenario())


def test_write_failure_retains_raw_output_and_pending_batch(tmp_path):
    async def scenario():
        workspace, store, graph_model = _workspace(tmp_path)
        store.fail = True
        harness = SDKContextHarness(workspace)

        def second_model(call):
            assert _outputs(call)["a"] == _payload("a")
            assert not workspace.receipts
            return [assistant_message("done")]

        model = ScriptedModel(
            [
                [function_call("web_search", {"label": "a"}, call_id="a")],
                ModelStep.respond(second_model),
            ]
        )
        with pytest.raises(ResearchGraphSettlementError):
            await harness.run(
                Agent(name="producer", model=model, tools=[_tool(harness)]),
                "assignment",
            )
        assert not store.nodes and not workspace.receipts
        assert harness.last_settlement.pending_call_ids == ("a",)
        assert not harness.last_settlement.succeeded
        assert not harness.results["a"].consumed
        # Phase 2 resumes the retained write stage, rather than paying for the
        # same successful extraction again on exit settlement.
        assert len(graph_model.extractions) == 1
        assert workspace.metrics.as_dict().get("micro_compact_count", 0) == 0
        store.fail = False
        assert (await harness.settle("explicit_retry")).succeeded
        assert len(store.nodes) > 0
        assert harness.results["a"].consumed
        assert len(graph_model.extractions) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal", ["final_tool", "max_turns", "tool_exception"])
def test_remaining_batch_is_settled_on_normal_and_exception_exits(tmp_path, terminal):
    async def scenario():
        workspace, store, _ = _workspace(tmp_path)
        harness = SDKContextHarness(workspace)
        ready = asyncio.Event()

        async def good(context, arguments):
            ready.set()
            return _payload("good")

        async def bad(context, arguments):
            await ready.wait()
            raise LookupError("fixture tool exception")

        calls = [function_call("web_search", {"label": "good"}, call_id="good")]
        tools = [_tool(harness, good)]
        if terminal == "tool_exception":
            calls.append(function_call("bad", {"label": "bad"}, call_id="bad"))
            tools.append(_tool(harness, bad, name="bad", errors_as_results=False))
        model = ScriptedModel([calls])
        agent = Agent(
            name="producer",
            model=model,
            tools=tools,
            tool_use_behavior="stop_on_first_tool"
            if terminal == "final_tool"
            else "run_llm_again",
        )
        if terminal == "final_tool":
            result = await harness.run(agent, "assignment")
            assert result.final_output == _payload("good")
        else:
            with pytest.raises(
                MaxTurnsExceeded if terminal == "max_turns" else UserError
            ) as failure:
                await harness.run(agent, "assignment", max_turns=1)
            if terminal == "tool_exception":
                assert isinstance(failure.value.__cause__, LookupError)
        assert store.writes == 1
        assert harness.results["good"].consumed
        assert harness.last_settlement.succeeded
        assert harness.last_settlement.reason == (
            "completed" if terminal == "final_tool" else "exception"
        )
        assert harness.events.index(
            ("settlement", harness.last_settlement.reason)
        ) > harness.events.index(("batch_end", "1"))

    asyncio.run(scenario())


def test_cancel_settles_completed_evidence_and_records_cancelled_tool(tmp_path):
    async def scenario():
        workspace, store, _ = _workspace(tmp_path)
        harness = SDKContextHarness(workspace)
        good_ready, slow_ready = asyncio.Event(), asyncio.Event()

        async def invoke(context, arguments):
            if json.loads(arguments)["label"] == "good":
                good_ready.set()
                return _payload("good")
            slow_ready.set()
            await asyncio.Event().wait()

        model = ScriptedModel(
            [
                [
                    function_call("web_search", {"label": "good"}, call_id="good"),
                    function_call("web_search", {"label": "slow"}, call_id="slow"),
                ]
            ]
        )
        task = asyncio.create_task(
            harness.run(
                Agent(name="producer", model=model, tools=[_tool(harness, invoke)]),
                "assignment",
            )
        )
        await asyncio.wait_for(asyncio.gather(good_ready.wait(), slow_ready.wait()), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.writes == 1
        assert (
            harness.last_settlement.succeeded
            and harness.last_settlement.reason == "cancelled"
        )
        assert harness.results["slow"].status == "cancelled"
        assert harness.results["good"].consumed

    asyncio.run(scenario())


def test_late_tool_remains_visible_and_can_be_explicitly_settled(tmp_path):
    async def scenario():
        workspace, store, _ = _workspace(tmp_path)
        harness = SDKContextHarness(workspace, settlement_timeout=1.0)
        good_ready, slow_ready, release, returned = (asyncio.Event() for _ in range(4))

        async def invoke(context, arguments):
            if json.loads(arguments)["label"] == "good":
                good_ready.set()
                return _payload("good")
            slow_ready.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            returned.set()
            return _payload("late")

        model = ScriptedModel(
            [
                [
                    function_call("web_search", {"label": "good"}, call_id="good"),
                    function_call("web_search", {"label": "slow"}, call_id="slow"),
                ]
            ]
        )
        task = asyncio.create_task(
            harness.run(
                Agent(name="producer", model=model, tools=[_tool(harness, invoke)]),
                "assignment",
            )
        )
        await asyncio.wait_for(asyncio.gather(good_ready.wait(), slow_ready.wait()), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.writes == 1
        assert harness.last_settlement.in_flight_call_ids == ("slow",)
        assert harness.last_settlement.error_type == "ToolDrainTimeout"
        assert not harness.last_settlement.succeeded
        release.set()
        await asyncio.wait_for(returned.wait(), 1)
        assert (await harness.settle("late_tool_retry")).succeeded
        assert harness.results["slow"].status == "succeeded"
        assert store.writes == 2

    asyncio.run(scenario())


def test_duplicate_processing_is_idempotent_and_native_call_ids_are_run_isolated(
    tmp_path,
):
    async def scenario():
        store = _Store()
        harnesses = []
        for run_id in ("run-a", "run-b"):
            workspace, _, _ = _workspace(tmp_path, store=store, run_id=run_id)
            harness = SDKContextHarness(workspace)
            harnesses.append(harness)
            model = ScriptedModel(
                [
                    [function_call("web_search", {"label": run_id}, call_id="same-id")],
                    [function_call("web_search", {"label": run_id}, call_id="same-id")],
                    [assistant_message("done")],
                ]
            )
            invocations = []

            async def invoke(context, arguments):
                invocations.append(arguments)
                return _payload(run_id)

            await harness.run(
                Agent(name="producer", model=model, tools=[_tool(harness, invoke)]),
                "assignment",
            )
            writes = store.writes
            assert (await harness.settle("repeat")).succeeded
            assert store.writes == writes
            assert len(invocations) == 1
            assert len(harness.results) == 1
            with pytest.raises(ValueError, match="fresh harness"):
                await harness.run(
                    Agent(name="unused", model=ScriptedModel([])), "assignment"
                )
        assert harnesses[0].sdk_run_id != harnesses[1].sdk_run_id
        assert (
            harnesses[0].results["same-id"].graph_call_id
            != harnesses[1].results["same-id"].graph_call_id
        )
        assert all(
            node.run_id == "run-a"
            for node in store.retrieve("brake", run_id="run-a").nodes
        )
        assert all(
            node.run_id == "run-b"
            for node in store.retrieve("brake", run_id="run-b").nodes
        )
        assert store.writes == 2

    asyncio.run(scenario())


class _MCP(MCPServer):
    def __init__(self, harness):
        super().__init__(
            use_structured_content=True,
            custom_data_extractor=harness.mcp_result,
            failure_error_function=harness.tool_error,
        )
        self.connected = False

    @property
    def name(self):
        return "fixture-mcp"

    async def connect(self):
        self.connected = True

    async def cleanup(self):
        self.connected = False

    async def list_tools(self, run_context=None, agent=None):
        return [
            MCPTool(
                name="mcp_search", description="Fixture MCP", inputSchema=SEARCH_SCHEMA
            )
        ]

    async def call_tool(self, tool_name, arguments, meta=None):
        assert self.connected
        label = arguments["label"]
        if label == "exception":
            raise ValueError("fixture MCP transport error")
        if label == "cancelled":
            raise asyncio.CancelledError()
        return CallToolResult(
            content=[TextContent(type="text", text=_payload(label))],
            structuredContent=json.loads(_payload(label)),
            isError=label == "error",
        )

    async def list_prompts(self):
        return ListPromptsResult(prompts=[])

    async def get_prompt(self, name, arguments=None):
        return GetPromptResult(messages=[])


def test_local_mcp_hooks_and_raw_is_error_collection(tmp_path):
    async def scenario():
        workspace, store, graph_model = _workspace(tmp_path)
        harness = SDKContextHarness(workspace, evidence_tools=frozenset({"mcp_search"}))
        server = _MCP(harness)
        model = ScriptedModel(
            [
                [
                    function_call("mcp_search", {"label": label}, call_id=label)
                    for label in ("good", "error", "exception", "cancelled")
                ],
                [assistant_message("done")],
            ]
        )
        await server.connect()
        try:
            await harness.run(
                Agent(name="producer", model=model, mcp_servers=[server]), "assignment"
            )
        finally:
            await server.cleanup()
        assert not server.connected
        assert harness.results["good"].status == "succeeded"
        assert harness.results["error"].status == "failed"
        assert harness.results["exception"].status == "failed"
        assert harness.results["cancelled"].status == "cancelled"
        assert harness.results["cancelled"].error_type == "MCPToolCancellationError"
        assert len(graph_model.extractions[0]) == 1
        assert (
            len([node for node in store.nodes.values() if node.node_type == "Source"])
            == 1
        )
        assert all(
            ("tool_start", label) in harness.events
            and ("tool_end", label) in harness.events
            for label in ("good", "error", "exception", "cancelled")
        )

    asyncio.run(scenario())


def test_hosted_tool_items_do_not_fire_local_hooks_or_produce_evidence(tmp_path):
    async def scenario():
        workspace, store, graph_model = _workspace(tmp_path)
        harness = SDKContextHarness(
            workspace, evidence_tools=frozenset({"file_search"})
        )
        hosted = ResponseFileSearchToolCall(
            id="hosted-1",
            type="file_search_call",
            queries=["brake"],
            status="completed",
        )
        model = ScriptedModel([[hosted, assistant_message("hosted response")]])
        result = await harness.run(
            Agent(
                name="producer",
                model=model,
                tools=[FileSearchTool(vector_store_ids=["vs-fixture"])],
            ),
            "assignment",
        )
        assert result.final_output == "hosted response"
        assert not harness.results and not store.nodes and not graph_model.extractions
        assert not any(
            event[0] in {"tool_start", "tool_end"} for event in harness.events
        )

    asyncio.run(scenario())


def test_scope_and_conflicting_native_call_identity_are_rejected(tmp_path):
    async def scenario():
        workspace, _, _ = _workspace(tmp_path)
        harness = SDKContextHarness(workspace)
        context = ToolContext(
            context=harness,
            tool_name="web_search",
            tool_call_id="id",
            tool_arguments='{"label":"a"}',
        )
        harness._record(context, "succeeded", _payload("a"))
        harness._record(context, "succeeded", _payload("a"))
        with pytest.raises(ValueError, match="Conflicting duplicate"):
            harness._record(context, "succeeded", _payload("changed"))
        changed = ToolContext(
            context=harness,
            tool_name="web_search",
            tool_call_id="id",
            tool_arguments='{"label":"b"}',
        )
        with pytest.raises(ValueError, match="different invocation"):
            harness._start(changed)
        context.context = object()
        with pytest.raises(ValueError, match="does not match"):
            harness._start(context)
        workspace.scope.task_id = "changed-task"
        with pytest.raises(ValueError, match="does not match"):
            harness._check(harness)

    asyncio.run(scenario())


def test_cancellation_during_ingestion_retries_remaining_evidence_at_exit(tmp_path):
    async def scenario():
        entered = asyncio.Event()

        class InterruptibleProducer(ResearchGraphProducerStrategy):
            attempts = 0

            async def after_tool_batch(self, workspace, messages, batch):
                self.attempts += 1
                if self.attempts == 1:
                    entered.set()
                    await asyncio.Event().wait()
                return await super().after_tool_batch(workspace, messages, batch)

        strategy = InterruptibleProducer()
        workspace, store, _ = _workspace(tmp_path, strategy=strategy)
        harness = SDKContextHarness(workspace)
        model = ScriptedModel(
            [
                [function_call("web_search", {"label": "a"}, call_id="a")],
                [assistant_message("should not run")],
            ]
        )
        task = asyncio.create_task(
            harness.run(
                Agent(name="producer", model=model, tools=[_tool(harness)]),
                "assignment",
            )
        )
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert strategy.attempts == 2
        assert len(model.calls) == 1
        assert store.writes == 1
        assert harness.last_settlement.succeeded
        assert harness.results["a"].consumed

    asyncio.run(scenario())


def test_repeated_cancellation_does_not_interrupt_exit_commit(tmp_path):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowProducer(ResearchGraphProducerStrategy):
            async def after_tool_batch(self, workspace, messages, batch):
                entered.set()
                await release.wait()
                return await super().after_tool_batch(workspace, messages, batch)

        workspace, store, _ = _workspace(tmp_path, strategy=SlowProducer())
        harness = SDKContextHarness(workspace)
        model = ScriptedModel(
            [[function_call("web_search", {"label": "a"}, call_id="a")]]
        )
        task = asyncio.create_task(
            harness.run(
                Agent(
                    name="producer",
                    model=model,
                    tools=[_tool(harness)],
                    tool_use_behavior="stop_on_first_tool",
                ),
                "assignment",
            )
        )
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert store.writes == 1
        assert (
            harness.last_settlement.succeeded
            and harness.last_settlement.reason == "cancelled"
        )

    asyncio.run(scenario())


def test_partial_graph_write_failure_never_compacts_uncommitted_batch(tmp_path):
    class PartialStore(_Store):
        def write_delta(self, delta):
            if self.writes == 1:
                self.writes += 1
                raise OSError("second extraction batch failed")
            return super().write_delta(delta)

    async def scenario():
        workspace, store, _ = _workspace(tmp_path, store=PartialStore())
        workspace.configurable.research_graph_extraction_batch_tokens = 1
        harness = SDKContextHarness(workspace)

        def second(call):
            assert store.writes == 2
            assert not workspace.receipts
            assert _outputs(call) == {"a": _payload("a"), "b": _payload("b")}
            assert (
                not harness.results["a"].consumed and not harness.results["b"].consumed
            )
            return [assistant_message("done")]

        model = ScriptedModel(
            [
                [
                    function_call("web_search", {"label": label}, call_id=label)
                    for label in ("a", "b")
                ],
                ModelStep.respond(second),
            ]
        )
        await harness.run(
            Agent(name="producer", model=model, tools=[_tool(harness)]), "assignment"
        )
        assert store.writes == 3
        assert (
            len([node for node in store.nodes.values() if node.node_type == "Source"])
            == 2
        )
        assert harness.last_settlement.succeeded

    asyncio.run(scenario())


def test_recent_raw_steps_are_preserved_then_compacted_after_a_control_turn(tmp_path):
    async def scenario():
        workspace, store, _ = _workspace(tmp_path, recent_raw_steps=1)
        harness = SDKContextHarness(workspace)

        def second(call):
            assert store.writes == 1
            assert _outputs(call)["evidence"] == _payload("evidence")
            return [
                function_call(
                    "think_tool", {"label": "reflection"}, call_id="reflection"
                )
            ]

        def third(call):
            assert _outputs(call)["evidence"].startswith("[Research result compacted.")
            assert _outputs(call)["reflection"] == _payload("reflection")
            assert store.writes == 1
            return [assistant_message("done")]

        model = ScriptedModel(
            [
                [
                    function_call(
                        "web_search", {"label": "evidence"}, call_id="evidence"
                    )
                ],
                ModelStep.respond(second),
                ModelStep.respond(third),
            ]
        )
        await harness.run(
            Agent(
                name="producer",
                model=model,
                tools=[_tool(harness), _tool(harness, name="think_tool")],
            ),
            "assignment",
        )

    asyncio.run(scenario())


def test_sdk_invocations_and_tasks_within_the_same_research_run_are_isolated(tmp_path):
    async def scenario():
        store = _Store()
        records = []
        for task_id in ("first-task", "second-task"):
            workspace, _, _ = _workspace(tmp_path, store=store, task_id=task_id)
            harness = SDKContextHarness(workspace)
            model = ScriptedModel(
                [
                    [
                        function_call(
                            "web_search", {"label": task_id}, call_id="same-id"
                        )
                    ],
                    [assistant_message("done")],
                ]
            )
            await harness.run(
                Agent(name="producer", model=model, tools=[_tool(harness)]),
                "assignment",
            )
            records.append(harness.results["same-id"])
        assert records[0].graph_call_id != records[1].graph_call_id
        assert (
            records[0].task_id == "first-task" and records[1].task_id == "second-task"
        )
        sources = [node for node in store.nodes.values() if node.node_type == "Source"]
        assert {source.task_id for source in sources} == {"first-task", "second-task"}

    asyncio.run(scenario())


def test_model_exception_preserves_original_error_and_settles_run(tmp_path):
    async def scenario():
        workspace, store, _ = _workspace(tmp_path)
        harness = SDKContextHarness(workspace)
        error = RuntimeError("fixture model failure")
        model = ScriptedModel(
            [
                [function_call("web_search", {"label": "a"}, call_id="a")],
                error,
            ]
        )
        with pytest.raises(RuntimeError) as failure:
            await harness.run(
                Agent(name="producer", model=model, tools=[_tool(harness)]),
                "assignment",
            )
        assert failure.value is error
        assert store.writes == 1
        assert (
            harness.last_settlement.succeeded
            and harness.last_settlement.reason == "exception"
        )
        assert not any(event[0] == "agent_end" for event in harness.events)

    asyncio.run(scenario())


def test_cancelled_graph_settlement_reports_pending_data_without_hanging(tmp_path):
    class CancelledProducer(ResearchGraphProducerStrategy):
        async def after_tool_batch(self, workspace, messages, batch):
            raise asyncio.CancelledError()

    async def scenario():
        workspace, store, _ = _workspace(tmp_path, strategy=CancelledProducer())
        harness = SDKContextHarness(workspace)
        model = ScriptedModel(
            [[function_call("web_search", {"label": "a"}, call_id="a")]]
        )
        agent = Agent(
            name="producer",
            model=model,
            tools=[_tool(harness)],
            tool_use_behavior="stop_on_first_tool",
        )
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(harness.run(agent, "assignment"), 2)
        assert harness.last_settlement.pending_call_ids == ("a",)
        assert harness.last_settlement.error_type == "CancelledError"
        assert not harness.last_settlement.succeeded and not store.nodes
        assert harness.results["a"].observation == _payload("a")

    asyncio.run(scenario())


@pytest.mark.parametrize("separate_runs", [False, True])
def test_duplicate_url_compaction_never_publishes_unpersisted_source_ids(
    tmp_path, separate_runs
):
    async def scenario():
        store = _Store()
        batches = [("a",), ("b",)] if separate_runs else [("a", "b")]
        for labels in batches:
            workspace, _, _ = _workspace(tmp_path, store=store)
            harness = SDKContextHarness(workspace)

            async def duplicate(context, arguments):
                return _payload("same-url")

            def inspect(call):
                assert all(
                    output.startswith("[Research result compacted.")
                    for output in _outputs(call).values()
                )
                for output in _outputs(call).values():
                    if output.startswith("[Research result compacted."):
                        for source_line in output.splitlines():
                            if source_line.startswith(
                                ("Sources:", "Duplicate sources skipped:")
                            ):
                                for source_id in source_line.split(": ", 1)[1].split(
                                    ", "
                                ):
                                    assert (workspace.run_id, source_id) in store.nodes
                    else:
                        assert output == _payload("same-url")
                return [assistant_message("done")]

            model = ScriptedModel(
                [
                    [
                        function_call("web_search", {"label": label}, call_id=label)
                        for label in labels
                    ],
                    ModelStep.respond(inspect),
                ]
            )
            await harness.run(
                Agent(name="producer", model=model, tools=[_tool(harness, duplicate)]),
                "assignment",
            )
            assert not any(
                event[0] == "compaction_deferred" for event in harness.events
            )
        assert store.writes == 1

    asyncio.run(scenario())


def test_context_update_failure_is_not_settled_by_url_dedup_alone(tmp_path):
    class FailedContextModel(_GraphModel):
        def with_structured_output(self, schema, include_raw=False):
            if schema is WorkingContextDelta:
                raise ValueError("fixture structured context update failed")
            return super().with_structured_output(schema, include_raw=include_raw)

    async def scenario():
        workspace, store, _ = _workspace(tmp_path)
        graph_model = FailedContextModel()
        workspace.model_factory = lambda _name, _tokens: graph_model
        harness = SDKContextHarness(workspace)

        def second(call):
            assert store.writes == 1
            assert _outputs(call)["a"] == _payload("a")
            return [assistant_message("done")]

        model = ScriptedModel(
            [
                [function_call("web_search", {"label": "a"}, call_id="a")],
                ModelStep.respond(second),
            ]
        )
        with pytest.raises(ResearchGraphSettlementError):
            await harness.run(
                Agent(name="producer", model=model, tools=[_tool(harness)]),
                "assignment",
            )
        assert store.writes == 1
        assert len(graph_model.extractions) == 1
        assert harness.last_settlement.pending_call_ids == ("a",)
        assert not harness.results["a"].consumed
        assert not workspace.receipts

    asyncio.run(scenario())
