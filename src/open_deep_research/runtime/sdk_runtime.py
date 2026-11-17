"""Execute business AgentSpecs with the official SDK, retaining business results.

Only Runner owns the model/tool loop. The harness adds business admission and SDK
usage to the collector; DBOS owns durable business orchestration.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from agents import Agent, FunctionTool, Model, ModelSettings, RunConfig
from agents.exceptions import MaxTurnsExceeded
from agents.retry import ModelRetrySettings
from agents.run import ModelInputData
from dbos import DBOS
from dbos import error as dbos_error
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from sqlalchemy.exc import SQLAlchemyError

from open_deep_research.budget import (
    ModelBudgetExceeded,
    begin_model_call,
    budget_capture,
    budget_from_sdk_usage,
    budget_usage_with_reason,
    can_spend_model_call,
    capture_budget_usage,
    context_pressure_ratio,
    diff_budget_usage,
    end_model_call,
    estimate_text_tokens,
    is_over_budget,
    merge_budget_usage,
    model_budget_admission,
    physical_model_usage,
)
from open_deep_research.configuration import Configuration
from open_deep_research.llm.context import get_model_token_limit
from open_deep_research.llm.errors import ModelInvocationError, is_token_limit_exceeded
from open_deep_research.mcp.domain_filter import get_tool_domain
from open_deep_research.mcp.tool_wrapper import mcp_function_tool
from open_deep_research.observability.telemetry import (
    annotate,
    business_span,
    telemetry_enabled,
)
from open_deep_research.research_graph.compaction import summarize_compactable_history
from open_deep_research.research_graph.workspace import ResearchWorkspace
from open_deep_research.runtime.sdk_context import SDKContextHarness, tool_usage
from open_deep_research.runtime.tool_executor import ToolExecutor, format_tool_error
from open_deep_research.tools import BusinessTool, tool_result_text
from open_deep_research.workflow.dbos_config import native_client, settings
from open_deep_research.workflow.execution import (
    ExecutionControlError,
    owned_io,
    stage_context,
)

SDK_RUNTIME_VERSION = "sdk-phase5-v1"

CompressionCallback = Callable[
    [list[Any], str, str, dict[str, Any]],
    Awaitable[tuple[str, list[str], dict[str, Any]]],
]


@dataclass
class AgentRuntimeResult:
    """Serializable business result of one SDK role invocation."""

    report: str
    memory: dict[str, Any]
    rolling_summary: str
    budget: dict[str, Any] = field(default_factory=dict)
    replace_report: bool = False


class SDKContextLimitExceeded(RuntimeError):
    """Reject requests whose protected history cannot fit safely after compaction."""


class _ReasoningStopped(Exception):
    """Stop Runner at a public hook, then finish the existing business report."""


class MeteredModel(Model):
    """Write physical receipts inside the official DBOSRunner model Step."""

    def __init__(self, model):
        self.model = model
        self.model_name = getattr(model, "model_name", type(model).__name__)

    async def get_response(self, *args, **kwargs):
        try:
            identity = await begin_model_call()
        except ModelBudgetExceeded as exc:
            raise _ReasoningStopped(str(exc)) from exc
        except SQLAlchemyError as exc:
            raise ExecutionControlError("Model admission storage unavailable") from exc
        try:
            response = await self._response(identity, *args, **kwargs)
        except BaseException as exc:
            usage = {"model_calls": 1}
            await end_model_call(identity, physical_model_usage(usage, failed=True))
            if isinstance(exc, Exception) and not isinstance(
                exc,
                (
                    dbos_error.DBOSWorkflowCancelledError,
                    ExecutionControlError,
                    ModelBudgetExceeded,
                ),
            ):
                raise ModelInvocationError(
                    type(exc).__name__,
                    context_limit=is_token_limit_exceeded(exc, self.model_name),
                    budget_usage=usage,
                ) from exc
            raise
        usage = budget_from_sdk_usage(response.usage)
        usage["model_calls"] = 1
        await end_model_call(identity, physical_model_usage(usage))
        return response

    async def _response(self, identity, *args, **kwargs):
        if identity is None:
            return await self.model.get_response(*args, **kwargs)

        async def wait_for_native_stop():
            client = native_client()
            handle = await client.retrieve_workflow_async(DBOS.workflow_id)
            await client.wait_first_async([handle])

        # The official runner's model Step is not preemptible in 0.3.0.
        # Keep its checkpoint and use DBOS's terminal wait for provider cancellation.
        response = asyncio.create_task(self.model.get_response(*args, **kwargs))
        stop = asyncio.create_task(wait_for_native_stop())
        try:
            done, _ = await asyncio.wait(
                (response, stop), return_when=asyncio.FIRST_COMPLETED
            )
            if response in done:
                return response.result()
            stop.result()
            raise dbos_error.DBOSWorkflowCancelledError("Native execution stopped")
        finally:
            for task in (response, stop):
                if not task.done():
                    task.cancel()
            await asyncio.gather(response, stop, return_exceptions=True)

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError("Durable business Agents use DBOSRunner.run")


@dataclass
class _RunBudget:
    """Share only budget data between the Runtime, tools and Context Harness."""

    initial: dict[str, Any]
    delta: dict[str, Any] = field(default_factory=dict)
    nested: dict[str, Any] = field(default_factory=dict)

    def projected(self) -> dict[str, Any]:
        """Project cumulative admission usage without submitting the baseline."""
        return merge_budget_usage(
            self.initial, merge_budget_usage(self.delta, self.nested)
        )

    def add(self, usage: Any) -> None:
        """Merge one actual usage delta with the existing accounting rules."""
        self.delta = merge_budget_usage(self.delta, usage)
        local = tool_usage.get()
        if local is not None:
            merged = merge_budget_usage(local, usage)
            local.clear()
            local.update(merged)


class _BusinessAgentHarness(SDKContextHarness):
    """Apply business budgets while retaining the existing Context Harness lifecycle."""

    def __init__(
        self,
        *,
        workspace: ResearchWorkspace,
        config: Configuration,
        budget: _RunBudget,
        evidence_tools: frozenset[str],
    ) -> None:
        super().__init__(
            workspace,
            evidence_tools=evidence_tools,
            consumer_refresh_every=1,
            update_consumer_context=True,
        )
        self.config = config
        self.run_budget = budget
        self.items: list[Any] = []
        self._compacted_turns: set[int] = set()
        self._summary_references: set[str] = set()

    async def filter_model_input(self, data: Any) -> Any:
        self.items = list(data.model_data.input)
        filtered = await super().filter_model_input(data)
        if not can_spend_model_call(
            self.config, self._filter_budget(), reserve_final_report_call=True
        ) or is_over_budget(self.config, self._filter_budget()):
            raise _ReasoningStopped(
                "Business budget stopped SDK reasoning before another request."
            )
        filtered = await self._compact_history(filtered, data.agent)
        if not can_spend_model_call(
            self.config, self._filter_budget(), reserve_final_report_call=True
        ) or is_over_budget(self.config, self._filter_budget()):
            raise _ReasoningStopped(
                "Rolling compaction used the remaining reasoning allowance."
            )
        return filtered

    def _filter_budget(self):
        return merge_budget_usage(
            self.run_budget.projected(), getattr(self, "_filter_usage", None)
        )

    def _estimate(self, data: ModelInputData, agent: Agent[Any]) -> int:
        return estimate_text_tokens(
            json.dumps(
                {
                    "instructions": data.instructions,
                    "input": data.input,
                    "tools": [tool.params_json_schema for tool in agent.tools],
                },
                ensure_ascii=False,
                default=str,
            )
        )

    def _with_summary(
        self, head: list[Any], units: list[Any], summary: str, instructions: Any
    ) -> ModelInputData:
        summary_items = (
            [
                {
                    "role": "user",
                    "content": "<SDK Rolling Summary>\n"
                    + summary
                    + "\n</SDK Rolling Summary>",
                }
            ]
            if summary
            else []
        )
        return ModelInputData(
            input=[
                *head,
                *summary_items,
                *(item for _, unit in units for item in unit),
            ],
            instructions=instructions,
        )

    async def _compact_history(
        self, data: ModelInputData, agent: Agent[Any]
    ) -> ModelInputData:
        # The public filter changes requests, not Runner's original transcript.
        # Reapply the committed window on every call without mutating SDK internals.
        head, units = self.history_units(data.input)
        active = [
            (turn, unit) for turn, unit in units if turn not in self._compacted_turns
        ]
        current = self._with_summary(
            head, active, self.workspace.rolling_summary, data.instructions
        )
        capacity = (
            self.config.research_graph_context_capacity_tokens
            or get_model_token_limit(self.config.research_model)
            # Unknown deployment aliases still get a bounded application
            # window. This is not a claim about the provider's context limit.
            or 32_768
        )
        before = self._estimate(current, agent)
        self.estimated_input_tokens = before
        if (
            context_pressure_ratio(before, capacity)
            < self.config.context_compaction_threshold_ratio
        ):
            return current
        recent = max(1, self.config.recent_raw_steps)
        eligible = [
            (turn, unit)
            for turn, unit in active[:-recent]
            if self.can_compact_unit(unit)
        ]
        if not eligible:
            raise SDKContextLimitExceeded(
                "Protected task, memory, pending evidence or recent native steps exceed context capacity; Phase 2 cannot safely discard them."
            )
        removed = {turn for turn, _ in eligible}
        kept = [(turn, unit) for turn, unit in active if turn not in removed]
        # Bound the summary request independently of the business context window.
        # The complete original items and tool observations remain available.
        history_text = json.dumps(
            [item for _, unit in eligible for item in unit],
            ensure_ascii=False,
            default=str,
        )
        if len(history_text) > 30_000:
            history_text = (
                history_text[:15_000]
                + "\n[Middle omitted from summary input; original transcript retained.]\n"
                + history_text[-15_000:]
            )
        with business_span("rolling_compaction", estimated_tokens_before=before):
            model_name = (
                self.config.rolling_compaction_model or self.config.compression_model
            )
            protected = json.dumps(head, ensure_ascii=False, default=str)
            previous = self.workspace.rolling_summary
            try:
                summary, _ = await summarize_compactable_history(
                    history_text,
                    previous_summary=previous,
                    protected_context=protected,
                    model=self.workspace.model_factory(
                        model_name, self.config.rolling_compaction_model_max_tokens
                    ),
                    model_name=model_name,
                    max_retries=self.config.max_structured_output_retries,
                )
                # Keep receipt identities independently of lossy model prose. They
                # remain references, never newly verified evidence or a second graph.
                references = set()
                for _, unit in eligible:
                    for item in unit:
                        result = self.results.get(item.get("call_id"))
                        receipt = (
                            self.workspace.receipts.get(result.graph_call_id)
                            if result
                            else None
                        )
                        if receipt is not None:
                            references.update(receipt.node_ids)
                references.update(self._summary_references)
                allowed = set(self.workspace.relevant_subgraph.node_ids) | references
                cited = set(
                    re.findall(
                        r"\b(?:SRC|EVD|CLM|FND|EVT|ENT|UNT|SFD|COV)_[a-f0-9]{20}\b",
                        summary,
                    )
                )
                if not cited.issubset(allowed):
                    raise ValueError(
                        "Rolling summary contains unverified graph references."
                    )
                references.update(cited)
                if references:
                    summary += "\nCommitted graph references: " + ", ".join(
                        sorted(references)
                    )
                if (
                    estimate_text_tokens(summary)
                    > self.config.rolling_compaction_model_max_tokens
                ):
                    raise ValueError(
                        "Rolling summary exceeds the configured output bound."
                    )
                candidate = self._with_summary(head, kept, summary, data.instructions)
                after = self._estimate(candidate, agent)
                if (
                    after >= before
                    or context_pressure_ratio(after, capacity)
                    >= self.config.context_compaction_threshold_ratio
                ):
                    raise ValueError(
                        "Rolling compaction cannot fit the protected request or did not reduce it."
                    )
            except BaseException as exc:
                self.events.append(("rolling_compact_failed", type(exc).__name__))
                await self._observe(
                    "harness",
                    stage="rolling_compact",
                    status="failed",
                    error_type=type(exc).__name__,
                )
                self.workspace.metrics.add("rolling_compact_failures")
                if self.workspace.transcript is not None:
                    self.workspace.transcript.append(
                        "sdk_rolling_compact_failed",
                        {
                            "error_type": type(exc).__name__,
                            "estimated_input_tokens": before,
                        },
                    )
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise SDKContextLimitExceeded(
                    f"Rolling compaction failed safely: {type(exc).__name__}: {exc}; original history retained."
                ) from exc
            annotate(estimated_tokens_after=after, removed_turns=len(removed))
            self.estimated_input_tokens = after
            self._compacted_turns.update(removed)
            self._summary_references = references
            self.workspace.rolling_summary = summary
            self.workspace.metrics.add("rolling_compact_count")
            self.workspace.metrics.add(
                "rolling_compact_tokens_removed", before - after, quality="estimated"
            )
            self.events.append(("rolling_compact", str(len(removed))))
            await self._observe(
                "harness",
                stage="rolling_compact",
                status="completed",
                removed_turns=len(removed),
            )
            if self.workspace.transcript is not None:
                self.workspace.transcript.append(
                    "sdk_rolling_compact",
                    {
                        "turns": sorted(removed),
                        "tokens_removed": before - after,
                        "rolling_summary": summary,
                    },
                )
            return candidate

    async def on_llm_start(
        self, context: Any, agent: Any, system_prompt: Any, input_items: Any
    ) -> None:
        await super().on_llm_start(context, agent, system_prompt, input_items)
        self.run_budget.add({"model_calls": 1})

    async def on_llm_end(self, context: Any, agent: Any, response: Any) -> None:
        self.run_budget.add(budget_from_sdk_usage(response.usage))
        await super().on_llm_end(context, agent, response)
        self.items.extend(response.to_input_items())


class SDKAgentRuntime:
    """Map all four AgentSpecs to SDK Agents and run each exactly once.

    Own assembly, the report envelope and private memory. Runner is the only
    model/tool loop; no alternate runtime can execute a role.
    """

    run_budget: _RunBudget
    harness: _BusinessAgentHarness

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
        compress_report: CompressionCallback,
    ) -> None:
        """Bind one AgentSpec to its SDK model, tools and ResearchWorkspace."""
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
        self.compress_report = compress_report
        self.rolling_summary = str(self.agent_state.get("rolling_summary") or "")
        self.workspace.rolling_summary = self.rolling_summary

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
    def build_private_memory(
        role: str, report: str, raw_notes: list[str]
    ) -> dict[str, Any]:
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

    async def _finish(
        self, task: str, messages: list[Any], budget_update: dict[str, Any]
    ) -> AgentRuntimeResult:
        """Preserve the business report and private-memory contract for SDK roles."""
        research_round = self.workspace.scope.research_round
        mode = "followup" if research_round > 1 else "initial"
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

    def projected_budget(self) -> dict[str, Any]:
        """Return cumulative business usage for admission, without submitting it."""
        return self.run_budget.projected()

    def _register_tool(
        self, tool: BusinessTool | BaseTool, executor: ToolExecutor
    ) -> FunctionTool:
        if get_tool_domain(tool) not in self.spec.allowed_domains:
            raise ValueError(
                f"Tool {tool.name!r} is outside {self.role}'s allowed domains."
            )
        if isinstance(tool, BusinessTool):
            declared = tool.sdk_tool
            drain_on_cancel = tool.drain_on_cancel
        else:
            # MCP keeps its server-owned Schema and protocol invoker.
            declared = mcp_function_tool(tool)
            drain_on_cancel = bool(
                getattr(tool, "func", None) and not getattr(tool, "coroutine", None)
            )
        return self.harness.function_tool(
            declared,
            invoke=partial(executor.invoke, declared.name, declared.on_invoke_tool),
            error_formatter=format_tool_error,
            drain_on_cancel=drain_on_cancel,
        )

    def _report_messages(self, task: str) -> list[Any]:
        messages: list[Any] = [HumanMessage(content=task)]
        output_ids: set[str] = set()
        for item in self.harness.items:
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
                        content=tool_result_text(item["output"]),
                        tool_call_id=item["call_id"],
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
        for result in self.harness.results.values():
            if result.tool_call_id not in output_ids and result.status != "running":
                messages.append(
                    ToolMessage(
                        content=tool_result_text(result.observation),
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
        self.run_budget = _RunBudget(initial=self.initial_budget)
        # Preserve the existing Producer's admitted tools, excluding controls.
        self.harness = _BusinessAgentHarness(
            workspace=self.workspace,
            config=self.config,
            budget=self.run_budget,
            evidence_tools=frozenset(
                tool.name
                for tool in self.tools
                if isinstance(tool, (BusinessTool, BaseTool))
                and tool.name not in {"think_tool", "ResearchComplete"}
            ),
        )
        tools: dict[str, BusinessTool | BaseTool] = {}
        for tool in self.tools:
            if not isinstance(tool, (BusinessTool, BaseTool)):
                raise TypeError(
                    "SDK business runtime requires local function tools; hosted/native search is not verified."
                )
            if tool.name in tools:
                raise ValueError(f"Duplicate business tool name: {tool.name}")
            tools[tool.name] = tool
        executor = ToolExecutor(
            self.role,
            self.spec.allowed_domains,
            self.config,
            self.runtime_config,
            self.run_budget,
            tools,
        )
        self.harness.tool_executor = executor
        scope = stage_context.get()
        self.agent = Agent(
            name=self.spec.display_name,
            instructions=self.system_prompt,
            model=MeteredModel(self.model) if scope else self.model,
            tools=[self._register_tool(tool, executor) for tool in tools.values()],
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
                        + "\n</Agent Dynamic Context>"
                    ),
                }
            )
        self.sdk_result = None
        session = None
        try:
            if scope is not None:
                await scope.observe(
                    "session_started",
                    {"role": self.role, "invocation_id": self.harness.sdk_run_id},
                )
                if settings().session_path:
                    from agents import SQLiteSession

                    session, cancelled = await owned_io(
                        SQLiteSession,
                        scope.stage_id + ":" + self.harness.sdk_run_id,
                        settings().session_path,
                    )
                    if cancelled is not None:
                        raise cancelled
            # Graph services and report synthesis retain their existing model
            # adapters. Capture their actual attempts once, including failures.
            with (
                budget_capture() as nested,
                model_budget_admission(self.config, self.run_budget.projected),
            ):
                self.run_budget.nested = nested
                try:
                    self.sdk_result = await self.harness.run(
                        self.agent,
                        inputs,
                        run_config=RunConfig(
                            tracing_disabled=not telemetry_enabled(),
                            trace_include_sensitive_data=False,
                        ),
                        max_turns=max(1, self.config.max_react_tool_calls),
                        session=session,
                    )
                except (_ReasoningStopped, MaxTurnsExceeded) as stopped:
                    if (
                        self.harness.last_settlement is None
                        or not self.harness.last_settlement.succeeded
                    ):
                        raise
                    self.run_budget.add(budget_usage_with_reason(str(stopped)))
                messages = self._report_messages(task)
                # The old graph finalizer has no budget admission of its own.
                # Explicitly preserve the final workflow report reserve here.
                if self.workspace.strategy.graph_enabled and not can_spend_model_call(
                    self.config, self.projected_budget(), reserve_final_report_call=True
                ):
                    self.run_budget.add(
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
                    before_report = merge_budget_usage(self.run_budget.delta, nested)
                    with budget_capture() as report_usage:
                        try:
                            result = await self._finish(task, messages, before_report)
                        except BaseException as exc:
                            self.run_budget.add(
                                getattr(exc, "budget_usage", report_usage)
                            )
                            raise
                    self.run_budget.add(diff_budget_usage(result.budget, before_report))
            # _finish returns the same costs for legacy callers. Use captured
            # nested usage here instead, so none of those costs are merged twice.
            self.budget_usage = merge_budget_usage(self.run_budget.delta, nested)
            result.budget = self.budget_usage
            return result
        except BaseException as exc:
            self.budget_usage = merge_budget_usage(
                self.run_budget.delta, self.run_budget.nested
            )
            setattr(exc, "budget_usage", self.budget_usage)
            setattr(exc, "context_settlement", self.harness.last_settlement)
            # Keep an explicit owner for any result that outlives the bounded
            # drain. Callers can inspect/settle it without replaying the role.
            setattr(exc, "context_harness", self.harness)
            raise
        finally:
            # Match legacy capture semantics on success and exceptional exits.
            capture_budget_usage(self.budget_usage)
            if session is not None:
                await owned_io(session.close)


__all__ = ["SDKAgentRuntime", "SDKContextLimitExceeded"]
