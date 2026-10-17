"""Explicitly enabled live SDK/Graph smoke using synthetic local evidence only."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

from agents import Agent, ModelSettings, RunConfig
from langchain_core.callbacks import BaseCallbackHandler

from open_deep_research.configuration import Configuration
from open_deep_research.models.factory import create_chat_model, parse_model_ref
from open_deep_research.models.sdk import sdk_model
from open_deep_research.research_graph import (
    InMemoryResearchGraphStore,
    ResearchGraphConsumerStrategy,
    ResearchGraphProducerStrategy,
    ResearchWorkspace,
)
from open_deep_research.research_graph.workspace import TaskDescriptor
from open_deep_research.runtime.sdk_context import SDKContextHarness

LOGGER = logging.getLogger(__name__)


def _usage(result: Any) -> dict[str, int]:
    usage = result.context_wrapper.usage
    return {
        "requests": usage.requests,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "reasoning_tokens": usage.output_tokens_details.reasoning_tokens,
    }


class _GraphCalls(BaseCallbackHandler):
    """Capture graph tool arguments and usage, excluding reasoning text."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        for generation in response.generations:
            message = generation[0].message
            self.calls.append(
                {
                    "finish_reason": message.response_metadata.get("finish_reason"),
                    "usage": message.usage_metadata,
                    "tool_calls": message.tool_calls,
                    "content_chars": len(str(message.content)),
                }
            )


class _ObservedHarness(SDKContextHarness):
    """Inspect the public model boundary without replacing SDK model execution."""

    def __init__(self, workspace: ResearchWorkspace, **kwargs: Any) -> None:
        super().__init__(workspace, **kwargs)
        self.requests: list[dict[str, Any]] = []
        self.initial_working_context = workspace.working_context.model_dump()

    async def on_llm_start(
        self, context: Any, agent: Any, system_prompt: Any, input_items: Any
    ) -> None:
        await super().on_llm_start(context, agent, system_prompt, input_items)
        outputs = [
            item for item in input_items if item.get("type") == "function_call_output"
        ]
        context_text = "\n".join(
            str(item.get("content", ""))
            for item in input_items
            if item.get("role") == "user"
        )
        self.requests.append(
            {
                "turn": self.model_calls,
                "output_count": len(outputs),
                "compacted_outputs": sum(
                    str(item.get("output", "")).startswith(
                        "[Research result compacted."
                    )
                    for item in outputs
                ),
                "evidence_in_context": "[Evidence]" in context_text,
                "context_blocks": context_text.count("<SDK Research Context>"),
                "working_progress": bool(
                    self.workspace.working_context.recent_progress
                ),
                "working_context_changed": self.workspace.working_context.model_dump()
                != self.initial_working_context,
            }
        )


def _workspace(
    *,
    model_ref: str,
    run_id: str,
    producer: bool,
    store: Any,
    config: Configuration,
    model_factory: Any,
) -> ResearchWorkspace:
    role = "public_signal" if producer else "risk_assessment"
    return ResearchWorkspace(
        strategy=ResearchGraphProducerStrategy()
        if producer
        else ResearchGraphConsumerStrategy(),
        research_state={},
        role=role,
        run_id=run_id,
        task=TaskDescriptor(
            task_id=f"{role}-live-smoke",
            objective="Verify synthetic Orion service notices A and B using cited evidence.",
            evidence_needed="Notice A and notice B with source and evidence IDs.",
        ),
        configurable=config,
        store=store,
        model_factory=model_factory,
    )


async def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    """Call live SDK and graph models; keep tool data and graph storage local."""
    from dotenv import load_dotenv

    load_dotenv()
    model_ref = args.model
    run_id = "sdk-live-" + uuid4().hex
    store = InMemoryResearchGraphStore()
    graph_models: list[Any] = []
    graph_calls = _GraphCalls()

    def graph_model_factory(name: str, max_tokens: int | None) -> Any:
        model = create_chat_model(
            name,
            max_tokens=max_tokens,
            timeout=45,
            max_retries=0,
            callbacks=[graph_calls],
        )
        graph_models.append(model)
        return model

    config = Configuration(
        research_graph_enabled=True,
        research_graph_backend="memory",
        research_model=model_ref,
        research_graph_extraction_model=model_ref,
        context_manager_model=model_ref,
        research_graph_extraction_model_max_tokens=4096,
        context_manager_model_max_tokens=2048,
        max_structured_output_retries=1,
        recent_raw_steps=0,
        research_graph_max_retrieved_nodes=16,
        research_graph_max_retrieved_edges=24,
        research_graph_transcript_dir=str(Path(args.output).parent / "transcripts"),
    )
    producer_workspace = _workspace(
        model_ref=model_ref,
        run_id=run_id,
        producer=True,
        store=store,
        config=config,
        model_factory=graph_model_factory,
    )
    producer = _ObservedHarness(
        producer_workspace,
        evidence_tools=frozenset({"lookup_notice"}),
        settlement_timeout=60,
    )
    started: set[str] = set()
    both_started = asyncio.Event()

    async def lookup_notice(context: Any, arguments: str) -> str:
        notice_id = json.loads(arguments)["notice_id"]
        if notice_id not in {"A", "B"}:
            raise ValueError("Unknown synthetic notice ID.")
        started.add(notice_id)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=3)
        content = (
            "SYNTHETIC TEST FIXTURE ONLY. Orion service campaign starts 2026-10-01 for batch X100."
            if notice_id == "A"
            else "SYNTHETIC TEST FIXTURE ONLY. Orion notice B confirms the service campaign covers batch X100 and requires inspection."
        )
        return json.dumps(
            {
                "type": "research_raw_search",
                "results": [
                    {
                        "url": f"https://example.test/sdk-phase0/notice-{notice_id.lower()}",
                        "title": f"Synthetic Orion notice {notice_id}",
                        "content": content,
                    }
                ],
            }
        )

    tool = producer.function_tool(
        name="lookup_notice",
        description="Read one synthetic Orion validation notice. A and B are independent; request both together before answering.",
        params_json_schema={
            "type": "object",
            "properties": {"notice_id": {"type": "string", "enum": ["A", "B"]}},
            "required": ["notice_id"],
            "additionalProperties": False,
        },
        invoke=lookup_notice,
    )
    settings = ModelSettings(max_tokens=2048, parallel_tool_calls=True)
    if parse_model_ref(model_ref).provider == "deepseek":
        settings.extra_body = {"thinking": {"type": args.thinking}}
        if args.thinking == "enabled":
            settings.extra_body["reasoning_effort"] = "low"
    run_config = RunConfig(tracing_disabled=True, trace_include_sensitive_data=False)
    try:
        async with sdk_model(model_ref, api="chat_completions") as model:
            producer_result = await producer.run(
                Agent(
                    name="sdk-live-producer",
                    model=model,
                    tools=[tool],
                    model_settings=settings,
                    instructions="This is a synthetic integration test. In your FIRST response call lookup_notice twice, once with A and once with B, in the SAME tool-call batch. Do not answer before both calls. After their outputs and Research Graph context arrive, stop calling tools and report one short sentence citing the provided SRC and EVD IDs. Do not invent IDs or claim these are real notices.",
                ),
                "Validate both independent synthetic notices A and B; request both now in one batch.",
                run_config=run_config,
                max_turns=3,
            )
            assert len(producer.results) == 2, (
                "Model did not execute exactly two evidence calls."
            )
            assert all(
                result.status == "succeeded" and result.consumed
                for result in producer.results.values()
            ), "Tool or evidence settlement failed."
            assert ("batch_start", "2") in producer.events, (
                "Real provider did not produce a unified parallel batch."
            )
            assert any(
                request["compacted_outputs"] == 2
                and request["evidence_in_context"]
                and request["working_progress"]
                for request in producer.requests[1:]
            ), (
                "Updated graph context/receipts were not available before the next live model call."
            )
            assert all(
                request["context_blocks"] == 1 for request in producer.requests
            ), "Duplicate context injection."
            counts = Counter(node.node_type for node in store.nodes.values())
            relation_counts = Counter(
                edge.relation_type for edge in store.edges.values()
            )
            assert counts["Source"] == 2 and counts["Evidence"] >= 2, (
                "Graph extraction did not persist both sources and evidence."
            )
            assert (
                relation_counts["EXTRACTED_FROM"] >= 2
                and relation_counts["SUPPORTS"] >= 2
                and relation_counts["DERIVED_FROM"] >= 1
                and relation_counts["SUPPORTED_BY"] >= 1
            ), "Graph provenance chain is incomplete."
            consumer_workspace = _workspace(
                model_ref=model_ref,
                run_id=run_id,
                producer=False,
                store=store,
                config=config,
                model_factory=graph_model_factory,
            )
            consumer = _ObservedHarness(consumer_workspace)
            writes_before = dict(counts)
            consumer_result = await consumer.run(
                Agent(
                    name="sdk-live-consumer",
                    model=model,
                    model_settings=ModelSettings(
                        max_tokens=1024, extra_body=settings.extra_body
                    ),
                    instructions="Use the provided Research Graph context to summarize the two synthetic Orion notices in one sentence. Cite supplied SRC and EVD IDs. Call no tools; invent no facts or IDs.",
                ),
                "Read existing Orion evidence and cite it.",
                run_config=run_config,
                max_turns=1,
            )
            assert consumer.requests[0]["evidence_in_context"], (
                "Consumer did not retrieve evidence before its first live request."
            )
            assert (
                dict(Counter(node.node_type for node in store.nodes.values()))
                == writes_before
            ), "Consumer changed the graph."
            assert not consumer.results and not any(
                event[0] == "batch_start" for event in consumer.events
            )
            persisted_ids = {node.node_id for node in store.nodes.values()}
            for result in [producer_result, consumer_result]:
                cited_ids = set(
                    re.findall(r"(?:SRC|EVD)_[A-Za-z0-9]+", str(result.final_output))
                )
                assert cited_ids and cited_ids.issubset(persisted_ids), (
                    "Live model cited missing graph evidence."
                )
            metrics = producer_workspace.metrics.as_dict()
            summary = {
                "status": "passed",
                "model": model_ref,
                "thinking": args.thinking,
                "run_id": run_id,
                "graph_backend": "memory",
                "tool_data": "synthetic local fixtures",
                "producer_requests": producer.requests,
                "consumer_requests": consumer.requests,
                "graph_node_counts": dict(counts),
                "graph_relation_counts": dict(relation_counts),
                "graph_extraction_calls": metrics.get("graph_extraction_calls"),
                "context_manager_calls": metrics.get("context_manager_calls"),
                "producer_sdk_usage": _usage(producer_result),
                "consumer_sdk_usage": _usage(consumer_result),
                "graph_budget_usage": producer.budget_usage,
                "producer_output": str(producer_result.final_output),
                "consumer_output": str(consumer_result.final_output),
                "events": producer.events,
                "producer_settled": producer.last_settlement.succeeded,
                "consumer_settled": consumer.last_settlement.succeeded,
            }
            return summary
    finally:
        diagnostic = {
            "model": model_ref,
            "thinking": args.thinking,
            "run_id": run_id,
            "producer_requests": producer.requests,
            "events": producer.events,
            "graph_node_counts": dict(
                Counter(node.node_type for node in store.nodes.values())
            ),
            "graph_relation_counts": dict(
                Counter(edge.relation_type for edge in store.edges.values())
            ),
            "working_context": producer_workspace.working_context.model_dump(),
            "graph_metrics": producer_workspace.metrics.as_dict(),
            "graph_model_calls": graph_calls.calls,
            "receipts": {
                key: value.model_dump()
                for key, value in producer_workspace.receipts.items()
            },
        }
        diagnostic_path = Path(args.output).with_suffix(".diagnostic.json")
        diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
        diagnostic_path.write_text(
            json.dumps(diagnostic, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        for graph_model in graph_models:
            async_client = getattr(graph_model, "root_async_client", None)
            if async_client is not None:
                await async_client.close()
            sync_client = getattr(graph_model, "root_client", None)
            if sync_client is not None:
                sync_client.close()
        store.close()


def main() -> None:
    """Require explicit paid opt-in and write a local, inspectable result summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-real-api", action="store_true")
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--thinking", choices=["disabled", "enabled"], default="disabled"
    )
    parser.add_argument("--output", default=".tmp/agents-phase0/real-sdk-context.json")
    args = parser.parse_args()
    if not args.enable_real_api:
        parser.error("Real API smoke is disabled; pass --enable-real-api to opt in.")
    LOGGER.addHandler(logging.StreamHandler())
    LOGGER.setLevel(logging.INFO)
    try:
        summary = asyncio.run(asyncio.wait_for(run_smoke(args), timeout=240))
    except Exception as exc:
        summary = {
            "status": "failed",
            "exception_type": type(exc).__name__,
            "http_status": getattr(exc, "status_code", None),
        }
        if isinstance(exc, AssertionError):
            summary["assertion"] = str(exc)
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    LOGGER.info("SDK context live smoke: %s; summary=%s", summary["status"], path)
    if summary["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
