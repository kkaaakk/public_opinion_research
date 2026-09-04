"""Context Strategy routing and the shared ReAct Harness lifecycle."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from langchain_core.messages import HumanMessage, SystemMessage

from open_deep_research.budget import (
    budget_from_model_response,
    estimate_tokens,
    merge_budget_usage,
)
from open_deep_research.observability import observe_model_ainvoke
from open_deep_research.research_graph.compaction import (
    context_token_estimate,
    micro_compact_messages,
    render_protected_context,
    rolling_compact,
    should_rolling_compact,
)
from open_deep_research.research_graph.context_manager import (
    ContextManager,
    initial_working_context,
    render_working_context,
)
from open_deep_research.research_graph.extractor import (
    GraphExtractor,
    build_source_documents_from_raw_result,
)
from open_deep_research.research_graph.metrics import ResearchGraphMetrics
from open_deep_research.research_graph.models import (
    RawResearchDocument,
    RelevantSubgraph,
    ResearchGraphScope,
    WorkingContext,
    WriteReceipt,
)
from open_deep_research.research_graph.retriever import (
    ResearchGraphRetriever,
    format_relevant_subgraph,
)
from open_deep_research.research_graph.schema import stable_id
from open_deep_research.research_graph.store import (
    ResearchGraphStore,
    create_research_graph_store,
)
from open_deep_research.research_graph.transcript import ResearchTranscript

LOGGER = logging.getLogger(__name__)


@dataclass
class TaskDescriptor:
    """Role-neutral task view used by graph strategies."""

    task_id: str
    objective: str
    evidence_needed: str
    reason: str = ""


@dataclass
class ToolBatchItem:
    """One tool call and its observed result."""

    tool_name: str
    tool_call_id: str
    args: Any
    observation: Any
    success: bool


@dataclass
class HarnessHookResult:
    """Lifecycle output returned after a tool batch."""

    messages: list[Any]
    succeeded: bool = True
    budget_usage: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class HarnessFinalResult:
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

    async def before_model(self, harness: ResearchContextHarness, messages: list[Any]) -> list[Any]:
        """Build the next bounded model input."""

    async def after_tool_batch(
        self,
        harness: ResearchContextHarness,
        messages: list[Any],
        batch: list[ToolBatchItem],
    ) -> HarnessHookResult:
        """Consume a completed tool batch."""

    async def finalize(
        self,
        harness: ResearchContextHarness,
        messages: list[Any],
        expected_output: str,
    ) -> HarnessFinalResult | None:
        """Finalize a role report when the strategy owns report generation."""


class ResearchContextHarness:
    """Generic ReAct message lifecycle with pluggable context strategy."""

    def __init__(
        self,
        *,
        strategy: ContextStrategy,
        state: dict[str, Any],
        role: str,
        assignment: str,
        agent_prompt: str,
        configurable: Any,
        runtime_config: Any,
        model_factory: Callable[[str, int | None], Any],
        store: ResearchGraphStore | None = None,
        task: TaskDescriptor | None = None,
        run_id: str | None = None,
        driver_factory: Callable[..., Any] | None = None,
        store_factory: Callable[..., ResearchGraphStore] | None = None,
    ) -> None:
        """Initialize one role's scoped strategy state and context memory."""
        self.strategy = strategy
        self.state = state
        self.role = role
        self.assignment = assignment
        self.agent_prompt = agent_prompt
        self.configurable = configurable
        self.runtime_config = runtime_config
        self.model_factory = model_factory
        self.driver_factory = driver_factory
        self.store_factory = store_factory
        self.run_id = run_id or stable_id("RUN", role, assignment)
        self.task = task or TaskDescriptor(
            task_id=stable_id("TASK", self.run_id, role),
            objective=assignment,
            evidence_needed="Evidence relevant to the role objective.",
        )
        self.scope = ResearchGraphScope(
            run_id=self.run_id,
            role=role,
            research_round=max(1, int(state.get("research_round", 1) or 1)),
            task_id=self.task.task_id,
        )
        self.store = store
        self.retriever: ResearchGraphRetriever | None = None
        if strategy.graph_enabled and self.store is None:
            self.store = create_research_graph_store(
                configurable,
                run_id=self.run_id,
                driver_factory=driver_factory,
                store_factory=store_factory,
            )
        if self.store is not None:
            self.retriever = ResearchGraphRetriever(
                self.store,
                max_nodes=int(getattr(configurable, "research_graph_max_retrieved_nodes", 24)),
                max_edges=int(getattr(configurable, "research_graph_max_retrieved_edges", 48)),
            )
        self.working_context = self._load_working_context()
        self.relevant_subgraph = RelevantSubgraph(run_id=self.run_id)
        self.rolling_summary = str(
            (state.get("rolling_summaries", {}) or {}).get(role, "")
        )
        self.receipts: dict[str, WriteReceipt] = {}
        self.metrics = ResearchGraphMetrics()
        self.transcript = (
            ResearchTranscript(
                getattr(configurable, "research_graph_transcript_dir", None),
                self.run_id,
            )
            if strategy.graph_enabled
            else None
        )

    def _load_working_context(self) -> WorkingContext:
        values = self.state.get("working_contexts", {}) or {}
        value = values.get(self.role) if isinstance(values, dict) else None
        if isinstance(value, WorkingContext):
            return value
        if isinstance(value, dict):
            try:
                return WorkingContext.model_validate(value)
            except Exception:
                pass
        return initial_working_context(self.task)

    async def before_model(self, messages: list[Any]) -> list[Any]:
        """Build the next strategy-specific model input."""
        return await self.strategy.before_model(self, messages)

    async def after_tool_batch(
        self,
        messages: list[Any],
        batch: list[ToolBatchItem],
    ) -> HarnessHookResult:
        """Run the strategy's post-tool lifecycle hook."""
        return await self.strategy.after_tool_batch(self, messages, batch)

    async def finalize(
        self,
        messages: list[Any],
        expected_output: str,
    ) -> HarnessFinalResult | None:
        """Finalize a graph report or return ``None`` for standard mode."""
        return await self.strategy.finalize(self, messages, expected_output)

    def protected_context(self) -> str:
        """Return the context block protected from rolling history compaction."""
        return render_protected_context(
            current_task=self.task.objective,
            working_context=render_working_context(self.working_context),
            relevant_subgraph=format_relevant_subgraph(self.relevant_subgraph),
            rolling_summary=self.rolling_summary,
        )

    def refresh_retrieval(self, *, query_suffix: str = "") -> RelevantSubgraph:
        """Retrieve scoped graph memory for the current task."""
        if self.retriever is None:
            return self.relevant_subgraph
        started_at = time.perf_counter()
        self.relevant_subgraph = self.retriever.retrieve(
            self.task,
            scope=self.scope,
            working_context=self.working_context,
            query_suffix=query_suffix,
        )
        self.metrics.add("graph_retrieval_calls")
        self.metrics.add(
            "graph_retrieval_latency",
            (time.perf_counter() - started_at) * 1000,
            quality="exact",
        )
        self.metrics.add("retrieved_nodes", len(self.relevant_subgraph.nodes))
        self.metrics.add("retrieved_edges", len(self.relevant_subgraph.edges))
        return self.relevant_subgraph

    def update_state_payload(self) -> dict[str, Any]:
        """Return graph state channels without placing raw content in them."""
        return {
            "research_run_id": self.run_id,
            "working_contexts": {self.role: self.working_context.model_dump(mode="json")},
            "rolling_summaries": {self.role: self.rolling_summary},
            "research_graph_metrics": self.metrics.as_dict(),
        }


class StandardContextStrategy:
    """Compatibility strategy that preserves the existing lifecycle."""

    name = "standard"
    graph_enabled = False
    is_producer = False

    async def before_model(self, harness: ResearchContextHarness, messages: list[Any]) -> list[Any]:
        """Return the unmodified standard system-plus-history input."""
        return [SystemMessage(content=harness.agent_prompt), *messages]

    async def after_tool_batch(
        self,
        harness: ResearchContextHarness,
        messages: list[Any],
        batch: list[ToolBatchItem],
    ) -> HarnessHookResult:
        """Leave standard tool results unchanged."""
        return HarnessHookResult(messages=list(messages))

    async def finalize(
        self,
        harness: ResearchContextHarness,
        messages: list[Any],
        expected_output: str,
    ) -> HarnessFinalResult | None:
        """Defer standard report compression to the legacy caller."""
        return None


class _ResearchGraphStrategyBase:
    """Shared bounded context behavior for producer and consumer strategies."""

    graph_enabled = True

    async def before_model(self, harness: ResearchContextHarness, messages: list[Any]) -> list[Any]:
        if not harness.relevant_subgraph.nodes and harness.store is not None:
            harness.refresh_retrieval()
        items = list(messages)
        assignment = items[:1]
        history = items[1:]
        context_message = HumanMessage(
            content=(
                "<Research Graph Working Context>\n"
                f"Current task: {harness.task.objective}\n"
                f"Working Context:\n{render_working_context(harness.working_context)}\n\n"
                f"Relevant Research Memory:\n{format_relevant_subgraph(harness.relevant_subgraph)}\n\n"
                f"Rolling Summary:\n{harness.rolling_summary or 'None'}\n"
                "</Research Graph Working Context>"
            )
        )
        return [SystemMessage(content=harness.agent_prompt), *assignment, context_message, *history]

    async def finalize(
        self,
        harness: ResearchContextHarness,
        messages: list[Any],
        expected_output: str,
    ) -> HarnessFinalResult | None:
        if harness.store is not None:
            harness.refresh_retrieval(query_suffix="final role report")
        recent_text = _render_recent_messages(messages)
        prompt = (
            "Generate a bounded public-opinion role report from the current research "
            "task, Working Context, and relevant Research Graph subgraph. Do not read "
            "or reconstruct the complete raw ReAct history. Keep Evidence, Finding, "
            "and Recommendation/Strategy distinct. Any fact or analytical conclusion "
            "must retain graph IDs so it can be traced Finding -> Claim -> Evidence -> Source. "
            "Do not invent URLs or source metadata.\n\n"
            f"Role: {harness.role}\n"
            f"Expected output: {expected_output}\n"
            f"Task:\n{harness.task.objective}\n\n"
            f"Working Context:\n{render_working_context(harness.working_context)}\n\n"
            f"Relevant subgraph:\n{format_relevant_subgraph(harness.relevant_subgraph)}\n\n"
            f"Recent analysis only:\n{recent_text}\n\n"
            "Return the concise role report."
        )
        model_name = str(
            getattr(harness.configurable, "research_graph_role_report_model", None)
            or getattr(harness.configurable, "compression_model", "")
        )
        max_tokens = int(
            getattr(harness.configurable, "research_graph_role_report_max_tokens", 0)
            or getattr(harness.configurable, "compression_model_max_tokens", 8192)
        )
        response = await observe_model_ainvoke(
            harness.model_factory(model_name, max_tokens),
            [HumanMessage(content=prompt)],
            observer_model=model_name,
            observer_component="graph_role_report",
        )
        budget = budget_from_model_response(response)
        if harness.transcript is not None:
            harness.transcript.append(
                "role_report",
                {"role": harness.role, "graph_node_ids": sorted(harness.relevant_subgraph.node_ids)},
            )
        return HarnessFinalResult(
            report=str(getattr(response, "content", response) or ""),
            raw_notes=[],
            budget_usage=budget,
            metrics=harness.metrics.as_dict(),
        )

    async def _maybe_rolling_compact(
        self,
        harness: ResearchContextHarness,
        messages: list[Any],
    ) -> list[Any]:
        model_limit = getattr(harness.configurable, "research_graph_context_capacity_tokens", None)
        if not model_limit:
            # The existing model lookup is only a capacity hint, never a
            # research stop condition.
            try:
                from open_deep_research.utils import get_model_token_limit

                model_limit = get_model_token_limit(harness.configurable.research_model)
            except Exception:
                model_limit = None
        if model_limit is None:
            return messages
        extra = harness.protected_context()
        if not should_rolling_compact(
            messages,
            extra_context=extra,
            model_context_capacity=model_limit,
            threshold_ratio=float(
                getattr(harness.configurable, "context_compaction_threshold_ratio", 0.75)
            ),
        ):
            return messages
        compaction_model_name = str(
            getattr(harness.configurable, "rolling_compaction_model", None)
            or getattr(harness.configurable, "compression_model", "")
        )
        compaction_model = harness.model_factory(
            compaction_model_name,
            int(getattr(harness.configurable, "rolling_compaction_model_max_tokens", 2048)),
        )
        before_tokens = context_token_estimate(messages, extra)
        result = await rolling_compact(
            messages,
            previous_summary=harness.rolling_summary,
            protected_context=extra,
            model=compaction_model,
            model_name=compaction_model_name,
            max_retries=int(getattr(harness.configurable, "max_structured_output_retries", 3)),
            recent_raw_steps=int(getattr(harness.configurable, "recent_raw_steps", 3)),
        )
        harness.rolling_summary = result.rolling_summary
        harness.metrics.add("rolling_compact_count")
        harness.metrics.add("rolling_compact_input_tokens", before_tokens, quality="estimated")
        output_tokens = result.budget_usage.get("output_tokens")
        if isinstance(output_tokens, int) and output_tokens > 0:
            harness.metrics.add("rolling_compact_output_tokens", output_tokens, quality="exact")
        else:
            harness.metrics.add(
                "rolling_compact_output_tokens",
                estimate_tokens(result.rolling_summary),
                quality="estimated",
            )
        harness.metrics.add("micro_compact_tokens_removed", result.tokens_removed)
        if harness.transcript is not None:
            harness.transcript.append(
                "rolling_compact",
                {
                    "tokens_removed": result.tokens_removed,
                    "rolling_summary": result.rolling_summary,
                },
            )
        return result.messages


class ResearchGraphProducerStrategy(_ResearchGraphStrategyBase):
    """Producer lifecycle: raw tools -> extraction -> graph -> context."""

    name = "research_graph_producer"
    is_producer = True

    async def after_tool_batch(
        self,
        harness: ResearchContextHarness,
        messages: list[Any],
        batch: list[ToolBatchItem],
    ) -> HarnessHookResult:
        """Persist and compact one successful producer tool batch."""
        if not batch:
            return HarnessHookResult(messages=list(messages))
        if harness.transcript is not None:
            harness.transcript.append(
                "raw_tool_batch",
                {
                    "role": harness.role,
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
                run_id=harness.run_id,
                role=harness.role,
                research_round=harness.scope.research_round,
                task_id=harness.scope.task_id,
                tool_name=item.tool_name,
                tool_call_id=item.tool_call_id,
                args=item.args,
                observation=item.observation,
            )
            for document in source_documents:
                harness.metrics.add("raw_tool_tokens_before_compact", estimate_tokens(document.content), quality="estimated")
                source_key = document.url or document.source_id
                if source_key in seen_source_versions and seen_source_versions[source_key] == document.content_hash:
                    skipped_by_call.setdefault(item.tool_call_id, []).append(document.source_id)
                    harness.metrics.add("duplicate_source_skipped")
                    if document.url:
                        harness.metrics.add("cache_hit_url")
                    continue
                seen_source_versions[source_key] = document.content_hash
                if document.url:
                    if harness.store is not None and harness.store.source_is_persisted(harness.scope, document):
                        skipped_by_call.setdefault(item.tool_call_id, []).append(document.source_id)
                        harness.metrics.add("cache_hit_url")
                        harness.metrics.add("duplicate_source_skipped")
                        continue
                    harness.metrics.add("cache_miss_url")
                documents.append((item, document))

        if not documents:
            # Exact duplicates can still be compacted; failures and empty tools
            # have no receipt and therefore remain raw.
            for call_id, source_ids in skipped_by_call.items():
                harness.receipts[call_id] = WriteReceipt(
                    run_id=harness.run_id,
                    role=harness.role,
                    research_round=harness.scope.research_round,
                    task_id=harness.scope.task_id,
                    source_ids=source_ids,
                    duplicate_source_ids=source_ids,
                )
            compacted = micro_compact_messages(
                messages,
                harness.receipts,
                recent_raw_steps=int(getattr(harness.configurable, "recent_raw_steps", 3)),
            )
            harness.metrics.add("micro_compact_count", len(compacted.compacted_tool_call_ids))
            harness.metrics.add("micro_compact_tokens_removed", compacted.tokens_removed)
            return HarnessHookResult(
                messages=compacted.messages,
                metrics=harness.metrics.as_dict(),
            )

        graph_model_name = str(
            getattr(harness.configurable, "research_graph_extraction_model", None)
            or getattr(harness.configurable, "research_model", "")
        )
        graph_model = harness.model_factory(
            graph_model_name,
            int(getattr(harness.configurable, "research_graph_extraction_model_max_tokens", 4096)),
        )
        extractor = GraphExtractor(
            model=graph_model,
            model_name=graph_model_name,
            max_tokens=int(getattr(harness.configurable, "research_graph_extraction_model_max_tokens", 4096)),
            max_retries=int(getattr(harness.configurable, "max_structured_output_retries", 3)),
            batch_token_limit=int(getattr(harness.configurable, "research_graph_extraction_batch_tokens", 12_000)),
        )
        source_only_documents = [document for _, document in documents]
        try:
            extraction = await extractor.extract(source_only_documents, scope=harness.scope)
            receipts_by_call: dict[str, WriteReceipt] = {}
            combined_deltas = []
            for batch_result in extraction.batches:
                delta = batch_result.delta
                if harness.store is None:
                    raise RuntimeError("Producer strategy has no Research Graph store.")
                write_started_at = time.perf_counter()
                receipt = harness.store.write_delta(delta)
                harness.metrics.add(
                    "graph_write_latency",
                    (time.perf_counter() - write_started_at) * 1000,
                    quality="exact",
                )
                combined_deltas.append(delta)
                harness.metrics.add("graph_nodes_written", len(receipt.node_ids))
                harness.metrics.add("graph_edges_written", len(receipt.edge_ids))
                input_tokens = batch_result.input_tokens
                output_tokens = batch_result.output_tokens
                harness.metrics.add(
                    "graph_extraction_input_tokens",
                    input_tokens if input_tokens else estimate_tokens(_render_documents(source_only_documents)),
                    quality="exact" if input_tokens else "estimated",
                )
                harness.metrics.add(
                    "graph_extraction_output_tokens",
                    output_tokens if output_tokens else estimate_tokens(json.dumps(delta.model_dump(mode="json"), ensure_ascii=False)),
                    quality="exact" if output_tokens else "estimated",
                )
                for item, document in documents:
                    if document.source_id in receipt.source_ids:
                        receipts_by_call[item.tool_call_id] = _merge_receipts(
                            receipts_by_call.get(item.tool_call_id),
                            receipt,
                        )
            harness.metrics.add("graph_extraction_calls", len(extraction.batches))
            for call_id, source_ids in skipped_by_call.items():
                receipts_by_call[call_id] = _merge_receipts(
                    receipts_by_call.get(call_id),
                    WriteReceipt(
                        run_id=harness.run_id,
                        role=harness.role,
                        research_round=harness.scope.research_round,
                        task_id=harness.scope.task_id,
                        source_ids=source_ids,
                        duplicate_source_ids=source_ids,
                    ),
                )
            harness.receipts.update(receipts_by_call)
            if combined_deltas:
                if harness.store is None:
                    raise RuntimeError("Producer strategy has no Research Graph store.")
                harness.refresh_retrieval(query_suffix="new research delta")
                context_model_name = str(
                    getattr(harness.configurable, "context_manager_model", None)
                    or getattr(harness.configurable, "research_model", "")
                )
                context_manager = ContextManager(
                    model=harness.model_factory(
                        context_model_name,
                        int(getattr(harness.configurable, "context_manager_model_max_tokens", 2048)),
                    ),
                    model_name=context_model_name,
                    max_retries=int(getattr(harness.configurable, "max_structured_output_retries", 3)),
                    max_active_findings=int(getattr(harness.configurable, "working_context_max_active_findings", 8)),
                    max_active_claims=int(getattr(harness.configurable, "working_context_max_active_claims", 16)),
                    max_active_evidence=int(getattr(harness.configurable, "working_context_max_active_evidence", 24)),
                    max_open_gaps=int(getattr(harness.configurable, "working_context_max_open_gaps", 8)),
                    max_conflicts=int(getattr(harness.configurable, "working_context_max_conflicts", 8)),
                )
                context_result = await context_manager.update(
                    task=harness.task,
                    current=harness.working_context,
                    relevant_subgraph=harness.relevant_subgraph,
                    research_delta=combined_deltas,
                )
                harness.working_context = context_result.context
                harness.metrics.add("context_manager_calls")
                context_input_tokens = estimate_tokens(
                    json.dumps(
                        {
                            "task": harness.task.objective,
                            "context": harness.working_context.model_dump(mode="json"),
                            "subgraph": harness.relevant_subgraph.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                    )
                )
                harness.metrics.add("context_manager_input_tokens", context_input_tokens, quality="estimated")
                output_tokens = context_result.budget_usage.get("output_tokens")
                harness.metrics.add(
                    "context_manager_output_tokens",
                    output_tokens if isinstance(output_tokens, int) and output_tokens > 0 else estimate_tokens(context_result.delta.model_dump_json()),
                    quality="exact" if isinstance(output_tokens, int) and output_tokens > 0 else "estimated",
                )
                harness.metrics.set(
                    "working_context_tokens",
                    estimate_tokens(render_working_context(harness.working_context)),
                    quality="estimated",
                )
            compacted = micro_compact_messages(
                messages,
                harness.receipts,
                recent_raw_steps=int(getattr(harness.configurable, "recent_raw_steps", 3)),
            )
            harness.metrics.add("micro_compact_count", len(compacted.compacted_tool_call_ids))
            harness.metrics.add("micro_compact_tokens_removed", compacted.tokens_removed)
            harness.metrics.add(
                "raw_tool_tokens_after_compact",
                context_token_estimate(compacted.messages),
                quality="estimated",
            )
            compacted_messages = await self._maybe_rolling_compact(harness, compacted.messages)
            return HarnessHookResult(
                messages=compacted_messages,
                budget_usage=merge_budget_usage(
                    extraction.budget_usage,
                    context_result.budget_usage if combined_deltas else {},
                ),
                metrics=harness.metrics.as_dict(),
            )
        except Exception:
            # Preserve the raw messages when extraction, writing, or context
            # update fails.  A receipt is only issued after the entire pipeline
            # succeeds, so retry/debug tooling still has the original material.
            LOGGER.exception("Research Graph producer hook failed for role %s.", harness.role)
            if harness.transcript is not None:
                harness.transcript.append("graph_hook_failure", {"role": harness.role})
            return HarnessHookResult(
                messages=list(messages),
                succeeded=False,
                metrics=harness.metrics.as_dict(),
            )


class ResearchGraphConsumerStrategy(_ResearchGraphStrategyBase):
    """Consumer lifecycle: scoped graph retrieval plus bounded analysis."""

    name = "research_graph_consumer"
    is_producer = False

    async def after_tool_batch(
        self,
        harness: ResearchContextHarness,
        messages: list[Any],
        batch: list[ToolBatchItem],
    ) -> HarnessHookResult:
        """Refresh scoped graph retrieval while preserving consumer raw results."""
        if harness.transcript is not None and batch:
            harness.transcript.append(
                "consumer_tool_batch",
                {
                    "role": harness.role,
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
        if harness.store is not None:
            harness.refresh_retrieval(query_suffix="consumer analysis")
        # Consumer tool outputs are not source evidence by default.  Keep them
        # raw so a future consumer-specific evidence policy cannot lose data.
        # Under context pressure, incremental Rolling Compact can still replace
        # complete older steps; the transcript retains the original payload.
        bounded_messages = await self._maybe_rolling_compact(harness, list(messages))
        return HarnessHookResult(
            messages=bounded_messages,
            metrics=harness.metrics.as_dict(),
        )


def create_context_strategy(
    strategy_name: str,
    *,
    graph_enabled: bool,
) -> ContextStrategy:
    """Resolve the strategy once at harness initialization."""
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
    "HarnessFinalResult",
    "HarnessHookResult",
    "ResearchContextHarness",
    "ResearchGraphConsumerStrategy",
    "ResearchGraphProducerStrategy",
    "StandardContextStrategy",
    "TaskDescriptor",
    "ToolBatchItem",
    "create_context_strategy",
]
