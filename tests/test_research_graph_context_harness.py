"""Focused tests for the Research Graph Context Harness refactor."""

import json
import re
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from open_deep_research.configuration import Configuration
from open_deep_research.public_opinion_agents import PUBLIC_OPINION_AGENT_SPECS
from open_deep_research.research_graph import (
    ContextConflict,
    GraphExtractionOutput,
    InMemoryResearchGraphStore,
    ResearchContextHarness,
    ResearchGraphProducerStrategy,
    ResearchGraphScope,
    ToolBatchItem,
    WorkingContext,
    WorkingContextDelta,
    apply_working_context_delta,
    batch_documents_by_tokens,
    micro_compact_messages,
    normalize_extraction_output,
    should_rolling_compact,
)
from open_deep_research.research_graph.models import RawResearchDocument
from open_deep_research.research_graph.schema import content_hash
from open_deep_research.state import role_reports_reducer


def _scope(run_id: str = "run-a") -> ResearchGraphScope:
    return ResearchGraphScope(
        run_id=run_id,
        role="public_signal",
        research_round=1,
        task_id="task-a",
    )


def _document(run_id: str = "run-a", source_id: str = "SRC1") -> RawResearchDocument:
    content = "Official notice confirms a brake investigation."
    return RawResearchDocument(
        source_id=source_id,
        url=f"https://example.test/{source_id}",
        title="Official notice",
        content=content,
        source_type="web_search",
        tool_name="web_search",
        content_hash=content_hash(content),
    )


def test_agent_specs_route_to_the_two_context_strategies() -> None:
    assert PUBLIC_OPINION_AGENT_SPECS["public_signal"].context_strategy == "research_graph_producer"
    assert PUBLIC_OPINION_AGENT_SPECS["internal_knowledge"].context_strategy == "research_graph_producer"
    assert PUBLIC_OPINION_AGENT_SPECS["risk_assessment"].context_strategy == "research_graph_consumer"
    assert PUBLIC_OPINION_AGENT_SPECS["response_strategy"].context_strategy == "research_graph_consumer"


def test_provenance_materializes_finding_claim_evidence_source_edges() -> None:
    scope = _scope()
    delta = normalize_extraction_output(
        GraphExtractionOutput(
            claims=[{"local_id": "claim-1", "statement": "Brake issue is reported."}],
            evidences=[
                {
                    "local_id": "evidence-1",
                    "source_id": "SRC1",
                    "statement": "The notice describes a brake investigation.",
                    "supports_claim_ids": ["claim-1"],
                }
            ],
            findings=[
                {
                    "local_id": "finding-1",
                    "summary": "The issue requires verification.",
                    "claim_ids": ["claim-1"],
                    "evidence_ids": ["evidence-1"],
                }
            ],
        ),
        [_document()],
        scope=scope,
    )
    store = InMemoryResearchGraphStore()
    receipt = store.write_delta(delta)
    subgraph = store.retrieve("brake investigation", run_id="run-a")
    edges = {(edge.relation_type, edge.source_id, edge.target_id) for edge in subgraph.edges}
    assert receipt.source_ids == ["SRC1"]
    assert receipt.evidence_ids and receipt.claim_ids and receipt.finding_ids
    assert any(kind == "EXTRACTED_FROM" for kind, _, _ in edges)
    assert any(kind == "SUPPORTS" for kind, _, _ in edges)
    assert any(kind == "DERIVED_FROM" for kind, _, _ in edges)
    assert any(kind == "SUPPORTED_BY" for kind, _, _ in edges)


def test_graph_retrieval_isolation_by_run_id() -> None:
    store = InMemoryResearchGraphStore()
    for run_id, source_id in (("run-a", "SRC-A"), ("run-b", "SRC-B")):
        store.write_delta(
            normalize_extraction_output(
                GraphExtractionOutput(
                    evidences=[
                        {
                            "source_id": source_id,
                            "statement": f"Evidence for {run_id}.",
                        }
                    ]
                ),
                [_document(run_id, source_id)],
                scope=_scope(run_id),
            )
        )
    assert all("SRC-B" not in node.node_id for node in store.retrieve("Evidence", run_id="run-a").nodes)
    assert any(node.node_id == "SRC-B" for node in store.retrieve("Evidence", run_id="run-b").nodes)


def test_url_dedup_skips_exact_source_version() -> None:
    store = InMemoryResearchGraphStore()
    scope = _scope()
    document = _document()
    delta = normalize_extraction_output(GraphExtractionOutput(), [document], scope=scope)
    store.write_delta(delta)
    assert store.source_is_persisted(scope, document)
    changed = document.model_copy(update={"content": "A changed notice.", "content_hash": content_hash("A changed notice.")})
    assert not store.source_is_persisted(scope, changed)


def test_context_manager_delta_keeps_conflict_provenance() -> None:
    current = WorkingContext(current_objective="assess safety")
    delta = WorkingContextDelta(
        conflicts_add=[
            ContextConflict(
                summary="Official and owner accounts differ.",
                claim_ids=["CLM1"],
                evidence_ids=["EVD1", "EVD2"],
                source_ids=["SRC1", "SRC2"],
            )
        ],
    )
    updated = apply_working_context_delta(
        current,
        delta,
        available_graph_ids={"CLM1", "EVD1", "EVD2", "SRC1", "SRC2"},
    )
    assert isinstance(updated.conflicts[0], ContextConflict)
    assert updated.conflicts[0].evidence_ids == ["EVD1", "EVD2"]


def test_micro_compact_replaces_old_tool_body_and_keeps_recent_step() -> None:
    first_call = {"name": "web_search", "args": {"queries": ["old"]}, "id": "call-old"}
    second_call = {"name": "web_search", "args": {"queries": ["new"]}, "id": "call-new"}
    messages = [
        HumanMessage(content="assignment"),
        AIMessage(content="old reasoning", tool_calls=[first_call]),
        ToolMessage(content="very large raw old result", name="web_search", tool_call_id="call-old"),
        AIMessage(content="new reasoning", tool_calls=[second_call]),
        ToolMessage(content="recent raw result", name="web_search", tool_call_id="call-new"),
    ]
    result = micro_compact_messages(
        messages,
        {"call-old": "[receipt]"},
        recent_raw_steps=1,
    )
    assert result.compacted_tool_call_ids == ["call-old"]
    assert result.messages[2].tool_call_id == "call-old"
    assert result.messages[2].content == "[receipt]"
    assert result.messages[4].content == "recent raw result"


def test_rolling_compact_threshold_is_context_only() -> None:
    messages = [HumanMessage(content="x" * 400)]
    assert not should_rolling_compact(
        messages,
        model_context_capacity=10_000,
        threshold_ratio=0.75,
    )
    assert should_rolling_compact(
        messages,
        model_context_capacity=10,
        threshold_ratio=0.75,
    )


def test_extraction_batches_by_tokens_instead_of_fixed_document_count() -> None:
    documents = [
        RawResearchDocument(source_id=f"S{i}", content="x" * 80)
        for i in range(3)
    ]
    batches = batch_documents_by_tokens(documents, max_tokens=25)
    assert len(batches) == 3
    assert all(len(batch) == 1 for batch in batches)


def test_tavily_raw_tool_keeps_source_boundaries_without_page_summarization(monkeypatch) -> None:
    import asyncio

    import open_deep_research.utils as utils

    async def fake_search(*_args, **_kwargs):
        return [
            {
                "query": "brake",
                "results": [
                    {
                        "url": "https://example.test/a",
                        "title": "A",
                        "raw_content": "raw A",
                    },
                    {
                        "url": "https://example.test/a",
                        "title": "A duplicate",
                        "raw_content": "raw duplicate",
                    },
                ],
            }
        ]

    monkeypatch.setattr(utils, "tavily_search_async", fake_search)
    payload = asyncio.run(
        utils.tavily_search_raw.ainvoke({"queries": ["brake"]}, {})
    )
    decoded = json.loads(payload)
    assert decoded["type"] == "research_raw_search"
    assert len(decoded["results"]) == 1
    assert decoded["results"][0]["content"] == "raw A"


class _GraphFixtureModel:
    def __init__(self) -> None:
        self.schema = None
        self.extraction_calls = 0

    def with_structured_output(self, schema):
        self.schema = schema
        return self

    def with_retry(self, **_kwargs):
        return self

    def with_config(self, _config):
        return self

    async def ainvoke(self, messages):
        prompt = str(messages[0].content)
        if self.schema is GraphExtractionOutput:
            self.extraction_calls += 1
            source_id = re.search(r'"source_id":\s*"([^"]+)"', prompt).group(1)
            return GraphExtractionOutput(
                claims=[{"local_id": "c1", "statement": "A brake issue is reported."}],
                evidences=[
                    {
                        "local_id": "e1",
                        "source_id": source_id,
                        "statement": "The source reports a brake issue.",
                        "supports_claim_ids": ["c1"],
                    }
                ],
            )
        if self.schema is WorkingContextDelta:
            return WorkingContextDelta(recent_progress="graph updated")
        return SimpleNamespace(content="role report")


def test_producer_hook_persists_then_compacts_raw_tool_result(tmp_path) -> None:
    model = _GraphFixtureModel()
    config = Configuration(
        research_graph_enabled=True,
        research_graph_backend="memory",
        research_graph_transcript_dir=str(tmp_path),
        recent_raw_steps=0,
    )
    store = InMemoryResearchGraphStore()
    harness = ResearchContextHarness(
        strategy=ResearchGraphProducerStrategy(),
        state={"research_round": 1, "working_contexts": {}, "rolling_summaries": {}},
        role="public_signal",
        assignment="Investigate brake safety reports.",
        agent_prompt="agent",
        configurable=config,
        runtime_config={},
        model_factory=lambda _name, _max_tokens: model,
        store=store,
        run_id="run-fixture",
    )
    tool_call = {"name": "web_search", "args": {"queries": ["brake"]}, "id": "call-1"}
    observation = json.dumps(
        {
            "type": "research_raw_search",
            "results": [
                {
                    "url": "https://example.test/brake",
                    "title": "Brake notice",
                    "content": "Official notice confirms a brake investigation.",
                }
            ],
        }
    )
    messages = [
        HumanMessage(content="assignment"),
        AIMessage(content="search", tool_calls=[tool_call]),
        ToolMessage(content=observation, name="web_search", tool_call_id="call-1"),
    ]
    result = __import__("asyncio").run(
        harness.after_tool_batch(
            messages,
            [ToolBatchItem("web_search", "call-1", tool_call["args"], observation, True)],
        )
    )
    assert result.succeeded
    assert model.extraction_calls == 1
    assert any(node.node_type == "Evidence" for node in store.nodes.values())
    assert result.messages[2].content.startswith("[Research result compacted.")
    assert (tmp_path / "run-fixture.jsonl").exists()


def test_graph_report_updates_replace_same_role_but_legacy_mapping_still_appends() -> None:
    updated = role_reports_reducer(
        {"public_signal": "old"},
        {"type": "role_report_update", "role": "public_signal", "value": "new"},
    )
    assert updated == {"public_signal": "new"}
    legacy = role_reports_reducer({"public_signal": "old"}, {"public_signal": "new"})
    assert "old" in legacy["public_signal"] and "new" in legacy["public_signal"]


def test_graph_agent_path_avoids_legacy_full_history_compression(monkeypatch, tmp_path) -> None:
    import asyncio

    import open_deep_research.deep_researcher as deep_researcher_module

    class FixtureTool:
        name = "web_search"
        metadata = {"type": "search", "name": "web_search"}

        async def ainvoke(self, _args, _config=None):
            return json.dumps(
                {
                    "type": "research_raw_search",
                    "results": [
                        {
                            "url": "https://example.test/graph-agent",
                            "title": "Graph agent source",
                            "content": "The source reports a brake issue.",
                        }
                    ],
                }
            )

    class AgentFixtureModel:
        def __init__(self, model_name="fixture", schema=None):
            self.model_name = model_name
            self.schema = schema
            self.agent_calls = 0

        def bind_tools(self, _tools):
            return self

        def with_retry(self, **_kwargs):
            return self

        def with_structured_output(self, schema):
            return AgentFixtureModel(self.model_name, schema=schema)

        def with_config(self, config):
            return AgentFixtureModel(config.get("model", self.model_name))

        async def ainvoke(self, messages):
            if self.schema is GraphExtractionOutput:
                source_id = re.search(r'"source_id":\s*"([^"]+)"', str(messages[0].content)).group(1)
                return GraphExtractionOutput(
                    claims=[{"local_id": "c1", "statement": "A brake issue is reported."}],
                    evidences=[
                        {
                            "local_id": "e1",
                            "source_id": source_id,
                            "statement": "The source reports a brake issue.",
                            "supports_claim_ids": ["c1"],
                        }
                    ],
                )
            if self.schema is WorkingContextDelta:
                return WorkingContextDelta(recent_progress="updated")
            if "Generate a bounded public-opinion role report" in str(messages[0].content):
                return SimpleNamespace(content="bounded graph report")
            self.agent_calls += 1
            if self.agent_calls == 1:
                return AIMessage(
                    content="search",
                    tool_calls=[
                        {
                            "name": "web_search",
                            "args": {"queries": ["brake"]},
                            "id": "call-graph-agent",
                        }
                    ],
                )
            return AIMessage(content="done")

    fixture_model = AgentFixtureModel()
    monkeypatch.setattr(deep_researcher_module, "configurable_model", fixture_model)
    monkeypatch.setattr(
        deep_researcher_module,
        "_business_agent_tools",
        lambda _config, _role: asyncio.sleep(0, result=[FixtureTool()]),
    )

    async def fail_legacy_compression(_state, _config):
        raise AssertionError("Graph Producer must not invoke compress_research")

    monkeypatch.setattr(deep_researcher_module, "compress_research", fail_legacy_compression)
    result = asyncio.run(
        deep_researcher_module._run_public_opinion_agent(
            {
                "research_brief": "Investigate brake safety.",
                "research_run_id": "run-graph-agent",
                "role_reports": {},
                "agent_memories": {},
                "working_contexts": {},
                "rolling_summaries": {},
                "budget_usage": {},
                "research_round": 1,
                "research_mode": "initial",
                "current_research_tasks": [],
            },
            {
                "configurable": {
                    "research_graph_enabled": True,
                    "research_graph_backend": "memory",
                    "enabled_business_agents": ["public_signal"],
                    "research_graph_transcript_dir": str(tmp_path),
                    "recent_raw_steps": 0,
                    "thread_id": "run-graph-agent",
                }
            },
            "public_signal",
        )
    )
    assert result["role_reports"]["type"] == "role_report_update"
    assert "bounded graph report" in result["role_reports"]["value"]
    assert result["working_contexts"]["public_signal"]["recent_progress"] == "updated"
