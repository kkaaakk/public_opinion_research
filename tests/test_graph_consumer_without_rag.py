# 验证图消费角色在没有本地 RAG 时仍可使用已存图证据生成报告。
"""Graph consumers can synthesize existing evidence without local RAG tools."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from open_deep_research.configuration import Configuration
from open_deep_research.research_graph.models import (
    Claim,
    Evidence,
    ResearchDeltaGraph,
    ResearchGraphScope,
    Source,
)
from open_deep_research.research_graph.store import (
    clear_research_graph_store_cache,
    create_research_graph_store,
)
from open_deep_research.runtime import business_agent
from open_deep_research.tools.registry import think_tool


# 验证消费角色仅有控制工具时仍能基于图证据完成报告。
@pytest.mark.parametrize("role", ["risk_assessment", "response_strategy"])
def test_graph_consumer_generates_report_without_rag(
    monkeypatch, tmp_path, role
) -> None:
    """Existing graph evidence reaches analysis and report generation without RAG."""
    run_id = f"consumer-without-rag-{role}"
    config = {
        "configurable": {
            "research_graph_enabled": True,
            "research_graph_backend": "memory",
            "research_graph_transcript_dir": str(tmp_path),
            "enabled_business_agents": [role],
            "rag_enabled": False,
            "search_api": "none",
            "research_model": "fixture:model",
            "compression_model": "fixture:model",
            "thread_id": run_id,
        }
    }
    store = create_research_graph_store(
        Configuration.from_runnable_config(config), run_id=run_id
    )
    store.write_delta(
        ResearchDeltaGraph(
            scope=ResearchGraphScope(
                run_id=run_id, role="public_signal", task_id="public-task"
            ),
            sources=[
                Source(source_id="SRC1", run_id=run_id, title="Brake safety notice")
            ],
            claims=[
                Claim(
                    claim_id="CLAIM1",
                    run_id=run_id,
                    statement="A brake issue is reported",
                )
            ],
            evidences=[
                Evidence(
                    evidence_id="EVIDENCE1",
                    run_id=run_id,
                    source_id="SRC1",
                    statement="The notice reports a brake issue",
                    supports_claim_ids=["CLAIM1"],
                )
            ],
        )
    )
    prompts = []

    # 提供只依赖预置图证据的模型替身，隔离真实网络请求。
    class FixtureModel:
        def with_structured_output(self, schema, include_raw=False):
            async def update(messages, config=None):
                return {
                    "raw": None,
                    "parsed": schema(
                        active_claim_ids=["CLAIM1"], active_evidence_ids=["EVIDENCE1"]
                    ),
                    "parsing_error": None,
                }

            return RunnableLambda(update)

        # 校验绑定工具仅包含思考工具，并返回当前模型替身。
        def bind_tools(self, tools):
            assert [tool.name for tool in tools] == ["think_tool"]
            return self

        # 提供链式重试配置接口，不执行真实请求。
        def with_retry(self, **_kwargs):
            return self

        # 接收运行配置并返回测试模型，兼容真实调用链。
        def with_config(self, _config):
            return self

        # 异步返回固定模型消息，避免真实供应商调用。
        async def ainvoke(self, messages, config=None):
            prompts.append("\n".join(str(message.content) for message in messages))
            return AIMessage(
                content="Draft grounded in EVIDENCE1 and SRC1; internal approvals are missing."
            )

    # 只返回思考控制工具，模拟缺少外部检索能力的环境。
    async def core_tools_only(*_args, **_kwargs):
        return [think_tool]

    # 阻止图消费路径意外调用旧版压缩函数。
    async def no_legacy_compression(*_args, **_kwargs):
        raise AssertionError("Graph consumers must finalize from the research graph")

    @asynccontextmanager
    async def offline_sdk(*args, **kwargs):
        def reply(call):
            prompts.append(
                str(call.system_instructions) + "\n" + json.dumps(call.input)
            )
            return [
                assistant_message(
                    "Draft grounded in EVIDENCE1 and SRC1; internal approvals are missing."
                )
            ]

        yield ScriptedModel([ModelStep.respond(reply)])

    monkeypatch.setattr(business_agent, "sdk_model", offline_sdk)
    monkeypatch.setattr(business_agent, "_CONFIGURABLE_MODEL", FixtureModel())
    monkeypatch.setattr(business_agent, "_business_agent_tools", core_tools_only)
    monkeypatch.setattr(business_agent, "_compress_research", no_legacy_compression)
    try:
        result = asyncio.run(
            business_agent.run_business_agent(
                role=role,
                state={
                    "workflow": {
                        "brief": "Assess the brake issue and propose a response",
                        "round": 1,
                    },
                    "research": {"run_id": run_id, "working_contexts": {}},
                    "agents": {},
                    "runtime": {"budget": {}},
                },
                config=config,
            )
        )
    finally:
        clear_research_graph_store_cache()
    assert len(prompts) == 2
    assert "rag_search is unavailable" in prompts[0]
    assert "Role-specific tool whitelist: think_tool." in prompts[0]
    assert all("SRC1" in prompt and "EVIDENCE1" in prompt for prompt in prompts)
    report = result["agents"][role]["report"]
    assert report["type"] == "override"
    assert "Draft grounded in EVIDENCE1 and SRC1" in report["value"]


# 验证缺少图证据时角色仍需真实研究工具。
@pytest.mark.parametrize(
    "role,graph_enabled",
    [
        ("public_signal", True),
        ("internal_knowledge", True),
        ("response_strategy", False),
    ],
)
def test_agents_without_graph_evidence_still_require_research_tools(
    monkeypatch, role, graph_enabled
) -> None:
    """Producers and standard agents cannot bypass the research-tool requirement."""

    # 只返回思考控制工具，模拟缺少外部检索能力的环境。
    async def core_tools_only(*_args, **_kwargs):
        return [SimpleNamespace(name="think_tool")]

    monkeypatch.setattr(business_agent, "_business_agent_tools", core_tools_only)
    with pytest.raises(ValueError, match="requires tool domains"):
        asyncio.run(
            business_agent.run_business_agent(
                role=role,
                state={},
                config={
                    "configurable": {
                        "enabled_business_agents": [role],
                        "research_graph_enabled": graph_enabled,
                        "research_graph_backend": "memory",
                        "rag_enabled": False,
                        "search_api": "none",
                    }
                },
            )
        )
