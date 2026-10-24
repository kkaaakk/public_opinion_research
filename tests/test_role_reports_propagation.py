# 验证完整角色报告跨阶段传递，并与私有短期记忆和滚动摘要保持各自职责。
"""Regression tests for complete public-opinion role-report propagation."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from agents.testing import ModelStep, ScriptedModel, assistant_message
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from reference_workflow import PythonWorkflow
from test_public_opinion_research_loop import _ReviewModel
from test_sdk_runtime import _tool

import open_deep_research.deep_researcher as deep_researcher_module
import open_deep_research.runtime.business_agent as business_agent_module
import open_deep_research.workflow.research as workflow_research
import open_deep_research.workflow.shared as workflow_shared
from open_deep_research.configuration import Configuration
from open_deep_research.state import ResearchReview, Section, agents_reducer
from open_deep_research.workflow import reporting


# 构造报告传播测试使用的舆情配置。
def _public_opinion_config(*roles: str) -> dict:
    return {
        "configurable": {
            "research_graph_enabled": False,
            "thread_id": "role-report-fixture",
            "enabled_business_agents": list(roles),
        }
    }


# 验证精简记忆不会替代正式完整报告。
def test_full_role_report_is_not_replaced_by_compact_memory(monkeypatch) -> None:
    """A long formal report stays complete while its private memory is bounded."""
    full_report_body = "A" * 5_000 + "\nCOMPLETE_REPORT_TAIL"

    # 提供可记录配置或返回固定消息的模型替身。
    class FakeModel:
        # 提供工具绑定测试接口，保留链式模型调用。
        def bind_tools(self, _tools):
            return self

        # 提供链式重试配置接口，不执行真实请求。
        def with_retry(self, **_kwargs):
            return self

        # 接收运行配置并返回测试模型，兼容真实调用链。
        def with_config(self, _config):
            return self

        # 异步返回固定模型消息，避免真实供应商调用。
        async def ainvoke(self, _messages, config=None):
            return AIMessage(content="agent step")

    # 返回固定网页工具，避免初始化真实搜索与 MCP 服务。
    async def fake_tools(_config, _role, _spec=None):
        return [_tool("public_signal")]

    # 返回预设完整报告及预算增量，检查正式报告的传播。
    async def fake_compress(*_args, **_kwargs):
        return full_report_body, [], {}

    @asynccontextmanager
    async def offline_model(*args, **kwargs):
        yield ScriptedModel([[assistant_message("agent step")]])

    monkeypatch.setattr(business_agent_module, "sdk_model", offline_model)
    monkeypatch.setattr(business_agent_module, "_CONFIGURABLE_MODEL", FakeModel())
    monkeypatch.setattr(business_agent_module, "_business_agent_tools", fake_tools)
    monkeypatch.setattr(business_agent_module, "_compress_research", fake_compress)

    result = asyncio.run(
        business_agent_module.run_business_agent(
            state={
                "workflow": {"brief": "brand risk", "round": 1},
                "agents": {},
                "research": {"working_contexts": {}},
                "runtime": {"budget": {}},
            },
            config=_public_opinion_config("public_signal"),
            role="public_signal",
        )
    )

    formal_report = result["agents"]["public_signal"]["report"]
    private_memory = result["agents"]["public_signal"]["memory"][0]["content"]
    assert full_report_body in formal_report
    assert len(formal_report) > 1_800
    assert len(private_memory) < len(formal_report)
    assert "COMPLETE_REPORT_TAIL" not in private_memory


# 验证角色执行保留私有记忆和滚动摘要。
def test_business_agent_preserves_private_memory_and_rolling_summary(
    monkeypatch,
) -> None:
    """The assembly entry keeps agent-owned dynamic context and summary state."""
    captured_messages = []

    # 记录输入提示或消息，并返回固定报告以检查证据传递。
    class CapturingModel:
        # 提供工具绑定测试接口，保留链式模型调用。
        def bind_tools(self, _tools):
            return self

        # 提供链式重试配置接口，不执行真实请求。
        def with_retry(self, **_kwargs):
            return self

        # 接收运行配置并返回测试模型，兼容真实调用链。
        def with_config(self, _config):
            return self

        # 记录模型实际输入，并返回预设章节或报告消息。
        async def ainvoke(self, messages, config=None):
            captured_messages.extend(messages)
            return AIMessage(content="done")

    # 返回固定网页工具，避免初始化真实搜索与 MCP 服务。
    async def fake_tools(_config, _role, _spec=None):
        return [_tool("public_signal")]

    # 返回预设完整报告及预算增量，检查正式报告的传播。
    async def fake_compress(*_args, **_kwargs):
        return "report", [], {"model_calls": 1}

    @asynccontextmanager
    async def offline_model(*args, **kwargs):
        def reply(call):
            captured_messages.append(AIMessage(content=json.dumps(call.input)))
            return [assistant_message("done")]

        yield ScriptedModel([ModelStep.respond(reply)])

    monkeypatch.setattr(business_agent_module, "sdk_model", offline_model)
    monkeypatch.setattr(business_agent_module, "_CONFIGURABLE_MODEL", CapturingModel())
    monkeypatch.setattr(business_agent_module, "_business_agent_tools", fake_tools)
    monkeypatch.setattr(business_agent_module, "_compress_research", fake_compress)

    result = asyncio.run(
        business_agent_module.run_business_agent(
            role="public_signal",
            state={
                "workflow": {"brief": "brand risk", "round": 1},
                "agents": {
                    "public_signal": {
                        "memory": [{"content": "PRIVATE_SENTINEL"}],
                        "rolling_summary": "PRIOR_SUMMARY",
                    }
                },
                "research": {"working_contexts": {}},
                "runtime": {"budget": {}},
            },
            config=_public_opinion_config("public_signal"),
        )
    )

    assert (
        sum("PRIVATE_SENTINEL" in str(message.content) for message in captured_messages)
        == 1
    )
    assert result["agents"]["public_signal"]["rolling_summary"] == "PRIOR_SUMMARY"
    assert result["runtime"]["budget"]["model_calls"] >= 1


# 验证并行报告按角色分别合并。
def test_parallel_role_reports_merge_by_role() -> None:
    """Concurrent partial updates retain every role's complete report."""
    merged = agents_reducer(
        {},
        {"public_signal": {"report": "PUBLIC_SIGNAL_FULL"}},
    )
    merged = agents_reducer(
        merged,
        {"internal_knowledge": {"report": "INTERNAL_KNOWLEDGE_FULL"}},
    )

    assert merged == {
        "public_signal": {"report": "PUBLIC_SIGNAL_FULL"},
        "internal_knowledge": {"report": "INTERNAL_KNOWLEDGE_FULL"},
    }


# 验证子图向下游风险及策略角色提供完整上游报告。
def test_public_opinion_subgraph_keeps_full_reports_for_downstream_agents(
    monkeypatch,
) -> None:
    """The compiled subgraph applies the report reducer between agent stages."""
    reports = {
        "public_signal": "P" * 2_000 + "PUBLIC_SIGNAL_TAIL",
        "internal_knowledge": "I" * 2_000 + "INTERNAL_KNOWLEDGE_TAIL",
        "risk_assessment": "R" * 2_000 + "RISK_ASSESSMENT_TAIL",
        "response_strategy": "S" * 2_000 + "RESPONSE_STRATEGY_TAIL",
    }
    seen_states: dict[str, dict[str, str]] = {}

    # 记录角色看到的上游状态，再返回当前角色报告和精简记忆。
    async def fake_agent(*, state, config, role):
        del config
        seen_states[role] = {
            name: value.get("report", "")
            for name, value in state.get("agents", {}).items()
        }
        return {
            "agents": {
                role: {
                    "report": reports[role],
                    "memory": [{"content": reports[role][:1_800]}],
                }
            },
            "runtime": {"budget": {}},
        }

    # 固定返回证据充分的复核结论，隔离真实研究循环模型。
    class FakeReviewModel:
        # 将测试结构化响应函数包装为 Runnable，兼容真实输出链。
        def with_structured_output(self, _schema, include_raw=False):
            return RunnableLambda(self._structured_call)

        # 提供链式重试配置接口，不执行真实请求。
        def with_retry(self, **_kwargs):
            return self

        # 接收运行配置并返回测试模型，兼容真实调用链。
        def with_config(self, _config):
            return self

        # 返回包含原始响应、解析结果和错误字段的结构化测试响应。
        async def _structured_call(self, _messages, config=None):
            return {
                "raw": None,
                "parsed": ResearchReview(research_complete=True),
                "parsing_error": None,
            }

    monkeypatch.setattr(workflow_research, "run_business_agent", fake_agent)
    monkeypatch.setattr(workflow_shared, "configurable_model", FakeReviewModel())

    async def no_report(*args, **kwargs):
        return {}

    for name in ("section_writer", "write_final_sections", "compile_final_report"):
        monkeypatch.setattr(reporting, name, no_report)
    result = asyncio.run(
        PythonWorkflow().ainvoke(
            {
                "messages": [],
                "workflow": {
                    "brief": "brand risk",
                    "round": 1,
                    "review": None,
                    "pending_tasks": [],
                    "completed_tasks": [],
                },
                "agents": {},
                "research": {"working_contexts": {}},
                "report": {},
                "runtime": {"budget": {}, "metrics": {}},
            },
            _public_opinion_config(*reports),
            start_at="research",
        )
    )

    assert {
        role: value["report"] for role, value in result["agents"].items()
    } == reports
    assert seen_states["risk_assessment"]["public_signal"] == reports["public_signal"]
    assert (
        seen_states["risk_assessment"]["internal_knowledge"]
        == reports["internal_knowledge"]
    )
    assert (
        seen_states["response_strategy"]["risk_assessment"]
        == reports["risk_assessment"]
    )


# 验证风险任务说明包含完整上游证据报告。
def test_risk_assessment_assignment_uses_full_upstream_reports() -> None:
    """Risk assessment receives evidence after the memory truncation boundary."""
    public_signal_report = "A" * 1_800 + "CRITICAL_EVIDENCE_AT_END"
    prompt = business_agent_module._build_business_agent_assignment(
        {
            "workflow": {"brief": "brand risk", "round": 1},
            "agents": {
                "public_signal": {
                    "report": public_signal_report,
                    "memory": [
                        {"content": public_signal_report[:1_800] + "\n[truncated]"}
                    ],
                },
                "internal_knowledge": {"report": "INTERNAL_FACTS", "memory": []},
            },
        },
        "risk_assessment",
        configurable=Configuration(research_graph_enabled=False),
    )

    assert "CRITICAL_EVIDENCE_AT_END" in prompt


# 验证回应策略任务说明保留完整风险报告。
def test_response_strategy_assignment_uses_full_risk_report() -> None:
    """Response strategy receives the complete risk assessment output."""
    risk_report = "R" * 1_800 + "HIGH_PRIORITY_RESPONSE_ACTION"
    prompt = business_agent_module._build_business_agent_assignment(
        {
            "workflow": {"brief": "brand risk", "round": 1},
            "agents": {
                "public_signal": {"report": "PUBLIC_SIGNAL_FULL"},
                "internal_knowledge": {"report": "INTERNAL_FACTS"},
                "risk_assessment": {
                    "report": risk_report,
                    "memory": [{"content": risk_report[:1_800] + "\n[truncated]"}],
                },
            },
        },
        "response_strategy",
        configurable=Configuration(research_graph_enabled=False),
    )

    assert "HIGH_PRIORITY_RESPONSE_ACTION" in prompt


# 验证章节证据优先选择正式报告而非短期记忆。
def test_section_writer_prefers_full_role_report_over_memory(monkeypatch) -> None:
    """Section writing uses formal role reports even when memory omits the tail."""
    captured_prompts: list[str] = []

    # 记录输入提示或消息，并返回固定报告以检查证据传递。
    class CapturingModel:
        # 接收运行配置并返回测试模型，兼容真实调用链。
        def with_config(self, _config):
            return self

        # 记录模型实际输入，并返回预设章节或报告消息。
        async def ainvoke(self, messages, config=None):
            captured_prompts.append(str(messages[0].content))
            return SimpleNamespace(content="section output")

    full_report = "A" * 3_000 + "FINAL_RISK_EVIDENCE"
    monkeypatch.setattr(workflow_shared, "configurable_model", CapturingModel())

    asyncio.run(
        deep_researcher_module.section_writer(
            {
                "report": {
                    "sections": [
                        Section(
                            name="Risk evidence",
                            description="Summarize risk evidence.",
                            research=True,
                            agent_role="risk_assessment",
                        )
                    ]
                },
                "agents": {
                    "risk_assessment": {
                        "report": full_report,
                        "memory": [{"content": full_report[:1_800] + "\n[truncated]"}],
                    }
                },
                "runtime": {"budget": {}},
            },
            {
                "configurable": {
                    "section_writer_model": "fixture:model",
                    "research_graph_enabled": False,
                }
            },
        )
    )

    assert captured_prompts
    assert "FINAL_RISK_EVIDENCE" in captured_prompts[0]


# 验证研究阶段正确传回子图角色报告。
def test_research_phase_propagates_subgraph_role_reports(monkeypatch):
    """The shared reducer forwards complete reports and bounded role memory."""
    full_report = "P" * 4_000 + "\nSUBGRAPH_REPORT_TAIL"

    async def fake_agent(*, state, config, role):
        if role != "public_signal":
            return {}
        assert state["agents"] == {}
        return {
            "agents": {
                role: {
                    "report": full_report,
                    "memory": [{"content": full_report[:1800] + "\n[truncated]"}],
                }
            }
        }

    async def no_report(*args, **kwargs):
        return {}

    monkeypatch.setattr(workflow_research, "run_business_agent", fake_agent)
    monkeypatch.setattr(
        workflow_shared,
        "configurable_model",
        _ReviewModel([ResearchReview(research_complete=True)]),
    )
    for name in ("section_writer", "write_final_sections", "compile_final_report"):
        monkeypatch.setattr(reporting, name, no_report)
    result = asyncio.run(
        PythonWorkflow().ainvoke(
            {
                "messages": [],
                "workflow": {"brief": "brand risk"},
                "agents": {},
                "research": {},
                "report": {},
                "runtime": {"budget": {}},
            },
            _public_opinion_config("public_signal"),
            start_at="research",
        )
    )
    agent = result["agents"]["public_signal"]
    assert agent["report"] == full_report
    assert len(agent["report"]) == 4021
    assert agent["memory"][0]["content"].endswith("[truncated]")


# 验证兜底报告读取正式角色报告。
def test_final_report_fallback_reads_formal_role_reports(monkeypatch) -> None:
    """The final-report fallback also receives the complete formal reports."""
    captured_prompts: list[str] = []

    # 记录输入提示或消息，并返回固定报告以检查证据传递。
    class CapturingModel:
        # 接收运行配置并返回测试模型，兼容真实调用链。
        def with_config(self, _config):
            return self

        # 记录模型实际输入，并返回预设章节或报告消息。
        async def ainvoke(self, messages, config=None):
            captured_prompts.append(str(messages[0].content))
            return SimpleNamespace(content="final report")

    monkeypatch.setattr(workflow_shared, "configurable_model", CapturingModel())
    full_report = "A" * 3_000 + "FINAL_REPORT_EVIDENCE"
    asyncio.run(
        deep_researcher_module._fallback_report_generation(
            {
                "agents": {
                    "risk_assessment": {
                        "report": full_report,
                        "memory": [{"content": "compact memory only"}],
                    }
                },
                "messages": [],
                "workflow": {"brief": "brand risk"},
                "runtime": {"budget": {}},
            },
            {
                "configurable": {
                    "research_graph_enabled": False,
                    "final_report_model": "fixture:model",
                    "rag_memory_write_enabled": False,
                }
            },
        )
    )

    assert captured_prompts
    assert "FINAL_REPORT_EVIDENCE" in captured_prompts[0]
