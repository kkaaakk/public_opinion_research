"""Execute business AgentSpecs with the official SDK, retaining business results.

Only Runner owns the model/tool loop. The hooks add business admission and SDK
usage to the Phase 0 collector; report services and LangGraph stay in place.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agents import Agent, Model, ModelSettings, RunConfig
from agents.exceptions import MaxTurnsExceeded, MCPToolCancellationError
from agents.retry import ModelRetrySettings
from agents.tool_context import ToolContext
from jsonschema.validators import validator_for
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool

from open_deep_research.budget import (
    budget_capture,
    budget_from_sdk_usage,
    budget_from_tool_calls,
    budget_usage_with_reason,
    can_spend_model_call,
    can_spend_tool_call,
    capture_budget_usage,
    context_pressure_ratio,
    diff_budget_usage,
    estimate_text_tokens,
    is_over_budget,
    merge_budget_usage,
)
from open_deep_research.llm.context import get_model_token_limit
from open_deep_research.mcp.domain_filter import get_tool_domain
from open_deep_research.runtime.agent_runtime import AgentRuntime, AgentRuntimeResult
from open_deep_research.runtime.sdk_context import SDKContextHarness


class SDKContextLimitExceeded(RuntimeError):
    """Reject oversized requests until Phase 2 supports SDK rolling compaction."""


class _ReasoningStopped(Exception):
    """Stop Runner at a public hook, then finish the existing business report."""


class _ToolRejected(Exception):
    """Return a paired SDK tool error without invoking the business tool."""


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(
        isinstance(block, dict) and block.get("type") == "text" for block in value
    ):
        return "\n".join(str(block.get("text", "")) for block in value)
    return json.dumps(value, ensure_ascii=False, default=str)


class _BusinessRunHooks(SDKContextHarness):
    """Add role budgets to the existing collector through public SDK hooks."""

    def __init__(self, runtime: SDKAgentRuntime) -> None:
        # Match the existing Producer's source admission: every successfully
        # executed role tool except the two research control tools. Phase 0's
        # standalone default remains web_search/rag_search.
        super().__init__(
            runtime.workspace,
            evidence_tools=frozenset(
                tool.name
                for tool in runtime.tools
                if isinstance(tool, BaseTool)
                and tool.name not in {"think_tool", "ResearchComplete"}
            ),
        )
        self.runtime = runtime
        self.items: list[Any] = []

    async def filter_model_input(self, data: Any) -> Any:
        self.items = list(data.model_data.input)
        filtered = await super().filter_model_input(data)
        runtime = self.runtime
        if not can_spend_model_call(
            runtime.config, runtime.projected_budget(), reserve_final_report_call=True
        ) or is_over_budget(runtime.config, runtime.projected_budget()):
            raise _ReasoningStopped(
                "Business budget stopped SDK reasoning before another request."
            )
        # Use the actual SDK input, instructions and registered schemas. This is
        # admission estimation only; reported tokens always come from SDK usage.
        capacity = (
            runtime.config.research_graph_context_capacity_tokens
            or get_model_token_limit(runtime.config.research_model)
        )
        estimated = estimate_text_tokens(
            json.dumps(
                {
                    "instructions": filtered.instructions,
                    "input": filtered.input,
                    "tools": [tool.params_json_schema for tool in data.agent.tools],
                },
                ensure_ascii=False,
                default=str,
            )
        )
        if (
            context_pressure_ratio(estimated, capacity)
            >= runtime.config.context_compaction_threshold_ratio
        ):
            raise SDKContextLimitExceeded(
                "SDK input requires rolling compaction, which is reserved for Phase 2; "
                "use a smaller task/context or the legacy runtime. No history was discarded."
            )
        return filtered

    async def on_llm_start(
        self, context: Any, agent: Any, system_prompt: Any, input_items: Any
    ) -> None:
        await super().on_llm_start(context, agent, system_prompt, input_items)
        self.runtime.delta = merge_budget_usage(self.runtime.delta, {"model_calls": 1})

    async def on_llm_end(self, context: Any, agent: Any, response: Any) -> None:
        await super().on_llm_end(context, agent, response)
        self.items.extend(response.to_input_items())
        self.runtime.delta = merge_budget_usage(
            self.runtime.delta, budget_from_sdk_usage(response.usage)
        )


class SDKAgentRuntime(AgentRuntime):
    """Map all four AgentSpecs to SDK Agents and run each exactly once.

    Inherit only the existing assembly arguments, report envelope and private
    memory helpers. No legacy model/tool loop is called from this path.
    """

    delta: dict[str, Any]
    nested_usage: dict[str, Any]
    hooks: _BusinessRunHooks

    def projected_budget(self) -> dict[str, Any]:
        """Return cumulative business usage for admission, without submitting it."""
        return merge_budget_usage(
            self.initial_budget, merge_budget_usage(self.delta, self.nested_usage)
        )

    def _register_tool(self, tool: BaseTool) -> Any:
        name = tool.name
        if get_tool_domain(tool) not in self.spec.allowed_domains:
            raise ValueError(f"Tool {name!r} is outside {self.role}'s allowed domains.")
        # tool_call_schema excludes RunnableConfig and injected runtime fields.
        # Keep optional/default fields unchanged instead of making them required.
        schema = tool.tool_call_schema
        schema = schema if isinstance(schema, dict) else schema.model_json_schema()
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)
        validator = validator_class(schema)

        async def invoke(context: ToolContext[Any], arguments: str) -> str:
            # Recheck execution permission rather than relying only on visibility.
            if (
                get_tool_domain(tool) not in self.spec.allowed_domains
                or tool.name != name
            ):
                raise _ToolRejected(
                    f"Tool {name!r} is no longer authorized for {self.role}."
                )
            # A repeated native call ID shares its original result, including an
            # in-flight execution. Different IDs remain separate business calls.
            previous = self._tool_tasks.get(context.tool_call_id)
            if previous is not None:
                return await asyncio.shield(previous)

            async def execute() -> str:
                call = {"name": name, "id": context.tool_call_id}
                increment = budget_from_tool_calls([call], {name: tool})
                if is_over_budget(
                    self.config, self.projected_budget()
                ) or not can_spend_tool_call(
                    self.config, self.projected_budget(), increment
                ):
                    self.delta = merge_budget_usage(
                        self.delta,
                        budget_usage_with_reason(
                            f"Budget guard skipped {name}: tool or search budget exhausted."
                        ),
                    )
                    raise _ToolRejected(
                        f"Budget guard skipped {name}: tool or search budget exhausted."
                    )
                # Reserve before awaiting: SDK parallel calls cannot overbook.
                self.delta = merge_budget_usage(self.delta, increment)
                args = json.loads(arguments)
                validator.validate(args)
                with budget_capture() as captured:
                    try:
                        output = await tool.ainvoke(
                            {
                                "type": "tool_call",
                                "id": context.tool_call_id,
                                "name": name,
                                "args": args,
                            },
                            self.runtime_config,
                        )
                    finally:
                        self.delta = merge_budget_usage(self.delta, captured)
                if isinstance(output, ToolMessage):
                    if output.status == "error":
                        raise _ToolRejected(_text(output.content))
                    artifact = output.artifact
                    structured = (
                        artifact.get("structured_content")
                        if isinstance(artifact, dict)
                        else getattr(artifact, "structured_content", None)
                    )
                    content = output.content
                    if isinstance(content, list) and any(
                        not isinstance(block, dict) or block.get("type") != "text"
                        for block in content
                    ):
                        raise _ToolRejected(
                            "SDK business tools currently support text/structured results; "
                            "multimodal MCP outputs require a verified content adapter."
                        )
                    output = (
                        {"content": content, "structured_content": structured}
                        if structured is not None
                        else content
                    )
                # These are explicit existing business/protocol error envelopes,
                # not guesses based on arbitrary tool text or the SDK end hook.
                envelope = (
                    output.get("structured_content", output)
                    if isinstance(output, dict)
                    else output
                )
                if isinstance(output, str):
                    try:
                        envelope = json.loads(output)
                    except ValueError:
                        pass
                if isinstance(envelope, dict) and envelope.get("status") == "cancelled":
                    raise MCPToolCancellationError(_text(output))
                if isinstance(envelope, dict) and (
                    envelope.get("isError") is True
                    or envelope.get("success") is False
                    or envelope.get("status") in ("failed", "error")
                ):
                    raise _ToolRejected(_text(output))
                if (
                    name == "rag_search"
                    and isinstance(output, str)
                    and output.startswith("Local RAG search failed:")
                ):
                    raise _ToolRejected(output)
                return _text(output)

            pending = asyncio.create_task(execute())
            self._tool_tasks[context.tool_call_id] = pending
            if (
                getattr(tool, "func", None) is not None
                and getattr(tool, "coroutine", None) is None
            ):
                # A running thread cannot be killed by cancelling its awaiter.
                # Keep the collector live so the existing bounded exit drain
                # records its actual result and nested usage when it completes.
                while True:
                    try:
                        return await asyncio.shield(pending)
                    except asyncio.CancelledError:
                        if pending.cancelled():
                            raise
            return await pending

        return self.hooks.function_tool(
            name=name,
            description=tool.description or name,
            params_json_schema=schema,
            invoke=invoke,
            strict_json_schema=False,
            error_formatter=lambda _context, error: f"Error executing tool: {error}",
        )

    def _report_messages(self, task: str) -> list[Any]:
        messages: list[Any] = [HumanMessage(content=task)]
        output_ids: set[str] = set()
        for item in self.hooks.items:
            kind = item.get("type")
            if kind == "function_call":
                try:
                    arguments = json.loads(item["arguments"])
                except ValueError:
                    arguments = None
                if not isinstance(arguments, dict):
                    messages.append(
                        AIMessage(
                            content=(
                                f"Invalid arguments for {item['name']}: {item['arguments']}"
                            )
                        )
                    )
                    continue
                messages.append(
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": item["name"],
                                "id": item["call_id"],
                                "args": arguments,
                            }
                        ],
                    )
                )
            elif kind == "function_call_output":
                output_ids.add(item["call_id"])
                messages.append(
                    ToolMessage(
                        content=_text(item["output"]), tool_call_id=item["call_id"]
                    )
                )
            elif item.get("role") == "assistant":
                content = item.get("content", [])
                text = (
                    content
                    if isinstance(content, str)
                    else "\n".join(
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict)
                    )
                )
                messages.append(AIMessage(content=text))
        # MaxTurnsExceeded can occur before the next input filter, after tools.
        for result in self.hooks.results.values():
            if result.tool_call_id not in output_ids and result.status != "running":
                messages.append(
                    ToolMessage(
                        content=_text(result.observation),
                        tool_call_id=result.tool_call_id,
                        name=result.tool_name,
                    )
                )
        return messages

    async def run(self, task: str) -> AgentRuntimeResult:
        """Run the SDK loop, settle tools, and preserve the role result contract."""
        if not isinstance(self.model, Model):
            raise TypeError(
                "SDKAgentRuntime requires an official SDK Model, not a LangChain model."
            )
        self.delta = {}
        self.nested_usage = {}
        self._tool_tasks: dict[str, asyncio.Task[str]] = {}
        self.hooks = _BusinessRunHooks(self)
        registered = []
        names: set[str] = set()
        for tool in self.tools:
            if not isinstance(tool, BaseTool):
                raise TypeError(
                    "SDK business runtime requires callable BaseTools; hosted/native search is not verified."
                )
            if tool.name in names:
                raise ValueError(f"Duplicate business tool name: {tool.name}")
            names.add(tool.name)
            registered.append(self._register_tool(tool))
        self.agent = Agent(
            name=self.spec.display_name,
            instructions=self.system_prompt,
            model=self.model,
            tools=registered,
            output_type=None,
            model_settings=ModelSettings(
                max_tokens=self.config.research_model_max_tokens,
                retry=ModelRetrySettings(max_retries=0),
            ),
            tool_use_behavior={"stop_at_tool_names": ["ResearchComplete"]},
        )
        private_memory = self.format_private_memory(self.agent_state)
        inputs = [{"role": "user", "content": task}]
        if self.agent_state.get("memory") or self.rolling_summary:
            inputs.append(
                {
                    "role": "user",
                    "content": (
                        "<Agent Dynamic Context>\nPrivate Agent Memory:\n"
                        + private_memory
                        + "\nRolling Summary:\n"
                        + self.rolling_summary
                        + "\n</Agent Dynamic Context>"
                    ),
                }
            )
        self.sdk_result = None
        try:
            # Graph services and report synthesis retain their existing model
            # adapters. Capture their actual attempts once, including failures.
            with budget_capture() as nested:
                self.nested_usage = nested
                try:
                    self.sdk_result = await self.hooks.run(
                        self.agent,
                        inputs,
                        run_config=RunConfig(
                            tracing_disabled=True, trace_include_sensitive_data=False
                        ),
                        max_turns=max(1, self.config.max_react_tool_calls),
                    )
                except (_ReasoningStopped, MaxTurnsExceeded) as stopped:
                    if (
                        self.hooks.last_settlement is None
                        or not self.hooks.last_settlement.succeeded
                    ):
                        raise
                    self.delta = merge_budget_usage(
                        self.delta, budget_usage_with_reason(str(stopped))
                    )
                messages = self._report_messages(task)
                # The old graph finalizer has no budget admission of its own.
                # Explicitly preserve the final workflow report reserve here.
                if self.workspace.strategy.graph_enabled and not can_spend_model_call(
                    self.config, self.projected_budget(), reserve_final_report_call=True
                ):
                    self.delta = merge_budget_usage(
                        self.delta,
                        budget_usage_with_reason(
                            "Skipped graph role report synthesis to preserve the final report budget; retained SDK notes."
                        ),
                    )
                    body = "\n".join(str(message.content) for message in messages[1:])
                    report = (
                        f"Agent role: {self.role}\nAgent name: {self.spec.display_name}\n"
                        f"Research round: {self.workspace.scope.research_round}\n"
                        f"Research mode: {'followup' if self.workspace.scope.research_round > 1 else 'initial'}\n"
                        f"Context strategy: {self.workspace.strategy.name}\n"
                        f"Expected output: {self.spec.expected_output}\n\n"
                        "Budget guard retained research notes without role report synthesis.\n"
                        + body
                    )
                    result = AgentRuntimeResult(
                        report=report,
                        memory=self.build_private_memory(self.role, report, [body]),
                        rolling_summary=self.workspace.rolling_summary,
                        replace_report=True,
                    )
                else:
                    before_report = merge_budget_usage(self.delta, nested)
                    with budget_capture() as report_usage:
                        try:
                            result = await self._finish(task, messages, before_report)
                        except BaseException as exc:
                            self.delta = merge_budget_usage(
                                self.delta, getattr(exc, "budget_usage", report_usage)
                            )
                            raise
                    self.delta = merge_budget_usage(
                        self.delta, diff_budget_usage(result.budget, before_report)
                    )
            # _finish returns the same costs for legacy callers. Use captured
            # nested usage here instead, so none of those costs are merged twice.
            self.budget_usage = merge_budget_usage(self.delta, nested)
            result.budget = self.budget_usage
            return result
        except BaseException as exc:
            self.budget_usage = merge_budget_usage(self.delta, self.nested_usage)
            setattr(exc, "budget_usage", self.budget_usage)
            setattr(exc, "context_settlement", self.hooks.last_settlement)
            raise
        finally:
            # Match legacy capture semantics on success and exceptional exits.
            capture_budget_usage(self.budget_usage)


__all__ = ["SDKAgentRuntime", "SDKContextLimitExceeded"]
