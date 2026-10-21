# 按标准、图生产和图消费策略管理模型上下文与工具批次生命周期。
"""Producer/consumer strategies operating on a per-run Research Workspace."""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from langchain_core.messages import HumanMessage

from open_deep_research.budget import (
    ainvoke_model_with_budget,
    estimate_context_tokens,
    estimate_text_tokens,
    merge_budget_usage,
)
from open_deep_research.research_graph.compaction import (
    micro_compact_messages,
)
from open_deep_research.research_graph.context_manager import (
    render_working_context,
)
from open_deep_research.research_graph.extractor import (
    ExtractionResult,
    GraphExtractor,
    build_source_documents_from_raw_result,
)
from open_deep_research.research_graph.models import (
    RawResearchDocument,
    WriteReceipt,
)
from open_deep_research.research_graph.retriever import (
    format_relevant_subgraph,
)
from open_deep_research.research_graph.schema import content_hash
from open_deep_research.research_graph.store import ResearchGraphError
from open_deep_research.research_graph.workspace import (
    ResearchWorkspace,
    TaskDescriptor,
    ToolBatchItem,
)

LOGGER = logging.getLogger(__name__)


# 保存工具批次处理后的消息、回执和统计更新。
@dataclass
class WorkspaceHookResult:
    """Lifecycle output returned after a tool batch."""

    messages: list[Any]
    succeeded: bool = True
    budget_usage: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    error_type: str | None = None


# 保存图策略负责生成的最终角色报告。
@dataclass
class WorkspaceFinalResult:
    """Final role report output from a graph strategy."""

    report: str
    raw_notes: list[str] = field(default_factory=list)
    budget_usage: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)


# 约定模型调用前、工具批次后和报告收尾的上下文生命周期。
class ContextStrategy(Protocol):
    """Small lifecycle interface shared by standard and graph strategies."""

    name: str
    graph_enabled: bool
    is_producer: bool

    # 约定构建下一次模型调用所需的有限上下文。
    async def before_model(
        self, workspace: ResearchWorkspace, messages: list[Any]
    ) -> list[Any]:
        """Build the next bounded model input."""

    # 约定消费刚执行完的工具批次。
    async def after_tool_batch(
        self,
        workspace: ResearchWorkspace,
        messages: list[Any],
        batch: list[ToolBatchItem],
    ) -> WorkspaceHookResult:
        """Consume a completed tool batch."""

    # 约定由策略负责时生成最终角色报告。
    async def finalize(
        self,
        workspace: ResearchWorkspace,
        messages: list[Any],
        expected_output: str,
    ) -> WorkspaceFinalResult | None:
        """Finalize a role report when the strategy owns report generation."""


# 保留原有消息历史与报告压缩行为的兼容策略。
class StandardContextStrategy:
    """Compatibility strategy that preserves the existing lifecycle."""

    name = "standard"
    graph_enabled = False
    is_producer = False

    # 原样返回标准系统消息与历史组成的模型输入。
    async def before_model(
        self, workspace: ResearchWorkspace, messages: list[Any]
    ) -> list[Any]:
        """Return the unmodified standard system-plus-history input."""
        return list(messages)

    # 保留标准模式工具结果，不执行图抽取。
    async def after_tool_batch(
        self,
        workspace: ResearchWorkspace,
        messages: list[Any],
        batch: list[ToolBatchItem],
    ) -> WorkspaceHookResult:
        """Leave standard tool results unchanged."""
        return WorkspaceHookResult(messages=list(messages))

    # 将标准模式的报告压缩交回原调用流程。
    async def finalize(
        self,
        workspace: ResearchWorkspace,
        messages: list[Any],
        expected_output: str,
    ) -> WorkspaceFinalResult | None:
        """Defer standard report compression to the legacy caller."""
        return None


# 复用图生产与图消费共有的有限上下文行为。
class _ResearchGraphStrategyBase:
    """Shared bounded context behavior for producer and consumer strategies."""

    graph_enabled = True

    # 检索相关子图并组合当前工作上下文，构造有界模型输入。
    async def before_model(
        self, workspace: ResearchWorkspace, messages: list[Any]
    ) -> list[Any]:
        if not workspace.relevant_subgraph.nodes and workspace.store is not None:
            workspace.context_for()
        items = list(messages)
        assignment = items[:1]
        history = items[1:]
        context_message = HumanMessage(
            content=(
                "<Research Graph Working Context>\n"
                f"Current task: {workspace.task.objective}\n"
                f"Working Context:\n{render_working_context(workspace.working_context)}\n\n"
                f"Relevant Research Memory:\n{format_relevant_subgraph(workspace.relevant_subgraph)}\n\n"
                f"Rolling Summary:\n{workspace.rolling_summary or 'None'}\n"
                "</Research Graph Working Context>"
            )
        )
        return [*assignment, context_message, *history]

    # 基于当前图证据和工作上下文生成可追溯的角色报告。
    async def finalize(
        self,
        workspace: ResearchWorkspace,
        messages: list[Any],
        expected_output: str,
    ) -> WorkspaceFinalResult | None:
        if workspace.store is not None:
            workspace.context_for(query_suffix="final role report")
        recent_text = _render_recent_messages(messages)
        prompt = (
            "Generate a bounded public-opinion role report from the current research "
            "task, Working Context, and relevant Research Graph subgraph. Do not read "
            "or reconstruct the complete raw ReAct history. Keep Evidence, Finding, "
            "and Recommendation/Strategy distinct. Any fact or analytical conclusion "
            "must retain graph IDs so it can be traced Finding -> Claim -> Evidence -> Source. "
            "Do not invent URLs or source metadata.\n\n"
            f"Role: {workspace.role}\n"
            f"Expected output: {expected_output}\n"
            f"Task:\n{workspace.task.objective}\n\n"
            f"Working Context:\n{render_working_context(workspace.working_context)}\n\n"
            f"Relevant subgraph:\n{format_relevant_subgraph(workspace.relevant_subgraph)}\n\n"
            f"Recent analysis only:\n{recent_text}\n\n"
            "Return the concise role report."
        )
        model_name = str(
            getattr(workspace.configurable, "research_graph_role_report_model", None)
            or getattr(workspace.configurable, "compression_model", "")
        )
        max_tokens = int(
            getattr(workspace.configurable, "research_graph_role_report_max_tokens", 0)
            or getattr(workspace.configurable, "compression_model_max_tokens", 8192)
        )
        response, budget = await ainvoke_model_with_budget(
            workspace.model_factory(model_name, max_tokens),
            [HumanMessage(content=prompt)],
            model_name=model_name,
            component="graph_role_report",
        )
        if workspace.transcript is not None:
            workspace.transcript.append(
                "role_report",
                {
                    "role": workspace.role,
                    "graph_node_ids": sorted(workspace.relevant_subgraph.node_ids),
                },
            )
        return WorkspaceFinalResult(
            report=str(getattr(response, "content", response) or ""),
            raw_notes=[],
            budget_usage=budget,
            metrics=workspace.metrics.as_dict(),
        )


@dataclass
class _ProducerBatch:
    """Retain unfinished graph stages so settlement never repeats extraction."""

    documents: list[tuple[ToolBatchItem, RawResearchDocument]]
    duplicates: list[tuple[ToolBatchItem, RawResearchDocument]]
    extraction: ExtractionResult | None = None
    written: int = 0
    context_done: bool = False
    context_budget: dict[str, Any] = field(default_factory=dict)


# 执行原始工具结果、抽取、图写入和工作上下文更新的生产链路。
class ResearchGraphProducerStrategy(_ResearchGraphStrategyBase):
    """Producer lifecycle: raw tools -> extraction -> graph -> context."""

    name = "research_graph_producer"
    is_producer = True

    # 持久化成功工具批次的证据，并用写入回执压缩原始正文。
    async def after_tool_batch(
        self,
        workspace: ResearchWorkspace,
        messages: list[Any],
        batch: list[ToolBatchItem],
    ) -> WorkspaceHookResult:
        """Persist and compact one successful producer tool batch."""
        if not batch:
            return WorkspaceHookResult(messages=list(messages))
        if workspace.transcript is not None:
            workspace.transcript.append(
                "raw_tool_batch",
                {
                    "role": workspace.role,
                    "items": [
                        {
                            "tool_name": item.tool_name,
                            "tool_call_id": item.tool_call_id,
                            "args": item.args,
                            "observation": item.observation,
                            "success": item.success,
                        }
                        for item in batch
                    ],
                },
            )
        key = tuple(sorted(item.tool_call_id for item in batch))
        try:
            stage = workspace.pending_ingestions.get(key)
            if stage is None:
                documents, duplicates = _producer_documents(workspace, batch)
                stage = _ProducerBatch(documents, duplicates)
                workspace.pending_ingestions[key] = stage
            if stage.extraction is None:
                graph_model_name = str(
                    getattr(
                        workspace.configurable, "research_graph_extraction_model", None
                    )
                    or getattr(workspace.configurable, "research_model", "")
                )
                max_tokens = int(
                    getattr(
                        workspace.configurable,
                        "research_graph_extraction_model_max_tokens",
                        4096,
                    )
                )
                extractor = GraphExtractor(
                    model=workspace.model_factory(graph_model_name, max_tokens),
                    model_name=graph_model_name,
                    max_tokens=max_tokens,
                    max_retries=int(
                        getattr(
                            workspace.configurable, "max_structured_output_retries", 3
                        )
                    ),
                    batch_token_limit=int(
                        getattr(
                            workspace.configurable,
                            "research_graph_extraction_batch_tokens",
                            12_000,
                        )
                    ),
                )
                stage.extraction = (
                    await extractor.extract(
                        [document for _, document in stage.documents],
                        scope=workspace.scope,
                    )
                    if stage.documents
                    else ExtractionResult()
                )
                workspace.metrics.add(
                    "graph_extraction_calls", len(stage.extraction.batches)
                )
            if workspace.store is None:
                raise ResearchGraphError(
                    "Producer strategy has no Research Graph store."
                )
            while stage.written < len(stage.extraction.batches):
                batch_result = stage.extraction.batches[stage.written]
                write_started_at = time.perf_counter()
                receipt = workspace.store.write_delta(batch_result.delta)
                if not receipt.has_complete_node_references or (
                    receipt.run_id,
                    receipt.role,
                    receipt.task_id,
                    receipt.research_round,
                ) != (
                    workspace.run_id,
                    workspace.role,
                    workspace.scope.task_id,
                    workspace.scope.research_round,
                ):
                    raise ResearchGraphError(
                        "Invalid graph write receipt; raw evidence retained."
                    )
                stage.written += 1
                workspace.metrics.add(
                    "graph_write_latency",
                    (time.perf_counter() - write_started_at) * 1000,
                    quality="exact",
                )
                workspace.metrics.add("graph_nodes_written", len(receipt.node_ids))
                workspace.metrics.add("graph_edges_written", len(receipt.edge_ids))
                workspace.metrics.add(
                    "graph_extraction_input_tokens",
                    batch_result.input_tokens
                    or estimate_text_tokens(
                        _render_documents([doc for _, doc in stage.documents])
                    ),
                    quality="exact" if batch_result.input_tokens else "estimated",
                )
                workspace.metrics.add(
                    "graph_extraction_output_tokens",
                    batch_result.output_tokens
                    or estimate_text_tokens(batch_result.delta.model_dump_json()),
                    quality="exact" if batch_result.output_tokens else "estimated",
                )
            receipts = _verified_tool_receipts(
                workspace, stage.documents, stage.duplicates
            )
            # A failed context update resumes here on settlement. Never let the
            # duplicate-source fast path acknowledge an unfinished update.
            if not stage.context_done and (stage.documents or stage.duplicates):
                workspace.context_for(query_suffix="new committed research delta")
                previous_context = workspace.working_context
                try:
                    context_result = await workspace.update_working_context(
                        stage.extraction.deltas
                    )
                    # Context inference awaits external work. Reconfirm every
                    # source afterwards before publishing context or receipts.
                    receipts = _verified_tool_receipts(
                        workspace, stage.documents, stage.duplicates
                    )
                except BaseException:
                    workspace.working_context = previous_context
                    raise
                stage.context_budget = context_result.budget_usage
                stage.context_done = True
            workspace.receipts.update(receipts)
            compacted = micro_compact_messages(
                messages,
                workspace.receipts,
                recent_raw_steps=int(
                    getattr(workspace.configurable, "recent_raw_steps", 3)
                ),
            )
            workspace.metrics.add(
                "micro_compact_count", len(compacted.compacted_tool_call_ids)
            )
            workspace.metrics.add(
                "micro_compact_tokens_removed", compacted.tokens_removed
            )
            workspace.metrics.add(
                "raw_tool_tokens_after_compact",
                estimate_context_tokens(compacted.messages),
                quality="estimated",
            )
            del workspace.pending_ingestions[key]
            return WorkspaceHookResult(
                messages=compacted.messages,
                budget_usage=merge_budget_usage(
                    stage.extraction.budget_usage, stage.context_budget
                ),
                metrics=workspace.metrics.as_dict(),
            )
        except Exception:
            return _producer_hook_failure(workspace, messages)


# 为分析角色检索当前运行的图证据，提供有限分析上下文。
class ResearchGraphConsumerStrategy(_ResearchGraphStrategyBase):
    """Consumer lifecycle: scoped graph retrieval plus bounded analysis."""

    name = "research_graph_consumer"
    is_producer = False

    # 刷新限定范围的图检索，同时保留消费角色原始工具结果。
    async def after_tool_batch(
        self,
        workspace: ResearchWorkspace,
        messages: list[Any],
        batch: list[ToolBatchItem],
    ) -> WorkspaceHookResult:
        """Refresh scoped graph retrieval while preserving consumer raw results."""
        if workspace.transcript is not None and batch:
            workspace.transcript.append(
                "consumer_tool_batch",
                {
                    "role": workspace.role,
                    "items": [
                        {
                            "tool_name": item.tool_name,
                            "tool_call_id": item.tool_call_id,
                            "args": item.args,
                            "observation": item.observation,
                            "success": item.success,
                        }
                        for item in batch
                    ],
                },
            )
        if workspace.store is not None:
            workspace.context_for(query_suffix="consumer analysis")
        # Consumer tool outputs are not source evidence by default.  Keep them
        # raw so a future consumer-specific evidence policy cannot lose data.
        # Under context pressure, incremental Rolling Compact can still replace
        # complete older steps; the transcript retains the original payload.
        bounded_messages = list(messages)
        return WorkspaceHookResult(
            messages=bounded_messages,
            metrics=workspace.metrics.as_dict(),
        )


# 在工作区初始化时解析并创建上下文策略。
def create_context_strategy(
    strategy_name: str,
    *,
    graph_enabled: bool,
) -> ContextStrategy:
    """Resolve the strategy once at workspace initialization."""
    normalized = str(strategy_name or "standard").strip().lower()
    if not graph_enabled or normalized == "standard":
        return StandardContextStrategy()
    if normalized == "research_graph_producer":
        return ResearchGraphProducerStrategy()
    if normalized == "research_graph_consumer":
        return ResearchGraphConsumerStrategy()
    raise ValueError(f"Unsupported context strategy: {strategy_name}")


# 按 URL 和内容版本区分新材料与待确认的重复材料。
def _producer_documents(
    workspace: ResearchWorkspace,
    batch: list[ToolBatchItem],
) -> tuple[
    list[tuple[ToolBatchItem, RawResearchDocument]],
    list[tuple[ToolBatchItem, RawResearchDocument]],
]:
    """Separate exact source versions without acknowledging pending writes."""
    documents: list[tuple[ToolBatchItem, RawResearchDocument]] = []
    duplicates: list[tuple[ToolBatchItem, RawResearchDocument]] = []
    seen: set[tuple[str, str]] = set()
    for item in batch:
        if not item.success:
            continue
        for document in build_source_documents_from_raw_result(
            run_id=workspace.run_id,
            role=workspace.role,
            research_round=workspace.scope.research_round,
            task_id=workspace.scope.task_id,
            tool_name=item.tool_name,
            tool_call_id=item.tool_call_id,
            args=item.args,
            observation=item.observation,
        ):
            if workspace.task.related_task_ids:
                document.metadata["research_task_ids"] = list(
                    workspace.task.related_task_ids
                )
            if workspace.store is None:
                raise ResearchGraphError(
                    "Producer strategy has no Research Graph store."
                )
            workspace.metrics.add(
                "raw_tool_tokens_before_compact",
                estimate_text_tokens(document.content),
                quality="estimated",
            )
            key = (
                document.url or document.source_id,
                document.content_hash or content_hash(document.content),
            )
            existing = None
            if key not in seen and document.url:
                existing = workspace.store.get_source_receipt(workspace.scope, document)
            if key in seen or existing is not None:
                duplicates.append((item, document))
                workspace.metrics.add("duplicate_source_skipped")
                if document.url:
                    workspace.metrics.add("cache_hit_url")
            else:
                documents.append((item, document))
                if document.url:
                    workspace.metrics.add("cache_miss_url")
            seen.add(key)
    return documents, duplicates


def _verified_tool_receipts(
    workspace: ResearchWorkspace,
    documents: list[tuple[ToolBatchItem, RawResearchDocument]],
    duplicates: list[tuple[ToolBatchItem, RawResearchDocument]],
) -> dict[str, WriteReceipt]:
    """Resolve written and duplicate observations to actual persisted source IDs."""
    receipts: dict[str, WriteReceipt] = {}
    verified: dict[tuple[str, str], WriteReceipt] = {}
    for entries, duplicate in ((documents, False), (duplicates, True)):
        for item, document in entries:
            if workspace.store is None:
                raise ResearchGraphError(
                    "Producer strategy has no Research Graph store."
                )
            key = (
                document.url or document.source_id,
                document.content_hash or content_hash(document.content),
            )
            if key not in verified:
                proof = workspace.store.get_source_receipt(workspace.scope, document)
                if (
                    proof is None
                    or (proof.run_id, proof.role, proof.research_round, proof.task_id)
                    != (
                        workspace.scope.run_id,
                        workspace.scope.role,
                        workspace.scope.research_round,
                        workspace.scope.task_id,
                    )
                    or not proof.has_complete_node_references
                ):
                    raise ResearchGraphError(
                        "Unable to confirm persisted source/provenance for the tool result."
                    )
                verified[key] = proof
            receipt = verified[key].model_copy(
                update={
                    "duplicate_source_ids": list(verified[key].source_ids)
                    if duplicate
                    else [],
                }
            )
            receipts[item.tool_call_id] = _merge_receipts(
                receipts.get(item.tool_call_id), receipt
            )
    return receipts


def _producer_hook_failure(
    workspace: ResearchWorkspace, messages: list[Any]
) -> WorkspaceHookResult:
    LOGGER.exception("Research Graph producer hook failed for role %s.", workspace.role)
    if workspace.transcript is not None:
        workspace.transcript.append("graph_hook_failure", {"role": workspace.role})
    return WorkspaceHookResult(
        messages=list(messages),
        succeeded=False,
        error_type=type(sys.exc_info()[1]).__name__,
        metrics=workspace.metrics.as_dict(),
    )


# 合并已确认的回执，保留可追踪的节点和来源标识。
def _merge_receipts(left: WriteReceipt | None, right: WriteReceipt) -> WriteReceipt:
    if left is None:
        return right
    return WriteReceipt(
        run_id=right.run_id,
        role=right.role,
        research_round=right.research_round,
        task_id=right.task_id,
        source_ids=list(dict.fromkeys([*left.source_ids, *right.source_ids])),
        evidence_ids=list(dict.fromkeys([*left.evidence_ids, *right.evidence_ids])),
        claim_ids=list(dict.fromkeys([*left.claim_ids, *right.claim_ids])),
        finding_ids=list(dict.fromkeys([*left.finding_ids, *right.finding_ids])),
        node_ids=list(dict.fromkeys([*left.node_ids, *right.node_ids])),
        edge_ids=list(dict.fromkeys([*left.edge_ids, *right.edge_ids])),
        duplicate_source_ids=list(
            dict.fromkeys([*left.duplicate_source_ids, *right.duplicate_source_ids])
        ),
    )


# 将来源文档整理为受控模型输入文本。
def _render_documents(documents: Iterable[RawResearchDocument]) -> str:
    return "\n".join(
        f"{document.source_id}: {document.content}" for document in documents
    )


# 渲染近期消息，补充当前任务的最新观察。
def _render_recent_messages(messages: Iterable[Any], *, max_steps: int = 3) -> str:
    items = list(messages)
    text_parts = [
        str(getattr(message, "content", "") or "")
        for message in items[-max(1, max_steps * 3) :]
        if getattr(message, "content", "")
    ]
    text = "\n".join(text_parts)
    return text[:12_000] + ("\n[recent analysis bounded]" if len(text) > 12_000 else "")


__all__ = [
    "ContextStrategy",
    "WorkspaceFinalResult",
    "WorkspaceHookResult",
    "ResearchWorkspace",
    "ResearchGraphConsumerStrategy",
    "ResearchGraphProducerStrategy",
    "StandardContextStrategy",
    "TaskDescriptor",
    "ToolBatchItem",
    "create_context_strategy",
]
