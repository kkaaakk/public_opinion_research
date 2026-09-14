"""Per-run Research Graph workspace and role-scoped Working Context."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from open_deep_research.research_graph.compaction import render_protected_context
from open_deep_research.research_graph.context_manager import (
    initial_working_context,
    render_working_context,
)
from open_deep_research.research_graph.metrics import ResearchGraphMetrics
from open_deep_research.research_graph.models import (
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


class ResearchWorkspace:
    """Own Graph storage, retrieval, provenance, and Working Context for a role."""

    def __init__(
        self,
        *,
        strategy: Any,
        research_state: dict[str, Any],
        role: str,
        configurable: Any,
        model_factory: Callable[[str, int | None], Any],
        store: ResearchGraphStore | None = None,
        task: TaskDescriptor | None = None,
        run_id: str | None = None,
        research_round: int = 1,
        driver_factory: Callable[..., Any] | None = None,
        store_factory: Callable[..., ResearchGraphStore] | None = None,
    ) -> None:
        """Build runtime graph resources from serializable state and config."""
        self.strategy = strategy
        self.research_state = research_state
        self.role = role
        self.configurable = configurable
        self.model_factory = model_factory
        self.run_id = run_id or stable_id("RUN", role)
        self.task = task or TaskDescriptor(
            task_id=stable_id("TASK", self.run_id, role),
            objective="Research evidence relevant to the role contract.",
            evidence_needed="Evidence relevant to the role objective.",
        )
        self.scope = ResearchGraphScope(
            run_id=self.run_id,
            role=role,
            research_round=max(1, int(research_round or 1)),
            task_id=self.task.task_id,
        )
        self.store = store
        if strategy.graph_enabled and self.store is None:
            self.store = create_research_graph_store(
                configurable,
                run_id=self.run_id,
                driver_factory=driver_factory,
                store_factory=store_factory,
            )
        self.retriever = (
            ResearchGraphRetriever(
                self.store,
                max_nodes=int(getattr(configurable, "research_graph_max_retrieved_nodes", 24)),
                max_edges=int(getattr(configurable, "research_graph_max_retrieved_edges", 48)),
            )
            if self.store is not None
            else None
        )
        self.working_context = self._load_working_context()
        self.relevant_subgraph = RelevantSubgraph(run_id=self.run_id)
        self.rolling_summary = ""
        self.receipts: dict[str, WriteReceipt] = {}
        self.metrics = ResearchGraphMetrics()
        self.transcript = (
            ResearchTranscript(
                getattr(configurable, "research_graph_transcript_dir", None), self.run_id
            )
            if strategy.graph_enabled
            else None
        )

    def _load_working_context(self) -> WorkingContext:
        values = self.research_state.get("working_contexts", {}) or {}
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
        """Ask the configured context strategy for dynamic model context."""
        return await self.strategy.before_model(self, messages)

    async def ingest(self, messages: list[Any], batch: list[ToolBatchItem]) -> Any:
        """Ingest a tool batch through the producer/consumer context strategy."""
        return await self.strategy.after_tool_batch(self, messages, batch)

    async def finalize(self, messages: list[Any], expected_output: str) -> Any:
        """Produce a graph-grounded role report when graph mode owns it."""
        return await self.strategy.finalize(self, messages, expected_output)

    def protected_context(self) -> str:
        """Render context that AgentRuntime must preserve during compaction."""
        return render_protected_context(
            current_task=self.task.objective,
            working_context=render_working_context(self.working_context),
            relevant_subgraph=format_relevant_subgraph(self.relevant_subgraph),
            rolling_summary=self.rolling_summary,
        )

    def context_for(self, *, query_suffix: str = "") -> RelevantSubgraph:
        """Retrieve a scoped, recomputable RelevantSubgraph for the current task."""
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

    @staticmethod
    def retrieve(
        configurable: Any,
        *,
        run_id: str,
        query: str,
    ) -> RelevantSubgraph:
        """Retrieve scoped graph context for review/report consumers."""
        store = create_research_graph_store(configurable, run_id=run_id)
        return store.retrieve(
            query,
            run_id=run_id,
            max_nodes=configurable.research_graph_max_retrieved_nodes,
            max_edges=configurable.research_graph_max_retrieved_edges,
        )

    def state_update(self) -> dict[str, Any]:
        """Return only serializable Research and Runtime domain updates."""
        return {
            "research": {
                "run_id": self.run_id,
                "working_contexts": {
                    self.role: self.working_context.model_dump(mode="json")
                },
            },
            "runtime": {"metrics": self.metrics.as_dict()},
        }


def retrieve_research_context(
    configurable: Any,
    *,
    run_id: str,
    query: str,
) -> RelevantSubgraph:
    """Retrieve scoped graph context without exposing workspace construction."""
    return ResearchWorkspace.retrieve(configurable, run_id=run_id, query=query)


__all__ = [
    "ResearchWorkspace",
    "TaskDescriptor",
    "ToolBatchItem",
    "retrieve_research_context",
]
