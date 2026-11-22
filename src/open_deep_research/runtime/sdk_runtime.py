"""Execute business AgentSpecs with the official SDK, retaining business results.

Runner owns the model and tool loop. DBOS owns durable business orchestration.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from agents import Agent, Model, ModelSettings, TResponseInputItem
from agents.exceptions import MaxTurnsExceeded
from agents.retry import ModelRetrySettings
from dbos import DBOS
from dbos import error as dbos_error
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from open_deep_research.budget import (
    ModelBudgetExceeded,
    admit_model_call,
    budget_capture,
    budget_usage_with_reason,
    capture_budget_usage,
    merge_budget_usage,
    model_budget_admission,
)
from open_deep_research.configuration import Configuration
from open_deep_research.llm.errors import ModelInvocationError
from open_deep_research.llm.generation import ModelOutputError, metered_model_response
from open_deep_research.runtime.sdk_context import (
    SDKContextHarness,
    SDKContextLimitExceeded,
    _ReasoningStopped,
)
from open_deep_research.runtime.tool_executor import ToolExecutor, tool_usage
from open_deep_research.tools import tool_result_text
from open_deep_research.tools.manager import ToolManager
from open_deep_research.workflow.dbos_config import native_client, settings
from open_deep_research.workflow.execution import (
    owned_io,
    stage_context,
)

SDK_RUNTIME_VERSION = "sdk-vertical-v1"

CompressionCallback = Callable[
    [list[BaseMessage], dict[str, Any]],
    Awaitable[tuple[str, dict[str, Any]]],
]


@dataclass
class AgentRuntimeResult:
    """Serializable business result of one SDK role invocation."""

    report: str
    budget: dict[str, Any] = field(default_factory=dict)
    replace_report: bool = False
    evidence_collected: bool = False


class MeteredModel(Model):
    """Admit SDK inference and write physical receipts inside DBOSRunner's Step."""

    def __init__(self, model):
        self.model = model
        self.model_name = getattr(model, "model_name", type(model).__name__)

    async def get_response(self, *args, **kwargs):
        try:
            admit_model_call({})
            response, _, _ = await metered_model_response(
                self.model_name, lambda identity: self._response(identity, *args, **kwargs)
            )
        except ModelBudgetExceeded as exc:
            raise _ReasoningStopped(str(exc)) from exc
        except ModelOutputError as exc:
            raise ModelInvocationError(
                type(exc).__name__, budget_usage=exc.budget_usage
            ) from exc
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
    """Project this role's delta for local model and tool admission."""

    initial: dict[str, Any]
    delta: dict[str, Any] = field(default_factory=dict)

    def projected(self) -> dict[str, Any]:
        """Project cumulative admission usage without submitting the baseline."""
        return merge_budget_usage(self.initial, self.delta)

    def add(self, usage: Any) -> None:
        """Merge one actual usage delta with the existing accounting rules."""
        merged = merge_budget_usage(self.delta, usage)
        self.delta.clear()
        self.delta.update(merged)
        local = tool_usage.get()
        if local is not None:
            merged = merge_budget_usage(local, usage)
            local.clear()
            local.update(merged)


class SDKAgentRuntime:
    """Assemble and run a Public Signal or Internal Knowledge Agent."""

    def __init__(
        self,
        *,
        role: str,
        spec: Any,
        task_id: str,
        research_round: int,
        config: Configuration,
        runtime_config: Mapping[str, Any],
        model: Model,
        system_prompt: str,
        tools: list[Any],
        initial_budget: Mapping[str, Any],
        compress_report: CompressionCallback,
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

    def _report_messages(
        self, task: str, items: Iterable[TResponseInputItem]
    ) -> list[BaseMessage]:
        messages: list[BaseMessage] = [HumanMessage(content=task)]
        output_ids: set[str] = set()
        for item in items:
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
        # An early stop can occur after tools finish but before the SDK returns them.
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
        budget = _RunBudget(initial=self.initial_budget)
        self.harness = SDKContextHarness(
            role=self.role,
            task_id=self.task_id,
            research_round=self.research_round,
            config=self.config,
            runtime_config=self.runtime_config,
        )
        manager = ToolManager(self.role, self.spec, self.runtime_config)
        tools = manager.index(self.tools)
        self.harness.tool_executor = ToolExecutor(
            self.role,
            self.spec.allowed_domains,
            self.config,
            self.runtime_config,
            budget,
            tools,
        )
        scope = stage_context.get()
        self.agent = Agent(
            name=self.spec.display_name,
            instructions=self.system_prompt,
            model=MeteredModel(self.model),
            tools=manager.bind(tools.values(), self.harness.tool_executor),
            output_type=None,
            model_settings=ModelSettings(
                max_tokens=self.config.research_model_max_tokens,
                retry=ModelRetrySettings(max_retries=0),
            ),
        )
        inputs = [{"role": "user", "content": task}]
        session = None
        try:
            if scope is not None:
                await scope.observe(
                    "session_started",
                    {"role": self.role, "invocation_id": self.harness.sdk_run_id},
                )
                if session_path := settings().session_path:
                    from agents import SQLiteSession

                    session, cancelled = await owned_io(
                        SQLiteSession,
                        scope.stage_id + ":" + self.harness.sdk_run_id,
                        session_path,
                    )
                    if cancelled is not None:
                        raise cancelled
            with (
                budget_capture() as usage,
                model_budget_admission(self.config, budget.projected),
            ):
                budget.delta = usage
                try:
                    sdk_result = await self.harness.run(
                        self.agent,
                        inputs,
                        max_turns=max(1, self.config.max_react_tool_calls),
                        session=session,
                    )
                    items = sdk_result.to_input_list()
                except (_ReasoningStopped, MaxTurnsExceeded) as stopped:
                    if (
                        self.harness.last_settlement is None
                        or not self.harness.last_settlement.succeeded
                    ):
                        raise
                    budget.add(budget_usage_with_reason(str(stopped)))
                    items = (
                        [item.to_input_item() for item in stopped.run_data.new_items]
                        if stopped.run_data is not None
                        else []
                    )
                messages = self._report_messages(task, items)
                with budget_capture() as report_usage:
                    try:
                        body, compression_usage = await self.compress_report(
                            messages, budget.projected()
                        )
                    except BaseException as exc:
                        budget.add(getattr(exc, "budget_usage", report_usage))
                        raise
                budget.add(compression_usage)
            mode = "followup" if self.research_round > 1 else "initial"
            return AgentRuntimeResult(
                report=(
                    f"Agent role: {self.role}\n"
                    f"Agent name: {self.spec.display_name}\n"
                    f"Research round: {self.research_round}\n"
                    f"Research mode: {mode}\n"
                    f"Expected output: {self.spec.expected_output}\n\n{body}"
                ),
                budget=usage,
                evidence_collected=(
                    not usage["degradation_reasons"]
                    and any(
                        record.status == "succeeded"
                        for record in self.harness.results.values()
                    )
                ),
            )
        except BaseException as exc:
            setattr(exc, "budget_usage", budget.delta)
            setattr(exc, "context_settlement", self.harness.last_settlement)
            # Keep an explicit owner for any result that outlives the bounded
            # drain. Callers can inspect/settle it without replaying the role.
            setattr(exc, "context_harness", self.harness)
            raise
        finally:
            self.budget_usage = budget.delta
            capture_budget_usage(self.budget_usage)
            if session is not None:
                await owned_io(session.close)


__all__ = ["SDKAgentRuntime", "SDKContextLimitExceeded"]
