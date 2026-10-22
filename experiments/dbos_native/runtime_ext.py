"""Experimental injection seam; inherited business logic stays source-comparable.

The run method below is the Phase 4 SDKAgentRuntime.run implementation with one
Harness factory expression changed. It is retained business code, not a new loop.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from agents import Agent, Model, ModelSettings, RunConfig
from agents.exceptions import MaxTurnsExceeded
from agents.retry import ModelRetrySettings
from agents.tool import FunctionTool
from dbos_openai_agents import DBOSRunner
from langchain_core.tools import BaseTool

from open_deep_research.budget import (
    budget_capture,
    budget_usage_with_reason,
    can_spend_model_call,
    capture_budget_usage,
    diff_budget_usage,
    merge_budget_usage,
    model_budget_admission,
)
from open_deep_research.research_graph.schema import stable_id
from open_deep_research.runtime.agent_runtime import AgentRuntimeResult
from open_deep_research.runtime.sdk_context import ResearchGraphSettlementError
from open_deep_research.runtime.sdk_runtime import (
    SDKAgentRuntime,
    _BusinessAgentHarness,
    _ReasoningStopped,
    _RunBudget,
)
from open_deep_research.workflow.execution import observe_execution


def agent_policy(agent):
    import json

    def guards(value):
        return tuple(id(item) for item in (value or []))

    return (
        agent.name,
        agent.instructions,
        agent.output_type,
        tuple(
            (
                tool.name,
                tool.description,
                json.dumps(tool.params_json_schema, sort_keys=True),
                tool.strict_json_schema,
                guards(tool.tool_input_guardrails),
                guards(tool.tool_output_guardrails),
            )
            for tool in agent.tools
            if isinstance(tool, FunctionTool)
        ),
        len(agent.tools),
        guards(agent.input_guardrails),
        guards(agent.output_guardrails),
    )


class ScopedHarness(_BusinessAgentHarness):
    """Admit trusted runner clones using public Hook and filter data, not names alone."""

    runner = DBOSRunner

    async def on_agent_start(self, context, agent):
        self._check(context.context)
        if (
            not self._scope_open
            or agent.handoffs
            or agent.mcp_servers
            or agent_policy(agent) != self._policy
        ):
            raise ValueError("Untrusted agent policy or invocation scope")
        self._approved.add(id(agent))

    async def filter_model_input(self, data):
        self._check(data.context)
        if (
            id(data.agent) not in self._approved
            or agent_policy(data.agent) != self._policy
        ):
            raise ValueError("Agent was not admitted by this invocation Hook")
        # Normalize only AFTER admission. The original strict filter remains
        # intact and continues to validate the canonical role/context binding.
        return await super().filter_model_input(replace(data, agent=self._agent))

    async def run(self, agent, input, *, run_config=None, max_turns=10, session=None):
        if self._started or agent.handoffs or agent.mcp_servers:
            raise ValueError("One fresh scoped role invocation is required")
        if any(
            isinstance(t, FunctionTool) and t.tool_output_guardrails
            for t in agent.tools
        ):
            raise ValueError("Tool output guardrail settlement is unsupported")
        config = run_config or RunConfig(
            tracing_disabled=True, trace_include_sensitive_data=False
        )
        if config.call_model_input_filter is not None:
            raise ValueError("The bound Harness owns the input filter")
        self._policy = agent_policy(agent)
        self._approved = set()
        self._scope_open = True
        self._agent, self._started = agent, True
        self.sdk_run_id = stable_id(
            "SDK",
            self.scope.run_id,
            self.scope.role,
            self.scope.task_id,
            self.scope.research_round,
        )
        try:
            result = await self.runner.run(
                agent,
                input,
                context=self,
                hooks=self,
                run_config=replace(
                    config, call_model_input_filter=self.filter_model_input
                ),
                max_turns=max_turns,
                session=session,
            )
        except BaseException as exc:
            self._closing = True
            try:
                await self._settle_on_exit(
                    "cancelled"
                    if isinstance(exc, asyncio.CancelledError)
                    else "exception"
                )
            except BaseException as cleanup:
                exc.add_note(f"Settlement error: {type(cleanup).__name__}")
            raise
        finally:
            self._scope_open = False
        self._closing = True
        settlement = await self._settle_on_exit("completed")
        if not settlement.succeeded:
            raise ResearchGraphSettlementError(str(settlement))
        return result


class ExtensibleSDKRuntime(SDKAgentRuntime):
    harness_type = ScopedHarness

    async def run(self, task: str) -> AgentRuntimeResult:
        """Run the SDK loop, settle tools, and preserve the role result contract."""
        if not isinstance(self.model, Model):
            raise TypeError(
                "SDKAgentRuntime requires an official SDK Model, not a LangChain model."
            )
        self.run_budget = _RunBudget(initial=self.initial_budget)
        self._tool_tasks: dict[str, asyncio.Task[str]] = {}
        # Preserve the existing Producer's admitted tools, excluding controls.
        self.harness = self.harness_type(
            workspace=self.workspace,
            config=self.config,
            budget=self.run_budget,
            evidence_tools=frozenset(
                tool.name
                for tool in self.tools
                if isinstance(tool, BaseTool)
                and tool.name not in {"think_tool", "ResearchComplete"}
            ),
        )
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
                        + "\n</Agent Dynamic Context>"
                    ),
                }
            )
        self.sdk_result = None
        session = await observe_execution(
            "sdk_session", role=self.role, invocation_id=self.harness.sdk_run_id
        )
        try:
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
                            tracing_disabled=True, trace_include_sensitive_data=False
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
