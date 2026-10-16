"""Offline provider fixture exercising real LangChain callbacks and LangGraph execution."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from typing import Annotated, Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from open_deep_research.budget import ainvoke_model_with_budget, merge_budget_usage
from open_deep_research.observability.langsmith import node_metadata


class FixtureChat(BaseChatModel):
    """Use the native model lifecycle, including streaming, usage and invocation params."""

    script: list[Any]
    schemas: list[dict] = []
    calls: int = 0
    delay: float = 0
    partial_error: bool = False

    @property
    def _llm_type(self):
        return "trajectory-fixture"

    def _get_invocation_params(self, stop=None, **kwargs):
        return {"model": "fixture-chat", "temperature": 0.2, "max_tokens": 1024,
                "tools": self.schemas, **kwargs}

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return ChatResult(generations=[ChatGeneration(message=item)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop, run_manager, **kwargs)

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        result = self._generate(messages, stop, run_manager, **kwargs)
        message = result.generations[0].message
        for token in str(message.content).split(" "):
            yield ChatGenerationChunk(message=AIMessageChunk(content=token + " "))
        if self.partial_error and self.calls == 1:
            raise RuntimeError("stream transport failed")
        yield ChatGenerationChunk(message=AIMessageChunk(content="", chunk_position="last",
            tool_call_chunks=[{"id": c["id"], "name": c["name"], "args": json.dumps(c["args"]), "index": i}
                              for i, c in enumerate(message.tool_calls)],
            usage_metadata=message.usage_metadata, response_metadata=message.response_metadata))

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        for chunk in self._stream(messages, stop, run_manager, **kwargs):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk


def answer(content: str, calls: list[dict] | None = None) -> AIMessage:
    return AIMessage(content=content, tool_calls=calls or [],
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        response_metadata={"model_name": "fixture-chat", "finish_reason": "tool_calls" if calls else "stop"})


class FixtureState(TypedDict):
    report: dict
    runtime: Annotated[dict, lambda a, b: {"budget": merge_budget_usage(a.get("budget"), b.get("budget"))}]


def build_graph(*, delay: float = 0, rounds: int = 1):
    """Exercise all four roles, parallel calls, nested RAG, report and accounting."""
    counts = Counter()

    @tool
    async def web_search(query: str) -> str:
        """Search public battery-safety evidence (offline fixture)."""
        counts["web_search"] += 1
        await asyncio.sleep(delay)
        return "News evidence: battery safety discussion has increased."

    @tool
    async def lookup_document(query: str) -> str:
        """Read an internal safety playbook (offline fixture)."""
        counts["lookup_document"] += 1
        await asyncio.sleep(delay)
        return "Internal playbook: verify incident claims and publish a factual FAQ."

    @tool
    async def rag_search(query: str) -> str:
        """Retrieve internal safety knowledge with a nested document tool."""
        counts["rag_search"] += 1
        return await lookup_document.ainvoke({"query": query})

    tools = {t.name: t for t in (web_search, rag_search)}
    models = {}
    for role in ("public_signal", "internal_knowledge", "risk_assessment", "response_strategy"):
        script = []
        if role in {"public_signal", "internal_knowledge"}:
            name = "web_search" if role == "public_signal" else "rag_search"
            for i in range(rounds):
                script.append(answer(f"{role} research {i}", [
                    {"id": f"{role}-{i}", "name": name, "args": {"query": "battery safety"}},
                ]))
        script.append(answer(f"{role} final findings"))
        models[role] = FixtureChat(script=script, schemas=[convert_to_openai_tool(t) for t in tools.values()],
                                  delay=delay, metadata={"ls_provider": "fixture"})

    def node(role):
        async def run(state, config):
            counts[role] += 1
            model = models[role]
            history = [SystemMessage(content=f"Analyze as {role}."), HumanMessage(content="battery safety")]
            budget = {}
            while True:
                response, delta = await ainvoke_model_with_budget(model, history, config=config)
                budget = merge_budget_usage(budget, delta)
                history.append(response)
                if not response.tool_calls:
                    break
                results = await asyncio.gather(*[tools[c["name"]].ainvoke({"type": "tool_call", **c}, config)
                                                for c in response.tool_calls])
                history.extend(results)
            return {"runtime": {"budget": budget}}
        return run

    async def report(state):
        counts["report"] += 1
        return {"report": {"final": "# Battery safety fixture report\n\nRisk verified. Publish a factual FAQ."}}

    graph = StateGraph(FixtureState)
    for role in models:
        name = f"{role}_agent"
        graph.add_node(name, node(role), metadata=node_metadata(name, kind="agent"))
    graph.add_node("compile_final_report", report)
    graph.add_edge(START, "public_signal_agent")
    graph.add_edge(START, "internal_knowledge_agent")
    graph.add_edge(["public_signal_agent", "internal_knowledge_agent"], "risk_assessment_agent")
    graph.add_edge("risk_assessment_agent", "response_strategy_agent")
    graph.add_edge("response_strategy_agent", "compile_final_report")
    graph.add_edge("compile_final_report", END)
    return graph.compile(name="trajectory_integration_fixture"), counts, models
