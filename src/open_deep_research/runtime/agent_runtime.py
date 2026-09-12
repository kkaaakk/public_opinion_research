"""Shared ReAct runtime used by all four public-opinion AgentSpecs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from open_deep_research.budget import (
    budget_from_model_response,
    budget_from_native_search,
    budget_from_tool_calls,
    budget_usage_with_reason,
    can_spend_model_call,
    estimate_tokens,
    filter_tool_calls_for_budget,
    is_over_budget,
    merge_budget_usage,
)
from open_deep_research.observability import observe_model_ainvoke
from open_deep_research.research_graph import ResearchWorkspace, ToolBatchItem
from open_deep_research.research_graph.compaction import (
    context_token_estimate,
    rolling_compact,
    should_rolling_compact,
)
from open_deep_research.utils import (
    anthropic_websearch_called,
    get_model_token_limit,
    openai_websearch_called,
)

ToolExecutor = Callable[..., Awaitable[tuple[str, dict[str, Any], bool]]]
CompressionCallback = Callable[
    [list[Any], str, str, dict[str, Any]],
    Awaitable[tuple[str, list[str], dict[str, Any]]],
]


@dataclass
class AgentRuntimeResult:
    """Serializable output produced by one AgentRuntime invocation."""

    report: str
    memory: dict[str, Any]
    rolling_summary: str
    budget: dict[str, Any] = field(default_factory=dict)
    replace_report: bool = False


class AgentRuntime:
    """Own ReAct history, model/tool lifecycle, compaction, and role output."""

    def __init__(
        self,
        *,
        role: str,
        spec: Any,
        agent_state: Mapping[str, Any],
        workspace: ResearchWorkspace,
        config: Any,
        runtime_config: Any,
        model: Any,
        system_prompt: str,
        tools: list[Any],
        initial_budget: Mapping[str, Any],
        execute_tool: ToolExecutor,
        compress_report: CompressionCallback,
    ) -> None:
        """Bind one AgentSpec to its model, tools, and ResearchWorkspace."""
        self.role = role
        self.spec = spec
        self.agent_state = dict(agent_state or {})
        self.workspace = workspace
        self.config = config
        self.runtime_config = runtime_config
        self.model = model
        self.system_prompt = system_prompt
        self.tools = tools
        self.initial_budget = dict(initial_budget or {})
        self.execute_tool = execute_tool
        self.compress_report = compress_report
        # Rolling Summary is agent-owned; Workspace only receives the current
        # value while assembling graph context and returns any compacted value.
        self.rolling_summary = str(self.agent_state.get("rolling_summary") or "")
        self.workspace.rolling_summary = self.rolling_summary

    @staticmethod
    def _tool_name(tool: Any) -> str:
        return str(getattr(tool, "name", None) or getattr(tool, "__name__", ""))

    @staticmethod
    def _skipped_tool_message(tool_call: dict[str, Any], reason: str) -> ToolMessage:
        return ToolMessage(
            content=f"Budget guard skipped `{tool_call['name']}`: {reason}",
            name=tool_call["name"],
            tool_call_id=tool_call["id"],
        )

    @staticmethod
    def format_private_memory(agent_state: Mapping[str, Any]) -> str:
        """Render only this role's private memory for dynamic model context."""
        memories = list(agent_state.get("memory", []) or [])[-5:]
        if not memories:
            return "No private memory has been recorded for this agent yet."
        entries = []
        for index, memory in enumerate(memories, start=1):
            if isinstance(memory, Mapping):
                title = str(memory.get("title") or f"Memory {index}")
                content = str(memory.get("content") or "")
                source = str(memory.get("source") or "agent_private_memory")
                entries.append(f"{index}. {title} [{source}]\n{content}")
            else:
                entries.append(f"{index}. {memory}")
        return "\n\n".join(entries)

    @staticmethod
    def build_private_memory(role: str, report: str, raw_notes: list[str]) -> dict[str, Any]:
        """Create the bounded private-memory record owned by one role."""
        content = str(report or "").strip()
        if len(content) > 1800:
            content = content[:1800].rstrip() + "\n[truncated]"
        return {
            "title": f"{role} completed role report",
            "content": content or "No report content was produced.",
            "source": "current_public_opinion_run",
            "raw_note_count": len(raw_notes or []),
        }

    async def _compact_history(
        self, messages: list[Any]
    ) -> tuple[list[Any], dict[str, Any]]:
        """Compress ReAct history while preserving Workspace-derived context."""
        model_limit = getattr(
            self.config, "research_graph_context_capacity_tokens", None
        ) or get_model_token_limit(self.config.research_model)
        if model_limit is None:
            return messages, {}
        protected = self.workspace.protected_context()
        if not should_rolling_compact(
            messages,
            extra_context=protected,
            model_context_capacity=model_limit,
            threshold_ratio=float(
                getattr(self.config, "context_compaction_threshold_ratio", 0.75)
            ),
        ):
            return messages, {}
        model_name = str(
            getattr(self.config, "rolling_compaction_model", None)
            or getattr(self.config, "compression_model", "")
        )
        before_tokens = context_token_estimate(messages, protected)
        result = await rolling_compact(
            messages,
            previous_summary=self.rolling_summary,
            protected_context=protected,
            model=self.workspace.model_factory(
                model_name,
                int(getattr(self.config, "rolling_compaction_model_max_tokens", 2048)),
            ),
            model_name=model_name,
            max_retries=int(getattr(self.config, "max_structured_output_retries", 3)),
            recent_raw_steps=int(getattr(self.config, "recent_raw_steps", 3)),
        )
        self.rolling_summary = result.rolling_summary
        self.workspace.rolling_summary = self.rolling_summary
        metrics = self.workspace.metrics
        metrics.add("rolling_compact_count")
        metrics.add("rolling_compact_input_tokens", before_tokens, quality="estimated")
        output_tokens = result.budget_usage.get("output_tokens")
        metrics.add(
            "rolling_compact_output_tokens",
            output_tokens if isinstance(output_tokens, int) and output_tokens > 0
            else estimate_tokens(result.rolling_summary),
            quality="exact" if isinstance(output_tokens, int) and output_tokens > 0
            else "estimated",
        )
        metrics.add("micro_compact_tokens_removed", result.tokens_removed)
        if self.workspace.transcript is not None:
            self.workspace.transcript.append(
                "rolling_compact",
                {
                    "tokens_removed": result.tokens_removed,
                    "rolling_summary": result.rolling_summary,
                },
            )
        return result.messages, result.budget_usage

    async def run(self, task: str) -> AgentRuntimeResult:
        """Execute the shared ReAct lifecycle for this role assignment."""
        messages: list[Any] = [HumanMessage(content=task)]
        private_memory = self.format_private_memory(self.agent_state)
        if private_memory != "No private memory has been recorded for this agent yet.":
            messages.append(
                HumanMessage(
                    content=(
                        "<Agent Dynamic Context>\n"
                        "Private Agent Memory:\n"
                        f"{private_memory}\n"
                        "</Agent Dynamic Context>"
                    )
                )
            )
        tools_by_name = {self._tool_name(tool): tool for tool in self.tools}
        budget_update: dict[str, Any] = {}
        research_round = self.workspace.scope.research_round
        mode = "followup" if research_round > 1 else "initial"

        for step in range(self.config.max_react_tool_calls):
            projected = merge_budget_usage(self.initial_budget, budget_update)
            if not can_spend_model_call(
                self.config, projected, reserve_final_report_call=True
            ):
                budget_update = merge_budget_usage(
                    budget_update,
                    budget_usage_with_reason(
                        f"Stopped {self.role} agent reasoning to preserve the final report model call."
                    ),
                )
                break

            model_messages = [
                SystemMessage(content=self.system_prompt),
                *await self.workspace.before_model(messages),
            ]
            response = await observe_model_ainvoke(
                self.model,
                model_messages,
                observer_model=self.config.research_model,
                observer_component=f"{self.role}_{mode}_round_{research_round}",
            )
            messages.append(response)
            response_budget = budget_from_model_response(response)
            native_search = openai_websearch_called(response) or anthropic_websearch_called(response)
            if native_search:
                response_budget = merge_budget_usage(
                    response_budget, budget_from_native_search()
                )
            budget_update = merge_budget_usage(budget_update, response_budget)

            tool_calls = list(getattr(response, "tool_calls", []) or [])
            if not tool_calls and not native_search:
                break
            if is_over_budget(
                self.config, merge_budget_usage(self.initial_budget, budget_update)
            ):
                budget_update = merge_budget_usage(
                    budget_update,
                    budget_usage_with_reason(
                        f"Stopped {self.role} agent because a budget was reached."
                    ),
                )
                break

            allowed, skipped = filter_tool_calls_for_budget(
                self.config,
                merge_budget_usage(self.initial_budget, budget_update),
                tool_calls,
                tools_by_name,
            )
            batch: list[ToolBatchItem] = []
            if skipped:
                budget_update = merge_budget_usage(
                    budget_update,
                    budget_usage_with_reason(
                        f"Skipped {self.role} agent tools because a tool or search budget was exhausted."
                    ),
                )

            known = [call for call in allowed if call["name"] in tools_by_name]
            unknown = [call for call in allowed if call["name"] not in tools_by_name]
            for call in unknown:
                observation = (
                    f"Tool '{call['name']}' is not available in the current configuration. "
                    "Use one of the available tools instead."
                )
                messages.append(
                    ToolMessage(
                        content=observation,
                        name=call["name"],
                        tool_call_id=call["id"],
                    )
                )
                batch.append(
                    ToolBatchItem(call["name"], call["id"], call.get("args", {}), observation, False)
                )

            if known:
                results = await asyncio.gather(
                    *[
                        self.execute_tool(
                            tools_by_name[call["name"]],
                            call["args"],
                            self.runtime_config,
                            tool_call_id=call["id"],
                        )
                        for call in known
                    ]
                )
                budget_update = merge_budget_usage(
                    budget_update, budget_from_tool_calls(known, tools_by_name)
                )
                for (observation, captured, success), call in zip(results, known):
                    budget_update = merge_budget_usage(budget_update, captured)
                    messages.append(
                        ToolMessage(
                            content=observation,
                            name=call["name"],
                            tool_call_id=call["id"],
                        )
                    )
                    batch.append(
                        ToolBatchItem(call["name"], call["id"], call.get("args", {}), observation, success)
                    )

            for call in skipped:
                skipped_message = self._skipped_tool_message(
                    call, "tool or search call budget exhausted"
                )
                messages.append(skipped_message)
                batch.append(
                    ToolBatchItem(
                        call["name"], call["id"], call.get("args", {}), skipped_message.content, False
                    )
                )
            if native_search:
                batch.append(
                    ToolBatchItem(
                        "web_search",
                        f"native-search-{research_round}-{step}",
                        {},
                        getattr(response, "content", ""),
                        True,
                    )
                )

            hook = await self.workspace.ingest(messages, batch)
            messages, compaction_budget = await self._compact_history(hook.messages)
            budget_update = merge_budget_usage(budget_update, hook.budget_usage)
            budget_update = merge_budget_usage(budget_update, compaction_budget)
            if any(call["name"] == "ResearchComplete" for call in tool_calls):
                break
            if not allowed:
                break

        graph_final = await self.workspace.finalize(messages, self.spec.expected_output)
        if graph_final is not None:
            budget_update = merge_budget_usage(budget_update, graph_final.budget_usage)
            body = graph_final.report
            raw_notes = graph_final.raw_notes
            replace_report = True
            strategy_line = f"Context strategy: {self.workspace.strategy.name}\n"
        else:
            body, raw_notes, compression_budget = await self.compress_report(
                messages,
                task,
                self.spec.expected_output,
                merge_budget_usage(self.initial_budget, budget_update),
            )
            budget_update = merge_budget_usage(budget_update, compression_budget)
            replace_report = False
            strategy_line = ""

        report = (
            f"Agent role: {self.role}\n"
            f"Agent name: {self.spec.display_name}\n"
            f"Research round: {research_round}\n"
            f"Research mode: {mode}\n"
            f"{strategy_line}"
            f"Expected output: {self.spec.expected_output}\n\n{body}"
        )
        return AgentRuntimeResult(
            report=report,
            memory=self.build_private_memory(self.role, report, raw_notes),
            rolling_summary=self.workspace.rolling_summary,
            budget=budget_update,
            replace_report=replace_report,
        )
