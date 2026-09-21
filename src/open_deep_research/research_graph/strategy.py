"""Producer/consumer strategies operating on a per-run Research Workspace."""

from __future__ import annotations

import json
import logging
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
    ContextManager,
    render_working_context,
)
from open_deep_research.research_graph.extractor import (
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
from open_deep_research.research_graph.workspace import (
    ResearchWorkspace,
    TaskDescriptor,
    ToolBatchItem,
)

LOGGER = logging.getLogger(__name__)



@dataclass
class WorkspaceHookResult:
    """Lifecycle output returned after a tool batch."""

    messages: list[Any]
    succeeded: bool = True
    budget_usage: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceFinalResult:
    """Final role report output from a graph strategy."""

    report: str
    raw_notes: list[str] = field(default_factory=list)
    budget_usage: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)


class ContextStrategy(Protocol):
    """Small lifecycle interface shared by standard and graph strategies."""

    name: str
    graph_enabled: bool
    is_producer: bool

    async def before_model(self, workspace: ResearchWorkspace, messages: list[Any]) -> list[Any]:
        """Build the next bounded model input."""

    async def after_tool_batch(
        self,
        workspace: ResearchWorkspace,
        messages: list[Any],
        batch: list[ToolBatchItem],
    ) -> WorkspaceHookResult:
        """Consume a completed tool batch."""

    async def finalize(
        self,
        workspace: ResearchWorkspace,
        messages: list[Any],
        expected_output: str,
    ) -> WorkspaceFinalResult | None:
        """Finalize a role report when the strategy owns report generation."""



class StandardContextStrategy:
    """Compatibility strategy that preserves the existing lifecycle."""

    name = "standard"
    graph_enabled = False
    is_producer = False

    async def before_model(self, workspace: ResearchWorkspace, messages: list[Any]) -> list[Any]:
        """Return the unmodified standard system-plus-history input."""
        return list(messages)

    async def after_tool_batch(
        self,
        workspace: ResearchWorkspace,
        messages: list[Any],
        batch: list[ToolBatchItem],
    ) -> WorkspaceHookResult:
        """Leave standard tool results unchanged."""
        return WorkspaceHookResult(messages=list(messages))

    async def finalize(
        self,
        workspace: ResearchWorkspace,
        messages: list[Any],
        expected_output: str,
    ) -> WorkspaceFinalResult | None:
        """Defer standard report compression to the legacy caller."""
        return None


class _ResearchGraphStrategyBase:
    """Shared bounded context behavior for producer and consumer strategies."""

    graph_enabled = True

    async def before_model(self, workspace: ResearchWorkspace, messages: list[Any]) -> list[Any]:
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
                {"role": workspace.role, "graph_node_ids": sorted(workspace.relevant_subgraph.node_ids)},
            )
        return WorkspaceFinalResult(
            report=str(getattr(response, "content", response) or ""),
            raw_notes=[],
            budget_usage=budget,
            metrics=workspace.metrics.as_dict(),
        )


class ResearchGraphProducerStrategy(_ResearchGraphStrategyBase):
    """Producer lifecycle: raw tools -> extraction -> graph -> context."""

    name = "research_graph_producer"
    is_producer = True

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
        documents: list[tuple[ToolBatchItem, RawResearchDocument]] = []
        seen_source_versions: dict[str, str] = {}
        skipped_by_call: dict[str, list[str]] = {}
        for item in batch:
            if not item.success:
                continue
            source_documents = build_source_documents_from_raw_result(
                run_id=workspace.run_id,
                role=workspace.role,
                research_round=workspace.scope.research_round,
                task_id=workspace.scope.task_id,
                tool_name=item.tool_name,
                tool_call_id=item.tool_call_id,
                args=item.args,
                observation=item.observation,
            )
            for document in source_documents:
                workspace.metrics.add("raw_tool_tokens_before_compact", estimate_text_tokens(document.content), quality="estimated")
                source_key = document.url or document.source_id
                if source_key in seen_source_versions and seen_source_versions[source_key] == document.content_hash:
                    skipped_by_call.setdefault(item.tool_call_id, []).append(document.source_id)
                    workspace.metrics.add("duplicate_source_skipped")
                    if document.url:
                        workspace.metrics.add("cache_hit_url")
                    continue
                seen_source_versions[source_key] = document.content_hash
                if document.url:
                    if workspace.store is not None and workspace.store.source_is_persisted(workspace.scope, document):
                        skipped_by_call.setdefault(item.tool_call_id, []).append(document.source_id)
                        workspace.metrics.add("cache_hit_url")
                        workspace.metrics.add("duplicate_source_skipped")
                        continue
                    workspace.metrics.add("cache_miss_url")
                documents.append((item, document))

        if not documents:
            # Exact duplicates can still be compacted; failures and empty tools
            # have no receipt and therefore remain raw.
            for call_id, source_ids in skipped_by_call.items():
                workspace.receipts[call_id] = WriteReceipt(
                    run_id=workspace.run_id,
                    role=workspace.role,
                    research_round=workspace.scope.research_round,
                    task_id=workspace.scope.task_id,
                    source_ids=source_ids,
                    duplicate_source_ids=source_ids,
                )
            compacted = micro_compact_messages(
                messages,
                workspace.receipts,
                recent_raw_steps=int(getattr(workspace.configurable, "recent_raw_steps", 3)),
            )
            workspace.metrics.add("micro_compact_count", len(compacted.compacted_tool_call_ids))
            workspace.metrics.add("micro_compact_tokens_removed", compacted.tokens_removed)
            return WorkspaceHookResult(
                messages=compacted.messages,
                metrics=workspace.metrics.as_dict(),
            )

        graph_model_name = str(
            getattr(workspace.configurable, "research_graph_extraction_model", None)
            or getattr(workspace.configurable, "research_model", "")
        )
        graph_model = workspace.model_factory(
            graph_model_name,
            int(getattr(workspace.configurable, "research_graph_extraction_model_max_tokens", 4096)),
        )
        extractor = GraphExtractor(
            model=graph_model,
            model_name=graph_model_name,
            max_tokens=int(getattr(workspace.configurable, "research_graph_extraction_model_max_tokens", 4096)),
            max_retries=int(getattr(workspace.configurable, "max_structured_output_retries", 3)),
            batch_token_limit=int(getattr(workspace.configurable, "research_graph_extraction_batch_tokens", 12_000)),
        )
        source_only_documents = [document for _, document in documents]
        try:
            extraction = await extractor.extract(source_only_documents, scope=workspace.scope)
            receipts_by_call: dict[str, WriteReceipt] = {}
            combined_deltas = []
            for batch_result in extraction.batches:
                delta = batch_result.delta
                if workspace.store is None:
                    raise RuntimeError("Producer strategy has no Research Graph store.")
                write_started_at = time.perf_counter()
                receipt = workspace.store.write_delta(delta)
                workspace.metrics.add(
                    "graph_write_latency",
                    (time.perf_counter() - write_started_at) * 1000,
                    quality="exact",
                )
                combined_deltas.append(delta)
                workspace.metrics.add("graph_nodes_written", len(receipt.node_ids))
                workspace.metrics.add("graph_edges_written", len(receipt.edge_ids))
                input_tokens = batch_result.input_tokens
                output_tokens = batch_result.output_tokens
                workspace.metrics.add(
                    "graph_extraction_input_tokens",
                    input_tokens if input_tokens else estimate_text_tokens(_render_documents(source_only_documents)),
                    quality="exact" if input_tokens else "estimated",
                )
                workspace.metrics.add(
                    "graph_extraction_output_tokens",
                    output_tokens if output_tokens else estimate_text_tokens(json.dumps(delta.model_dump(mode="json"), ensure_ascii=False)),
                    quality="exact" if output_tokens else "estimated",
                )
                for item, document in documents:
                    if document.source_id in receipt.source_ids:
                        receipts_by_call[item.tool_call_id] = _merge_receipts(
                            receipts_by_call.get(item.tool_call_id),
                            receipt,
                        )
            workspace.metrics.add("graph_extraction_calls", len(extraction.batches))
            for call_id, source_ids in skipped_by_call.items():
                receipts_by_call[call_id] = _merge_receipts(
                    receipts_by_call.get(call_id),
                    WriteReceipt(
                        run_id=workspace.run_id,
                        role=workspace.role,
                        research_round=workspace.scope.research_round,
                        task_id=workspace.scope.task_id,
                        source_ids=source_ids,
                        duplicate_source_ids=source_ids,
                    ),
                )
            workspace.receipts.update(receipts_by_call)
            if combined_deltas:
                if workspace.store is None:
                    raise RuntimeError("Producer strategy has no Research Graph store.")
                workspace.context_for(query_suffix="new research delta")
                context_model_name = str(
                    getattr(workspace.configurable, "context_manager_model", None)
                    or getattr(workspace.configurable, "research_model", "")
                )
                context_manager = ContextManager(
                    model=workspace.model_factory(
                        context_model_name,
                        int(getattr(workspace.configurable, "context_manager_model_max_tokens", 2048)),
                    ),
                    model_name=context_model_name,
                    max_retries=int(getattr(workspace.configurable, "max_structured_output_retries", 3)),
                    max_active_findings=int(getattr(workspace.configurable, "working_context_max_active_findings", 8)),
                    max_active_claims=int(getattr(workspace.configurable, "working_context_max_active_claims", 16)),
                    max_active_evidence=int(getattr(workspace.configurable, "working_context_max_active_evidence", 24)),
                    max_open_gaps=int(getattr(workspace.configurable, "working_context_max_open_gaps", 8)),
                    max_conflicts=int(getattr(workspace.configurable, "working_context_max_conflicts", 8)),
                )
                context_result = await context_manager.update(
                    task=workspace.task,
                    current=workspace.working_context,
                    relevant_subgraph=workspace.relevant_subgraph,
                    research_delta=combined_deltas,
                )
                workspace.working_context = context_result.context
                workspace.metrics.add("context_manager_calls")
                context_input_tokens = estimate_text_tokens(
                    json.dumps(
                        {
                            "task": workspace.task.objective,
                            "context": workspace.working_context.model_dump(mode="json"),
                            "subgraph": workspace.relevant_subgraph.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                    )
                )
                workspace.metrics.add("context_manager_input_tokens", context_input_tokens, quality="estimated")
                output_tokens = context_result.budget_usage.get("output_tokens")
                workspace.metrics.add(
                    "context_manager_output_tokens",
                    output_tokens if isinstance(output_tokens, int) and output_tokens > 0 else estimate_text_tokens(context_result.delta.model_dump_json()),
                    quality="exact" if isinstance(output_tokens, int) and output_tokens > 0 else "estimated",
                )
                workspace.metrics.set(
                    "working_context_tokens",
                    estimate_text_tokens(render_working_context(workspace.working_context)),
                    quality="estimated",
                )
            compacted = micro_compact_messages(
                messages,
                workspace.receipts,
                recent_raw_steps=int(getattr(workspace.configurable, "recent_raw_steps", 3)),
            )
            workspace.metrics.add("micro_compact_count", len(compacted.compacted_tool_call_ids))
            workspace.metrics.add("micro_compact_tokens_removed", compacted.tokens_removed)
            workspace.metrics.add(
                "raw_tool_tokens_after_compact",
                estimate_context_tokens(compacted.messages),
                quality="estimated",
            )
            compacted_messages = compacted.messages
            return WorkspaceHookResult(
                messages=compacted_messages,
                budget_usage=merge_budget_usage(
                    extraction.budget_usage,
                    context_result.budget_usage if combined_deltas else {},
                ),
                metrics=workspace.metrics.as_dict(),
            )
        except Exception:
            # Preserve the raw messages when extraction, writing, or context
            # update fails.  A receipt is only issued after the entire pipeline
            # succeeds, so retry/debug tooling still has the original material.
            LOGGER.exception("Research Graph producer hook failed for role %s.", workspace.role)
            if workspace.transcript is not None:
                workspace.transcript.append("graph_hook_failure", {"role": workspace.role})
            return WorkspaceHookResult(
                messages=list(messages),
                succeeded=False,
                metrics=workspace.metrics.as_dict(),
            )


class ResearchGraphConsumerStrategy(_ResearchGraphStrategyBase):
    """Consumer lifecycle: scoped graph retrieval plus bounded analysis."""

    name = "research_graph_consumer"
    is_producer = False

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
        duplicate_source_ids=list(dict.fromkeys([*left.duplicate_source_ids, *right.duplicate_source_ids])),
    )


def _render_documents(documents: Iterable[RawResearchDocument]) -> str:
    return "\n".join(f"{document.source_id}: {document.content}" for document in documents)


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
