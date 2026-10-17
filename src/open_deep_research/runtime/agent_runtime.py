# 执行共享 ReAct 循环，管理消息、模型工具调用、压缩和角色私有记忆。
"""Shared ReAct runtime used by all four public-opinion AgentSpecs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from open_deep_research.budget import (
    ainvoke_model_with_budget,
    budget_from_native_search,
    budget_from_tool_calls,
    budget_usage_with_reason,
    can_spend_model_call,
    context_pressure_ratio,
    estimate_context_tokens,
    estimate_text_tokens,
    filter_tool_calls_for_budget,
    is_over_budget,
    merge_budget_usage,
)
from open_deep_research.llm.context import get_model_token_limit
from open_deep_research.llm.native_search import (
    anthropic_websearch_called,
    openai_websearch_called,
)
from open_deep_research.research_graph import ResearchWorkspace, ToolBatchItem
from open_deep_research.research_graph.compaction import rolling_compact

LOGGER = logging.getLogger(__name__)

ToolExecutor = Callable[..., Awaitable[tuple[str, dict[str, Any], bool]]]
CompressionCallback = Callable[
    [list[Any], str, str, dict[str, Any]],
    Awaitable[tuple[str, list[str], dict[str, Any]]],
]


# 保存一次角色执行的可序列化输出及资源消耗。
@dataclass
class AgentRuntimeResult:
    """Serializable output produced by one AgentRuntime invocation."""

    report: str
    memory: dict[str, Any]
    rolling_summary: str
    budget: dict[str, Any] = field(default_factory=dict)
    replace_report: bool = False


# 统一管理智能体生命周期，将模型推理、工具执行和研究工作区衔接起来。
class AgentRuntime:
    """Own ReAct history, model/tool lifecycle, compaction, and role output."""

    # 绑定角色规范、模型工厂、工具和工作区，以及运行所需的回调。
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

    # 兼容工具对象和原生工具声明，提取稳定工具名。
    @staticmethod
    def _tool_name(tool: Any) -> str:
        return str(getattr(tool, "name", None) or getattr(tool, "__name__", ""))

    # 为预算或策略跳过的工具调用生成配对结果，保持消息链完整。
    @staticmethod
    def _skipped_tool_message(tool_call: dict[str, Any], reason: str) -> ToolMessage:
        return ToolMessage(
            content=f"Budget guard skipped `{tool_call['name']}`: {reason}",
            name=tool_call["name"],
            tool_call_id=tool_call["id"],
        )

    # 只渲染当前角色私有记忆，用于动态模型上下文。
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

    # 生成受大小限制的角色私有记忆记录。
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

    # 以实际下一次请求的消息与工具结构估算压力，必要时压缩 ReAct 历史。
    async def _compact_history(
        self, messages: list[Any], model_messages: list[Any]
    ) -> tuple[list[Any], dict[str, Any]]:
        """Compact raw ReAct history when the real next request is over budget.

        ``model_messages`` is the exact message list the next ChatModel call would
        send (SystemMessage + dynamic context + tools), so the pressure estimate
        and the request share the same structure.
        """
        model_limit = getattr(
            self.config, "research_graph_context_capacity_tokens", None
        ) or get_model_token_limit(self.config.research_model)
        if model_limit is None:
            return messages, {}
        warning_ratio = float(getattr(self.config, "context_warning_ratio", 0.6))
        compaction_ratio = float(
            getattr(self.config, "context_compaction_threshold_ratio", 0.75)
        )
        estimated_tokens = estimate_context_tokens(model_messages, tools=self.tools)
        pressure = context_pressure_ratio(estimated_tokens, model_limit)
        if pressure >= warning_ratio:
            LOGGER.warning(
                "Agent %s context pressure %.2f (estimated=%d window=%d).",
                self.role,
                pressure,
                estimated_tokens,
                model_limit,
            )
        if pressure < compaction_ratio:
            return messages, {}
        model_name = str(
            getattr(self.config, "rolling_compaction_model", None)
            or getattr(self.config, "compression_model", "")
        )
        protected = self.workspace.protected_context()
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
        metrics.add("rolling_compact_input_tokens", estimated_tokens, quality="estimated")
        output_tokens = result.budget_usage.get("output_tokens")
        metrics.add(
            "rolling_compact_output_tokens",
            output_tokens if isinstance(output_tokens, int) and output_tokens > 0
            else estimate_text_tokens(result.rolling_summary),
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

    # 执行角色的模型推理与工具循环，处理预算、上下文压缩及最终角色输出。
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
            # 用“已有消耗 + 本角色增量”判断额度，避免把累计值再次提交给 reducer。
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
            messages, compaction_budget = await self._compact_history(
                messages, model_messages
            )
            if compaction_budget:
                budget_update = merge_budget_usage(budget_update, compaction_budget)
                # 压缩已改变历史，重新构建实际请求，使估算结构与发送结构保持一致。
                model_messages = [
                    SystemMessage(content=self.system_prompt),
                    *await self.workspace.before_model(messages),
                ]
            response, response_budget = await ainvoke_model_with_budget(
                self.model,
                model_messages,
                model_name=self.config.research_model,
                component=f"{self.role}_{mode}_round_{research_round}",
            )
            messages.append(response)
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
            # 未注册工具也要返回配对 ToolMessage，避免下一次请求的工具调用链不完整。
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
                # 同一轮工具并发执行；返回后逐项合并其内部模型请求捕获的消耗。
                results = await asyncio.gather(
                    *[
                        self.execute_tool(
                            tools_by_name[call["name"]],
                            call["args"],
                            self.runtime_config,
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

            # 图生产策略在此抽取并持久化证据，标准策略则保留原工具消息。
            hook = await self.workspace.ingest(messages, batch)
            messages = hook.messages
            budget_update = merge_budget_usage(budget_update, hook.budget_usage)
            if any(call["name"] == "ResearchComplete" for call in tool_calls):
                break
            if not allowed:
                break

        return await self._finish(task, messages, budget_update)

    async def _finish(
        self, task: str, messages: list[Any], budget_update: dict[str, Any]
    ) -> AgentRuntimeResult:
        """Preserve the shared report and private-memory contract for both runtimes."""
        research_round = self.workspace.scope.research_round
        mode = "followup" if research_round > 1 else "initial"
        # 图模式直接用有界证据生成报告；标准模式使用研究历史压缩入口收尾。
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


# 提供私有记忆格式化的兼容入口，避免调用方依赖运行时类。
def format_private_memory(agent_state: Mapping[str, Any]) -> str:
    """Format private agent memory without exposing AgentRuntime to callers."""
    return AgentRuntime.format_private_memory(agent_state)


__all__ = ["AgentRuntime", "AgentRuntimeResult", "format_private_memory"]
