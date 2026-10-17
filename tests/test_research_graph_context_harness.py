# 通过确定性替身验证来源链、图范围隔离及生产角色的压缩生命周期。
"""Focused tests for ResearchWorkspace graph/context behavior."""

import json
import re
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

import open_deep_research.runtime.business_agent as business_agent_module
from open_deep_research.budget import context_pressure_ratio, estimate_context_tokens
from open_deep_research.configuration import Configuration
from open_deep_research.public_opinion_agents import PUBLIC_OPINION_AGENT_SPECS
from open_deep_research.research_graph import (
    ContextConflict,
    GraphExtractionOutput,
    InMemoryResearchGraphStore,
    Neo4jResearchGraphStore,
    ResearchGraphProducerStrategy,
    ResearchGraphScope,
    ResearchWorkspace,
    ToolBatchItem,
    WorkingContext,
    WorkingContextDelta,
    apply_working_context_delta,
    batch_documents_by_tokens,
    micro_compact_messages,
    normalize_extraction_output,
)
from open_deep_research.research_graph.extractor import _build_extraction_prompt
from open_deep_research.research_graph.models import (
    GraphEdge,
    RawResearchDocument,
    WriteReceipt,
)
from open_deep_research.research_graph.schema import content_hash
from open_deep_research.state import agents_reducer


# 创建固定运行范围的研究图测试对象。
def _scope(run_id: str = "run-a") -> ResearchGraphScope:
    return ResearchGraphScope(
        run_id=run_id,
        role="public_signal",
        research_round=1,
        task_id="task-a",
    )


# 创建带代码侧来源身份的原始研究材料。
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


# 验证业务角色分配到图生产和图消费策略。
def test_agent_specs_route_to_the_two_context_strategies() -> None:
    assert (
        PUBLIC_OPINION_AGENT_SPECS["public_signal"].context_strategy
        == "research_graph_producer"
    )
    assert (
        PUBLIC_OPINION_AGENT_SPECS["internal_knowledge"].context_strategy
        == "research_graph_producer"
    )
    assert (
        PUBLIC_OPINION_AGENT_SPECS["risk_assessment"].context_strategy
        == "research_graph_consumer"
    )
    assert (
        PUBLIC_OPINION_AGENT_SPECS["response_strategy"].context_strategy
        == "research_graph_consumer"
    )


# 验证发现、主张、证据与来源关系完整物化。
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
    edges = {
        (edge.relation_type, edge.source_id, edge.target_id) for edge in subgraph.edges
    }
    assert receipt.source_ids == ["SRC1"]
    assert receipt.evidence_ids and receipt.claim_ids and receipt.finding_ids
    assert any(kind == "EXTRACTED_FROM" for kind, _, _ in edges)
    assert any(kind == "SUPPORTS" for kind, _, _ in edges)
    assert any(kind == "DERIVED_FROM" for kind, _, _ in edges)
    assert any(kind == "SUPPORTED_BY" for kind, _, _ in edges)


def test_extraction_prompt_record_contract_materializes_full_provenance() -> None:
    document = _document()
    prompt = _build_extraction_prompt([document], _scope())
    record_text = prompt.split("Record field contract:\n", 1)[1]
    records, _ = json.JSONDecoder().raw_decode(record_text)
    delta = normalize_extraction_output(
        GraphExtractionOutput.model_validate(records), [document], scope=_scope()
    )
    _, edges = delta.materialize()
    relations = {edge.relation_type for edge in edges}
    assert {"EXTRACTED_FROM", "SUPPORTS", "DERIVED_FROM", "SUPPORTED_BY"}.issubset(
        relations
    )
    assert delta.evidences[0].source_id == document.source_id


# 验证不同 run_id 的图检索相互隔离。
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
    assert all(
        "SRC-B" not in node.node_id
        for node in store.retrieve("Evidence", run_id="run-a").nodes
    )
    assert any(
        node.node_id == "SRC-B"
        for node in store.retrieve("Evidence", run_id="run-b").nodes
    )


# 验证已保存的相同 URL 内容版本不重复写入。
def test_url_dedup_skips_exact_source_version() -> None:
    store = InMemoryResearchGraphStore()
    scope = _scope()
    document = _document()
    delta = normalize_extraction_output(
        GraphExtractionOutput(), [document], scope=scope
    )
    store.write_delta(delta)
    assert store.source_is_persisted(scope, document)
    changed = document.model_copy(
        update={
            "content": "A changed notice.",
            "content_hash": content_hash("A changed notice."),
        }
    )
    assert not store.source_is_persisted(scope, changed)


# 验证上下文更新保留冲突的来源引用。
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


# 验证微压缩替换旧正文并保留近期完整工具步骤。
def test_micro_compact_replaces_old_tool_body_and_keeps_recent_step() -> None:
    first_call = {"name": "web_search", "args": {"queries": ["old"]}, "id": "call-old"}
    second_call = {"name": "web_search", "args": {"queries": ["new"]}, "id": "call-new"}
    messages = [
        HumanMessage(content="assignment"),
        AIMessage(content="old reasoning", tool_calls=[first_call]),
        ToolMessage(
            content="very large raw old result",
            name="web_search",
            tool_call_id="call-old",
        ),
        AIMessage(content="new reasoning", tool_calls=[second_call]),
        ToolMessage(
            content="recent raw result", name="web_search", tool_call_id="call-new"
        ),
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


# 验证滚动压缩触发依据上下文压力而非无关预算。
def test_rolling_compact_threshold_is_context_only() -> None:
    messages = [HumanMessage(content="x" * 400)]
    estimated = estimate_context_tokens(messages)
    assert context_pressure_ratio(estimated, 10_000) < 0.75
    assert context_pressure_ratio(estimated, 10) >= 0.75


# 验证抽取按令牌量分批，避免固定文档数造成超限。
def test_extraction_batches_by_tokens_instead_of_fixed_document_count() -> None:
    documents = [
        RawResearchDocument(source_id=f"S{i}", content="x" * 80) for i in range(3)
    ]
    batches = batch_documents_by_tokens(documents, max_tokens=25)
    assert len(batches) == 3
    assert all(len(batch) == 1 for batch in batches)


# 验证原始搜索保留来源边界且不调用逐页摘要。
def test_tavily_raw_tool_keeps_source_boundaries_without_page_summarization(
    monkeypatch,
) -> None:
    import asyncio

    import open_deep_research.search.tavily as utils

    # 返回固定网页来源与正文，隔离实际 Tavily 请求。
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
    payload = asyncio.run(utils.tavily_search_raw.ainvoke({"queries": ["brake"]}, {}))
    decoded = json.loads(payload)
    assert decoded["type"] == "research_raw_search"
    assert len(decoded["results"]) == 1
    assert decoded["results"][0]["content"] == "raw A"


# 提供图抽取、上下文更新和报告生成的预设模型响应。
class _GraphFixtureModel:
    """Fixture structured model emitting real ``include_raw=True`` envelopes."""

    # 初始化预设响应及调用记录。
    def __init__(self) -> None:
        self.schema = None
        self.extraction_calls = 0

    # 保存结构化输出模式并提供测试调用链。
    def with_structured_output(self, schema, include_raw=False):
        self.schema = schema
        return RunnableLambda(self._structured_call)

    # 保留测试模型的链式重试接口。
    def with_retry(self, **_kwargs):
        return self

    # 接收运行配置并返回可执行模型替身。
    def with_config(self, _config):
        return self

    # 按请求结构返回预设图或工作上下文响应。
    async def _structured_call(self, messages, config=None):
        parsed = await self.ainvoke(messages)
        return {
            "raw": AIMessage(
                content="",
                response_metadata={"model_name": "fixture"},
                usage_metadata={
                    "input_tokens": 7,
                    "output_tokens": 3,
                    "total_tokens": 10,
                },
            ),
            "parsed": parsed,
            "parsing_error": None,
        }

    # 异步返回脚本指定的模型消息。
    async def ainvoke(self, messages, config=None):
        prompt = str(messages[0].content)
        if self.schema is GraphExtractionOutput:
            self.extraction_calls += 1
            source_ids = list(
                dict.fromkeys(re.findall(r'"source_id":\s*"([^"]+)"', prompt))
            )
            return GraphExtractionOutput(
                claims=[{"local_id": "c1", "statement": "A brake issue is reported."}],
                evidences=[
                    {
                        "local_id": f"e{index}",
                        "source_id": source_id,
                        "statement": "The source reports a brake issue.",
                        "supports_claim_ids": ["c1"],
                    }
                    for index, source_id in enumerate(source_ids, start=1)
                ],
                findings=[
                    {
                        "local_id": f"f{index}",
                        "summary": "The brake issue requires verification.",
                        "claim_ids": ["c1"],
                        "evidence_ids": [f"e{index}"],
                    }
                    for index in range(1, len(source_ids) + 1)
                ],
            )
        if self.schema is WorkingContextDelta:
            return WorkingContextDelta(recent_progress="graph updated")
        return SimpleNamespace(content="role report")


# 验证生产钩子先持久化证据，再压缩原始工具结果。
def test_producer_hook_persists_then_compacts_raw_tool_result(tmp_path) -> None:
    model = _GraphFixtureModel()
    config = Configuration(
        research_graph_enabled=True,
        research_graph_backend="memory",
        research_graph_transcript_dir=str(tmp_path),
        recent_raw_steps=0,
    )
    store = InMemoryResearchGraphStore()
    workspace = ResearchWorkspace(
        strategy=ResearchGraphProducerStrategy(),
        research_state={"working_contexts": {}},
        role="public_signal",
        configurable=config,
        model_factory=lambda _name, _max_tokens: model,
        store=store,
        run_id="run-fixture",
        research_round=1,
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
        workspace.ingest(
            messages,
            [
                ToolBatchItem(
                    "web_search", "call-1", tool_call["args"], observation, True
                )
            ],
        )
    )
    assert result.succeeded
    assert model.extraction_calls == 1
    assert any(node.node_type == "Evidence" for node in store.nodes.values())
    assert result.messages[2].content.startswith("[Research result compacted.")
    assert (tmp_path / "run-fixture.jsonl").exists()


@pytest.mark.parametrize("same_batch", [False, True])
def test_duplicate_tool_call_receipt_reuses_persisted_source(tmp_path, same_batch):
    import asyncio

    model = _GraphFixtureModel()
    store = InMemoryResearchGraphStore()
    workspace = ResearchWorkspace(
        strategy=ResearchGraphProducerStrategy(),
        research_state={},
        role="public_signal",
        configurable=Configuration(
            research_graph_enabled=True,
            research_graph_backend="memory",
            research_graph_transcript_dir=str(tmp_path),
            recent_raw_steps=0,
        ),
        model_factory=lambda _name, _tokens: model,
        store=store,
        run_id="run-duplicate",
    )
    observation = json.dumps(
        {
            "type": "research_raw_search",
            "results": [
                {
                    "url": "https://example.test/brake",
                    "content": "Official notice confirms a brake investigation.",
                }
            ],
        }
    )
    calls = [
        {"name": "web_search", "args": {"queries": ["brake"]}, "id": call_id}
        for call_id in ("first", "second")
    ]

    async def scenario():
        messages = [HumanMessage(content="Investigate brakes")]
        for index, call in enumerate(calls):
            messages.extend(
                [
                    AIMessage(content="", tool_calls=[call]),
                    ToolMessage(
                        content=observation, name="web_search", tool_call_id=call["id"]
                    ),
                ]
            )
            if not same_batch or index == 1:
                batch_calls = calls if same_batch else [call]
                result = await workspace.ingest(
                    messages,
                    [
                        ToolBatchItem(
                            "web_search", item["id"], item["args"], observation, True
                        )
                        for item in batch_calls
                    ],
                )
                assert result.succeeded
        first, second = workspace.receipts["first"], workspace.receipts["second"]
        assert second.source_ids == first.source_ids
        assert second.duplicate_source_ids == first.source_ids
        assert second.evidence_ids and second.claim_ids and second.finding_ids
        assert set(
            second.source_ids
            + second.evidence_ids
            + second.claim_ids
            + second.finding_ids
        ).issubset(second.node_ids)
        assert all(
            (workspace.run_id, node_id) in store.nodes for node_id in second.node_ids
        )
        assert model.extraction_calls == 1
        assert result.messages[-1].content.startswith("[Research result compacted.")
        assert result.messages[-1].tool_call_id == "second"

    asyncio.run(scenario())


def _duplicate_workspace(tmp_path, *, store=None, run_id="version-run", model=None):
    model = model or _GraphFixtureModel()
    store = store if store is not None else InMemoryResearchGraphStore()
    return ResearchWorkspace(
        strategy=ResearchGraphProducerStrategy(),
        research_state={},
        role="public_signal",
        configurable=Configuration(
            research_graph_enabled=True,
            research_graph_backend="memory",
            research_graph_transcript_dir=str(tmp_path),
            recent_raw_steps=0,
        ),
        model_factory=lambda _name, _tokens: model,
        store=store,
        run_id=run_id,
    ), model


async def _ingest_notice(
    workspace, call_id, *, content="Official brake notice.", messages=None
):
    observation = json.dumps(
        {
            "type": "research_raw_search",
            "results": [{"url": "https://example.test/notice", "content": content}],
        }
    )
    messages = list(messages or [HumanMessage(content="Investigate brakes")])
    call = {"name": "web_search", "id": call_id, "args": {"queries": ["brakes"]}}
    messages.extend(
        [
            AIMessage(content="", tool_calls=[call]),
            ToolMessage(content=observation, name="web_search", tool_call_id=call_id),
        ]
    )
    outcome = await workspace.ingest(
        messages,
        [ToolBatchItem("web_search", call_id, call["args"], observation, True)],
    )
    return outcome, observation


def test_duplicate_receipt_versions_and_run_isolation(tmp_path):
    import asyncio

    async def scenario():
        store = InMemoryResearchGraphStore()
        workspace, model = _duplicate_workspace(tmp_path, store=store)
        first, _ = await _ingest_notice(workspace, "a", content="First brake notice.")
        changed, _ = await _ingest_notice(
            workspace, "b", content="Changed brake notice."
        )
        repeated, _ = await _ingest_notice(
            workspace, "c", content="First brake notice."
        )
        assert first.succeeded and changed.succeeded and repeated.succeeded
        a, b, c = (workspace.receipts[key] for key in ("a", "b", "c"))
        assert a.source_ids != b.source_ids
        assert c.source_ids == a.source_ids and c.duplicate_source_ids == a.source_ids
        assert not b.duplicate_source_ids and model.extraction_calls == 2
        assert (
            len([node for node in store.nodes.values() if node.node_type == "Source"])
            == 2
        )
        assert repeated.messages[-1].content.startswith("[Research result compacted.")
        other, other_model = _duplicate_workspace(
            tmp_path, store=store, run_id="other-run"
        )
        result, _ = await _ingest_notice(other, "a", content="First brake notice.")
        assert result.succeeded and other_model.extraction_calls == 1
        assert other.receipts["a"].source_ids != a.source_ids
        assert all(
            store.nodes[(other.run_id, node_id)].run_id == other.run_id
            for node_id in other.receipts["a"].node_ids
        )

    asyncio.run(scenario())


def test_one_batch_tracks_each_url_content_version_until_persisted(tmp_path):
    import asyncio

    async def scenario():
        workspace, model = _duplicate_workspace(tmp_path)
        messages = [HumanMessage(content="Compare source versions")]
        batch = []
        for call_id, text in (
            ("a", "Initial notice"),
            ("b", "Changed notice"),
            ("c", "Initial notice"),
        ):
            observation = json.dumps(
                {
                    "type": "research_raw_search",
                    "results": [
                        {"url": "https://example.test/notice", "content": text}
                    ],
                }
            )
            call = {"name": "web_search", "id": call_id, "args": {}}
            messages.extend(
                [
                    AIMessage(content="", tool_calls=[call]),
                    ToolMessage(content=observation, tool_call_id=call_id),
                ]
            )
            batch.append(ToolBatchItem("web_search", call_id, {}, observation, True))
        result = await workspace.ingest(messages, batch)
        assert result.succeeded and model.extraction_calls == 1
        assert workspace.receipts["a"].source_ids == workspace.receipts["c"].source_ids
        assert workspace.receipts["a"].source_ids != workspace.receipts["b"].source_ids
        assert (
            len(
                [
                    node
                    for node in workspace.store.nodes.values()
                    if node.node_type == "Source"
                ]
            )
            == 2
        )
        assert all(
            message.content.startswith("[Research result compacted.")
            for message in result.messages
            if isinstance(message, ToolMessage)
        )

    asyncio.run(scenario())


def test_source_must_still_be_confirmed_after_context_update(tmp_path):
    import asyncio

    store = InMemoryResearchGraphStore()

    class DeletingContextModel(_GraphFixtureModel):
        async def _structured_call(self, messages, config=None):
            result = await super()._structured_call(messages, config)
            if self.schema is WorkingContextDelta:
                for key, node in list(store.nodes.items()):
                    if node.node_type == "Source":
                        del store.nodes[key]
            return result

    async def scenario():
        workspace, _ = _duplicate_workspace(
            tmp_path, store=store, model=DeletingContextModel()
        )
        outcome, raw = await _ingest_notice(workspace, "deleted")
        assert not outcome.succeeded and not workspace.receipts
        assert outcome.messages[-1].content == raw
        assert outcome.messages[-1].tool_call_id == "deleted"

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["lookup_error", "unconfirmed", "write_error"])
def test_source_confirmation_failures_keep_raw_tool_results(tmp_path, failure):
    import asyncio

    class FailedStore(InMemoryResearchGraphStore):
        def get_source_receipt(self, scope, document):
            if failure == "lookup_error":
                raise OSError("fixture source query failed")
            if failure == "unconfirmed":
                return None
            return super().get_source_receipt(scope, document)

        def write_delta(self, delta):
            if failure == "write_error":
                raise OSError("fixture persistence failed")
            return super().write_delta(delta)

    async def scenario():
        workspace, _ = _duplicate_workspace(tmp_path, store=FailedStore())
        result, raw = await _ingest_notice(workspace, "failed")
        assert not result.succeeded and not workspace.receipts
        assert result.messages[-1].content == raw
        assert result.messages[-1].tool_call_id == "failed"
        assert workspace.metrics.as_dict().get("micro_compact_count", 0) == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"source_ids": ["SRC1"], "node_ids": []},
        {
            "source_ids": ["SRC1"],
            "duplicate_source_ids": ["ghost"],
            "node_ids": ["SRC1"],
        },
        {"source_ids": ["SRC1"], "evidence_ids": ["ghost"], "node_ids": ["SRC1"]},
        {"source_ids": ["SRC1"], "claim_ids": ["ghost"], "node_ids": ["SRC1"]},
        {"source_ids": ["SRC1"], "finding_ids": ["ghost"], "node_ids": ["SRC1"]},
    ],
)
def test_micro_compaction_rejects_incomplete_graph_receipt(invalid_fields):
    message = ToolMessage(content="original source body", tool_call_id="call")
    receipt = WriteReceipt(run_id="run-a", **invalid_fields)
    result = micro_compact_messages([message], {"call": receipt}, recent_raw_steps=0)
    assert not receipt.has_complete_node_references
    assert result.messages[0].content == message.content
    assert not result.compacted_tool_call_ids


def _persisted_provenance():
    document = _document()
    output = GraphExtractionOutput(
        claims=[{"local_id": "c", "statement": "A brake issue is reported."}],
        evidences=[
            {
                "local_id": "e",
                "source_id": document.source_id,
                "statement": "The notice confirms a brake issue.",
                "supports_claim_ids": ["c"],
            }
        ],
        findings=[
            {
                "local_id": "f",
                "summary": "The brake issue requires verification.",
                "claim_ids": ["c"],
                "evidence_ids": ["e"],
            }
        ],
    )
    delta = normalize_extraction_output(output, [document], scope=_scope())
    store = InMemoryResearchGraphStore()
    store.write_delta(delta)
    return store, document


def test_source_receipt_uses_live_nodes_and_preserves_provenance():
    store, document = _persisted_provenance()
    incoming = document.model_copy(update={"source_id": "NOT_WRITTEN"})
    scope = _scope().model_copy(
        update={
            "role": "internal_knowledge",
            "task_id": "new-task",
            "research_round": 2,
        }
    )
    proof = store.get_source_receipt(scope, incoming)
    assert proof.source_ids == proof.duplicate_source_ids == [document.source_id]
    assert proof.role == scope.role and proof.task_id == scope.task_id
    assert proof.evidence_ids and proof.claim_ids and proof.finding_ids
    assert "NOT_WRITTEN" not in proof.node_ids
    assert all((scope.run_id, node_id) in store.nodes for node_id in proof.node_ids)
    assert all((scope.run_id, edge_id) in store.edges for edge_id in proof.edge_ids)
    assert {
        store.edges[(scope.run_id, edge_id)].relation_type for edge_id in proof.edge_ids
    } >= {"EXTRACTED_FROM", "SUPPORTS", "DERIVED_FROM", "SUPPORTED_BY"}
    original_finding = next(
        node for node in store.nodes.values() if node.node_type == "Finding"
    )
    store.nodes[(scope.run_id, "FOREIGN_FINDING")] = original_finding.model_copy(
        update={"node_id": "FOREIGN_FINDING"}
    )
    store.edges[(scope.run_id, "foreign-edge")] = GraphEdge(
        edge_id="foreign-edge",
        run_id="other-run",
        relation_type="SUPPORTED_BY",
        source_id="FOREIGN_FINDING",
        target_id=proof.evidence_ids[0],
    )
    assert "FOREIGN_FINDING" not in store.get_source_receipt(scope, incoming).node_ids
    assert store.get_source_receipt(_scope("foreign-run"), incoming) is None
    assert (
        store.get_source_receipt(
            scope, incoming.model_copy(update={"content_hash": "other-version"})
        )
        is None
    )
    del store.nodes[(scope.run_id, document.source_id)]
    assert store.get_source_receipt(scope, incoming) is None
    assert not store.source_is_persisted(scope, incoming)


def test_neo4j_source_receipt_queries_and_verifies_scoped_nodes():
    memory, document = _persisted_provenance()
    nodes = [node.model_dump() for node in memory.nodes.values()]
    edges = [edge.model_dump() for edge in memory.edges.values()]
    rows = [{"source_id": document.source_id, "nodes": nodes, "edge_paths": [edges]}]
    calls = []

    class Driver:
        def execute_query(self, query, parameters_, database_):
            calls.append((query, parameters_))
            return rows

    store = Neo4jResearchGraphStore.__new__(Neo4jResearchGraphStore)
    store.driver, store.database = Driver(), None
    proof = store.get_source_receipt(
        _scope(), document.model_copy(update={"source_id": "NEW_CALL"})
    )
    assert proof == memory.get_source_receipt(_scope(), document)
    query, parameters = calls[-1]
    assert parameters == {
        "run_id": "run-a",
        "source_key": document.url,
        "version": document.content_hash,
    }
    assert "content_hash: $version" in query and "*0..3" in query
    assert "node.run_id = $run_id" in query and "edge.run_id = $run_id" in query
    assert store.get_source_receipt(_scope("other-run"), document) is None
    rows[0]["source_id"] = "NOT_WRITTEN"
    assert store.get_source_receipt(_scope(), document) is None
    rows[0]["source_id"] = document.source_id
    rows[0]["nodes"] = [
        node.model_copy(update={"run_id": "other-run"}).model_dump()
        for node in memory.nodes.values()
    ]
    assert store.get_source_receipt(_scope(), document) is None
    rows.clear()
    assert store.get_source_receipt(_scope(), document) is None


def test_neo4j_source_query_failure_propagates_without_receipt():
    class Driver:
        def execute_query(self, *args, **kwargs):
            raise OSError("fixture Neo4j query failed")

    store = Neo4jResearchGraphStore.__new__(Neo4jResearchGraphStore)
    store.driver, store.database = Driver(), None
    with pytest.raises(OSError, match="Neo4j query failed"):
        store.get_source_receipt(_scope(), _document())


# 验证图模式同角色报告替换，旧映射更新仍兼容追加。
def test_graph_report_updates_replace_same_role_but_legacy_mapping_still_appends() -> (
    None
):
    updated = agents_reducer(
        {"public_signal": {"report": "old"}},
        {"public_signal": {"report": {"type": "override", "value": "new"}}},
    )
    assert updated == {"public_signal": {"report": "new"}}
    legacy = agents_reducer(
        {"public_signal": {"report": "old"}},
        {"public_signal": {"report": "new"}},
    )
    assert "old" in legacy["public_signal"]["report"]
    assert "new" in legacy["public_signal"]["report"]


# 验证图角色路径不再调用旧完整历史压缩。
@pytest.mark.parametrize("duplicate_round", [False, True])
def test_graph_agent_path_avoids_legacy_full_history_compression(
    monkeypatch, tmp_path, duplicate_round
) -> None:
    import asyncio

    # 提供固定原始搜索结果的异步工具替身。
    class FixtureTool:
        name = "web_search"
        metadata = {"type": "search", "name": "web_search"}

        # 返回带真实来源字段的固定原始搜索 JSON。
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

    # 按模型和输出结构模拟图角色的抽取、工作区更新及报告生成。
    class AgentFixtureModel:
        # 初始化测试替身的 agent_calls、model_name、schema 状态。
        def __init__(self, model_name="fixture", schema=None):
            self.model_name = model_name
            self.schema = schema
            self.agent_calls = 0

        # 提供工具绑定测试接口，保留链式模型调用。
        def bind_tools(self, _tools):
            return self

        # 提供链式重试配置接口，不执行真实请求。
        def with_retry(self, **_kwargs):
            return self

        # 将测试结构化响应函数包装为 Runnable，兼容真实输出链。
        def with_structured_output(self, schema, include_raw=False):
            structured = AgentFixtureModel(self.model_name, schema=schema)
            return RunnableLambda(structured._structured_call)

        # 根据传入模型名创建新的图角色模型替身。
        def with_config(self, config):
            return AgentFixtureModel(config.get("model", self.model_name))

        # 返回包含原始响应、解析结果和错误字段的结构化测试响应。
        async def _structured_call(self, messages, config=None):
            parsed = await self._parse(messages)
            return {
                "raw": AIMessage(
                    content="",
                    response_metadata={"model_name": "fixture"},
                    usage_metadata={
                        "input_tokens": 7,
                        "output_tokens": 3,
                        "total_tokens": 10,
                    },
                ),
                "parsed": parsed,
                "parsing_error": None,
            }

        # 按当前结构化模式构造预设图抽取或工作上下文结果。
        async def _parse(self, messages):
            if self.schema is GraphExtractionOutput:
                source_id = re.search(
                    r'"source_id":\s*"([^"]+)"', str(messages[0].content)
                ).group(1)
                return GraphExtractionOutput(
                    claims=[
                        {"local_id": "c1", "statement": "A brake issue is reported."}
                    ],
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
            return None

        # 按请求阶段返回结构化输出、工具调用或最终图报告。
        async def ainvoke(self, messages, config=None):
            if self.schema is not None:
                return await self._structured_call(messages, config)
            if "Generate a bounded public-opinion role report" in str(
                messages[0].content
            ):
                return SimpleNamespace(content="bounded graph report")
            self.agent_calls += 1
            if self.agent_calls <= (2 if duplicate_round else 1):
                return AIMessage(
                    content="search",
                    tool_calls=[
                        {
                            "name": "web_search",
                            "args": {"queries": ["brake"]},
                            "id": f"call-graph-agent-{self.agent_calls}",
                        }
                    ],
                )
            if duplicate_round:
                tool_messages = [
                    message for message in messages if isinstance(message, ToolMessage)
                ]
                assert len(tool_messages) == 2
                assert all(
                    message.content.startswith("[Research result compacted.")
                    for message in tool_messages
                )
                source_lines = [
                    next(
                        line
                        for line in message.content.splitlines()
                        if line.startswith("Sources:")
                    )
                    for message in tool_messages
                ]
                assert source_lines[0] == source_lines[1]
                assert {message.tool_call_id for message in tool_messages} == {
                    "call-graph-agent-1",
                    "call-graph-agent-2",
                }
            return AIMessage(content="done")

    fixture_model = AgentFixtureModel()
    monkeypatch.setattr(business_agent_module, "_CONFIGURABLE_MODEL", fixture_model)
    store = InMemoryResearchGraphStore()
    workspace_type = business_agent_module.ResearchWorkspace

    def workspace_with_store(**kwargs):
        return workspace_type(**kwargs, store=store)

    monkeypatch.setattr(
        business_agent_module, "ResearchWorkspace", workspace_with_store
    )
    monkeypatch.setattr(
        business_agent_module,
        "_business_agent_tools",
        lambda _config, _role, _spec=None: asyncio.sleep(0, result=[FixtureTool()]),
    )

    # 若图生产路径调用旧版全历史压缩则立即使测试失败。
    async def fail_legacy_compression(*_args, **_kwargs):
        raise AssertionError("Graph Producer must not invoke compress_research")

    monkeypatch.setattr(
        business_agent_module, "_compress_research", fail_legacy_compression
    )
    result = asyncio.run(
        business_agent_module.run_business_agent(
            state={
                "workflow": {
                    "brief": "Investigate brake safety.",
                    "round": 1,
                    "pending_tasks": [],
                    "completed_tasks": [],
                },
                "research": {"run_id": "run-graph-agent", "working_contexts": {}},
                "agents": {},
                "runtime": {"budget": {}, "metrics": {}},
            },
            config={
                "configurable": {
                    "research_graph_enabled": True,
                    "research_graph_backend": "memory",
                    "enabled_business_agents": ["public_signal"],
                    "research_graph_transcript_dir": str(tmp_path),
                    "recent_raw_steps": 0,
                    "thread_id": "run-graph-agent",
                }
            },
            role="public_signal",
        )
    )
    assert result["agents"]["public_signal"]["report"]["type"] == "override"
    assert (
        "bounded graph report" in result["agents"]["public_signal"]["report"]["value"]
    )
    assert (
        result["research"]["working_contexts"]["public_signal"]["recent_progress"]
        == "updated"
    )
    assert (
        len([node for node in store.nodes.values() if node.node_type == "Source"]) == 1
    )
