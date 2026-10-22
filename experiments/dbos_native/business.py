"""Injected offline inference, real SDK Runtime/Harness and existing domain rules."""

from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from pathlib import Path

from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from codec import dumps
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langchain_core.tools import StructuredTool
from openai.types.responses import ResponseReasoningItem
from test_sdk_context import _payload
from test_sdk_phase2_integration import Services
from test_sdk_runtime import TOKENS

from open_deep_research.budget import (
    ainvoke_model_with_budget,
    budget_capture,
    model_budget_admission,
)
from open_deep_research.configuration import Configuration
from open_deep_research.public_opinion_agents import get_public_opinion_agent_spec
from open_deep_research.research_graph import ResearchWorkspace, create_context_strategy
from open_deep_research.runtime.business_agent import (
    _build_business_agent_assignment,
    _build_state_patch,
    _execute_tool_safely,
    _task_descriptor,
)
from open_deep_research.runtime.sdk_runtime import SDKAgentRuntime
from open_deep_research.state import (
    ResearchQuestion,
    ResearchReview,
    ResearchTask,
    Section,
    Sections,
)
from open_deep_research.workflow import reporting
from open_deep_research.workflow.execution import execution_observer
from open_deep_research.workflow.python_state import role_snapshot

ROLES = ("public_signal", "internal_knowledge", "risk_assessment", "response_strategy")


async def gate(request, point, role=""):
    if os.environ.get("NATIVE_FAULT") == point and (
        not os.environ.get("NATIVE_FAULT_ROLE")
        or os.environ["NATIVE_FAULT_ROLE"] == role
    ):
        directory = Path(request["directory"])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "ready").write_text(point, encoding="utf-8")
        while not (directory / "release").exists():
            await asyncio.sleep(0.1)


class Inference(Services):
    def __init__(self, request):
        super().__init__()
        self.request = request

    def with_structured_output(self, schema, include_raw=False):
        if schema not in {ResearchQuestion, Sections, ResearchReview}:
            return super().with_structured_output(schema, include_raw)

        async def respond(messages, config=None):
            raw = await self.raw.ainvoke(messages, config)
            if schema is ResearchQuestion:
                value = ResearchQuestion(
                    research_brief="Investigate brand recall evidence"
                )
            elif schema is Sections:
                value = Sections(
                    sections=[
                        Section(name="Evidence", description="brand recall evidence")
                    ]
                )
            else:
                first = "Current research round: 1" in str(messages[0].content)
                gap = self.request.get("scenario") == "gap" and first
                value = ResearchReview(
                    research_complete=not gap,
                    next_tasks=[
                        ResearchTask(
                            task_id="recall-date-gap",
                            target_role="public_signal",
                            objective="Verify recall date",
                            evidence_needed="Dated evidence",
                            reason="Changes risk",
                        ),
                        ResearchTask(
                            task_id="internal-date-gap",
                            target_role="internal_knowledge",
                            objective="Verify internal policy date",
                            evidence_needed="Internal dated evidence",
                            reason="Changes risk",
                        ),
                    ]
                    if gap
                    else [],
                )
            return {"raw": raw, "parsed": value, "parsing_error": None}

        return RunnableLambda(respond)


class Business:
    def __init__(self, request, domain):
        self.request = request
        self.domain = domain
        self.services = Inference(request)
        self.config = Configuration(
            business_agent_runtime="openai_agents",
            workflow_backend="python",
            research_graph_enabled=True,
            research_graph_backend="neo4j",
            research_graph_uri="bolt://127.0.0.1:17688",
            research_graph_username="neo4j",
            research_graph_password="native-poc-password",
            research_graph_transcript_dir=str(
                Path(__file__).resolve().parents[2]
                / ".tmp"
                / "native-transcripts"
                / request["run_id"][:16]
            ),
            recent_raw_steps=0,
            research_model="fixture:controlled",
            budget_enabled=True,
            max_model_calls=request.get("max_model_calls", 500),
            max_research_rounds=3,
            max_structured_output_retries=1,
            max_react_tool_calls=40,
            research_graph_context_capacity_tokens=22000
            if request.get("scenario") == "rolling"
            else 32768,
        )
        self.cfg = {
            "configurable": {
                "business_agent_runtime": "openai_agents",
                "workflow_backend": "python",
                "research_run_id": request["run_id"],
                "thread_id": request["run_id"],
                "user_id": request["owner"],
                "rag_memory_write_enabled": False,
            }
        }

    async def plan(self):
        with (
            budget_capture() as usage,
            model_budget_admission(self.config, lambda: dict(usage)),
        ):
            question, _ = await ainvoke_model_with_budget(
                self.services.with_structured_output(ResearchQuestion),
                [HumanMessage(content=self.request["topic"])],
                model_name="fixture:plan",
                structured_output=True,
            )
            sections, _ = await ainvoke_model_with_budget(
                self.services.with_structured_output(Sections),
                [HumanMessage(content=question["parsed"].research_brief)],
                model_name="fixture:plan",
                structured_output=True,
            )
        return {
            "messages": [HumanMessage(content=self.request["topic"], id="input")],
            "workflow": {"brief": question["parsed"].research_brief, "round": 1},
            "report": {"sections": sections["parsed"].sections},
            "research": {"run_id": self.request["run_id"]},
            "agents": {r: {"memory": [{"content": f"private-{r}"}]} for r in ROLES},
            "runtime": {"budget": dict(usage)},
        }

    async def review(self, state):
        round_number = state["workflow"]["round"]
        graph = ResearchWorkspace(
            strategy=create_context_strategy(
                "research_graph_consumer", graph_enabled=True
            ),
            research_state=state["research"],
            role="risk_assessment",
            configurable=self.config,
            model_factory=lambda *_: self.services,
            run_id=self.request["run_id"],
        )
        graph.context_for(query_suffix="brand recall")
        assert graph.relevant_subgraph.nodes, (
            "Review must read real committed graph evidence"
        )
        value, usage = await ainvoke_model_with_budget(
            self.services.with_structured_output(ResearchReview),
            [
                HumanMessage(
                    content=f"Current research round: {round_number}\nEvidence: {graph.relevant_subgraph.model_dump_json()}"
                )
            ],
            model_name="fixture:review",
            structured_output=True,
        )
        return {"workflow": {"review": value["parsed"]}, "runtime": {"budget": usage}}

    async def role(self, role, state):
        state = role_snapshot(state, role)
        round_number = state["workflow"]["round"]
        role_id = f"{self.request['run_id']}:{role}:{round_number}"
        permit, artifact = self.domain.acquire(role_id)
        if artifact is not None:
            return artifact
        self.domain.fact(role_id, "role_enter", {"role": role, "round": round_number})
        spec = get_public_opinion_agent_spec(role)
        ws = ResearchWorkspace(
            strategy=create_context_strategy(spec.context_strategy, graph_enabled=True),
            research_state=state["research"],
            role=role,
            configurable=self.config,
            model_factory=lambda *_: self.services,
            task=_task_descriptor(state, role, self.request["run_id"]),
            run_id=self.request["run_id"],
            research_round=round_number,
        )

        async def observer(kind, **facts):
            if kind in {"model_started", "tool_started"}:
                self.domain.check(role_id, permit)
            if kind == "tool_started":
                return self.domain.intent(role_id, permit, facts["identity"])
            if kind == "tool_finished":
                await gate(self.request, "lost_receipt", role)
                self.domain.receipt(role_id, permit, facts["identity"], facts["status"])
            return None

        async def search(query: str, config: RunnableConfig, limit: int = 3):
            operation_id = config["metadata"]["operation_id"]
            self.domain.fact(role_id, "tool_invoker", {"operation_id": operation_id})
            value = self.domain.effect(role_id, operation_id, dumps(_payload(role)))
            await gate(self.request, "tool_wait", role)
            return value

        tools = []
        if role in ROLES[:2]:
            tools = [
                StructuredTool.from_function(
                    coroutine=search,
                    name="web_search" if role == ROLES[0] else "rag_search",
                    description="Retrieve synthetic cited evidence",
                    metadata={
                        "tool_domain": "web_search" if role == ROLES[0] else "rag"
                    },
                )
            ]

        async def first(call):
            text = json.dumps(call.input)
            assert f"private-{role}" in text
            assert all(
                f"private-{other}" not in text for other in ROLES if other != role
            )
            if role in ROLES[:2]:
                if round_number == 1:
                    self.domain.fact(role_id, "model_enter", {"role": role})
                    for _ in range(100):
                        if (
                            len(
                                self.domain.facts(self.request["run_id"], "model_enter")
                            )
                            >= 2
                        ):
                            break
                        await asyncio.sleep(0.05)
                    else:
                        raise RuntimeError("Producer execution was serialized")
                return [function_call(tools[0].name, {"query": role}, call_id="lookup")]
            assert "[Evidence]" in text
            if role == "response_strategy":
                assert "risk_assessment GRAPH REPORT" in text
            return [assistant_message(f"{role} evidence analysis")]

        async def second(call):
            assert "Persisted to Research Graph" in json.dumps(call.input)
            if self.request.get("scenario") == "producer_error":
                if role == "internal_knowledge":
                    self.domain.fact(role_id, "waiting_model", {})
                    await asyncio.Event().wait()
                else:
                    for _ in range(100):
                        if self.domain.facts(
                            self.request["run_id"] + ":internal_knowledge:",
                            "waiting_model",
                        ):
                            break
                        await asyncio.sleep(0.05)
                    from agents.exceptions import ModelBehaviorError

                    raise ModelBehaviorError(
                        "Synthetic producer model failure after tool settlement"
                    )
            await gate(self.request, "model_wait", role)
            return [assistant_message("producer analysis complete")]

        steps = [ModelStep.respond(first), ModelStep.respond(second)]
        if self.request.get("scenario") == "rolling" and role in ROLES[:2]:
            n = 0

            def continuation(call):
                nonlocal n
                n += 1
                return [
                    ResponseReasoningItem(
                        id=f"reason-{n}",
                        type="reasoning",
                        summary=[],
                        content=[
                            {
                                "type": "reasoning_text",
                                "text": "verified brand recall evidence " * 180,
                            }
                        ],
                    ),
                    function_call(
                        tools[0].name, {"query": role}, call_id=f"lookup-{n}"
                    ),
                ]

            steps = (
                [ModelStep.respond(first)]
                + [ModelStep.respond(continuation) for _ in range(24)]
                + [[assistant_message("long producer complete")]]
            )
        model = ScriptedModel(steps, default_usage=TOKENS)

        async def compress(messages, task, contract, budget):
            value, usage = await ainvoke_model_with_budget(
                self.services, messages, model_name="fixture:report"
            )
            return str(value.content), [], usage

        runtime_cls = SDKAgentRuntime
        if self.request.get("route") == "B":
            from runtime_ext import ExtensibleSDKRuntime

            runtime_cls = ExtensibleSDKRuntime
        runtime = runtime_cls(
            role=role,
            spec=spec,
            agent_state=state["agents"][role],
            workspace=ws,
            config=self.config,
            runtime_config=self.cfg,
            model=model,
            system_prompt=spec.format_system_prompt(
                retrieval_tool_prompt="Controlled permitted evidence tools",
                mcp_prompt="",
                date="2026-10-22",
                organization_context="Synthetic POC organization",
            ),
            tools=tools,
            initial_budget=state["runtime"]["budget"],
            execute_tool=_execute_tool_safely,
            compress_report=compress,
        )
        token = execution_observer.set(observer)
        try:
            result = await runtime.run(
                _build_business_agent_assignment(state, role, self.config, spec)
            )
            patch = _build_state_patch(
                role=role,
                state=state,
                research_round=round_number,
                result=result,
                workspace=ws,
            )
            self.domain.artifact(role_id, permit, patch)
            return patch
        except BaseException as exc:
            self.domain.fact(
                role_id,
                "role_error",
                {
                    "kind": type(exc).__name__,
                    "budget": getattr(exc, "budget_usage", {}),
                    "settled": bool(
                        getattr(exc, "context_settlement", None)
                        and exc.context_settlement.succeeded
                    ),
                },
            )
            raise
        finally:
            if hasattr(runtime, "harness"):
                h = runtime.harness
                self.domain.fact(
                    role_id,
                    "harness",
                    {
                        "role": role,
                        "filter_calls": sum(e[0] == "input_filter" for e in h.events),
                        "compactions": sum(e[0] == "rolling_compact" for e in h.events),
                        "settled": bool(
                            h.last_settlement and h.last_settlement.succeeded
                        ),
                        "evidence": list(ws.working_context.active_evidence_ids),
                        "receipts": len(ws.receipts),
                        "budget": getattr(runtime, "budget_usage", {}),
                    },
                )
            execution_observer.reset(token)

    async def report(self, state):
        from open_deep_research.research_graph.workspace import (
            retrieve_research_context,
        )

        graph = retrieve_research_context(
            self.config, run_id=self.request["run_id"], query=state["workflow"]["brief"]
        )
        prompt = (
            "Compile final brand recall report from committed graph evidence: "
            + graph.model_dump_json()
        )
        assert all(f"private-{role}" not in prompt for role in ROLES)
        self.domain.fact(
            self.request["run_id"],
            "report_context",
            {"evidence_nodes": len(graph.nodes), "private_memory": False},
        )
        result, usage = await ainvoke_model_with_budget(
            self.services, [HumanMessage(content=prompt)], model_name="fixture:report"
        )
        sections = deepcopy(state["report"]["sections"])
        for section in sections:
            section.content = str(result.content)
            section.status = "done"
        state = deepcopy(state)
        state["report"]["completed_sections"] = sections
        patch = await reporting.compile_final_report(
            state, self.cfg, fail_on_error=True
        )
        patch["runtime"] = {"budget": usage}
        return patch
