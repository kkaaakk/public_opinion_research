"""Execute business AgentSpecs with the official SDK, retaining business results.

Only Runner owns the model/tool loop. The harness adds business admission and SDK
usage to the collector; DBOS owns durable business orchestration.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
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
from open_deep_research.runtime.compaction import summarize_history
from open_deep_research.runtime.sdk_context import SDKContextHarness
from open_deep_research.runtime.tool_executor import ToolExecutor, tool_usage
from open_deep_research.tools import BusinessTool, tool_result_text
from open_deep_research.workflow.dbos_config import native_client, settings
from open_deep_research.workflow.execution import (
    ExecutionControlError,
    owned_io,
    stage_context,
)

SDK_RUNTIME_VERSION = "sdk-vertical-v1"

CompressionCallback = Callable[
    [list[Any], str, str, dict[str, Any]],
    Awaitable[tuple[str, list[str], dict[str, Any]]],
]


@dataclass
class AgentRuntimeResult:
    """Serializable business result of one SDK role invocation."""

    report: str
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
    """Apply business budgets and the single rolling history compaction."""

    def __init__(
        self,
        *,
        role: str,
        task_id: str,
        research_round: int,
        config: Configuration,
        budget: _RunBudget,
        model_factory: Callable[[str, int | None], Any],
    ) -> None:
        super().__init__(
            role=role, task_id=task_id, research_round=research_round
        )
        self.config = config
        self.run_budget = budget
        self.model_factory = model_factory
        self.items: list[Any] = []
        self._compacted_turns: set[int] = set()
        self.rolling_summary = ""

    def _extra_snapshot(self) -> dict[str, Any]:
        return {
            "business": {
                "delta": self.run_budget.delta,
                "compacted_turns": sorted(self._compacted_turns),
                "rolling_summary": self.rolling_summary,
            }
        }

    def _restore_extra(self, state: dict[str, Any]) -> None:
        saved = state.get("business")
        if saved is None:
            return
        self.run_budget.delta = saved["delta"]
        self._compacted_turns = set(saved["compacted_turns"])
        self.rolling_summary = saved["rolling_summary"]

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

    @staticmethod
    def _with_summary(
        head: list[Any], units: list[Any], summary: str, instructions: Any
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
            head, active, self.rolling_summary, data.instructions
        )
        capacity = (
            self.config.context_capacity_tokens
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
                "Current task, rolling summary or recent native steps exceed "
                "context capacity; they cannot be safely discarded."
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
            task_context = json.dumps(head, ensure_ascii=False, default=str)
            previous = self.rolling_summary
            try:
                summary, _ = await summarize_history(
                    history_text,
                    previous_summary=previous,
                    task_context=task_context,
                    model=self.model_factory(
                        model_name, self.config.rolling_compaction_model_max_tokens
                    ),
                    model_name=model_name,
                    max_retries=self.config.max_structured_output_retries,
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
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise SDKContextLimitExceeded(
                    f"Rolling compaction failed safely: {type(exc).__name__}: {exc}; original history retained."
                ) from exc
            annotate(estimated_tokens_after=after, removed_turns=len(removed))
            self.estimated_input_tokens = after
            self._compacted_turns.update(removed)
            self.rolling_summary = summary
            self.events.append(("rolling_compact", str(len(removed))))
            await self._observe(
                "harness",
                stage="rolling_compact",
                status="completed",
                removed_turns=len(removed),
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

    Owns assembly and the report envelope. Runner is the only model/tool loop;
    no alternate runtime can execute a role.
    """

    run_budget: _RunBudget
    harness: _BusinessAgentHarness

    def __init__(
        self,
        *,
        role: str,
        spec: Any,
        task_id: str,
        research_round: int,
        config: Any,
        runtime_config: Any,
        model: Any,
        system_prompt: str,
        tools: list[Any],
        initial_budget: Mapping[str, Any],
        compress_report: CompressionCallback,
        model_factory: Callable[[str, int | None], Any],
    ) -> None:
        """Bind one AgentSpec to its SDK model, tools and compaction service."""
        self.role = role
        self.spec = spec
        self.task_id = task_id
        self.research_round = max(1, int(research_round or 1))
        self.config = config
        self.runtime_config = runtime_config
        self.model = model
        self.system_prompt = system_prompt
        self.tools = tools
        self.initial_budget = dict(initial_budget or {})
        self.compress_report = compress_report
        self.model_factory = model_factory

    async def _finish(
        self, task: str, messages: list[Any], budget_update: dict[str, Any]
    ) -> AgentRuntimeResult:
        """Preserve the business report contract for SDK roles."""
        mode = "followup" if self.research_round > 1 else "initial"
        body, _raw_notes, compression_budget = await self.compress_report(
            messages,
            task,
            self.spec.expected_output,
            merge_budget_usage(self.initial_budget, budget_update),
        )
        budget_update = merge_budget_usage(budget_update, compression_budget)
        report = (
            f"Agent role: {self.role}\n"
            f"Agent name: {self.spec.display_name}\n"
            f"Research round: {self.research_round}\n"
            f"Research mode: {mode}\n"
            f"Expected output: {self.spec.expected_output}\n\n{body}"
        )
        return AgentRuntimeResult(report=report, budget=budget_update)

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
        return executor.bind(declared, drain_on_cancel=drain_on_cancel)

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
        self.harness = _BusinessAgentHarness(
            role=self.role,
            task_id=self.task_id,
            research_round=self.research_round,
            config=self.config,
            budget=self.run_budget,
            model_factory=self.model_factory,
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
        inputs = [{"role": "user", "content": task}]
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
                before_report = merge_budget_usage(self.run_budget.delta, nested)
                with budget_capture() as report_usage:
                    try:
                        result = await self._finish(task, messages, before_report)
                    except BaseException as exc:
                        self.run_budget.add(getattr(exc, "budget_usage", report_usage))
                        raise
                self.run_budget.add(diff_budget_usage(result.budget, before_report))
            # _finish also returns the nested costs it was given. Use captured
            # usage here so none of those costs are merged twice.
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
