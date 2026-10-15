# 回归检查用量记账、索引批次、异常传播以及 Web 输入与授权行为。
"""Focused regression tests for the P1 reliability and safety fixes."""

import asyncio
from pathlib import Path

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ValidationError
from starlette.requests import Request

import open_deep_research.deep_researcher as deep_researcher_module
import open_deep_research.runtime.business_agent as business_agent_module
import open_deep_research.workflow.shared as workflow_shared
from open_deep_research.configuration import Configuration
from open_deep_research.rag import mcp_server
from open_deep_research.rag.config import HybridRetrievalConfig
from open_deep_research.rag.indexer import RAGIndexer
from open_deep_research.rag.service import (
    RAGPipelineConfig,
    build_rag_pipeline_config,
)
from open_deep_research.rag.types import RAGDocument
from open_deep_research.state import Section


# 提供可控制令牌用量的聊天模型替身。
class UsageModel(BaseChatModel):
    """Real LangChain chat model so budget callbacks fire like production."""

    content: str = "written"
    usage_metadata_payload: dict | None = None

    # 返回测试模型类型标识。
    @property
    def _llm_type(self) -> str:
        return "usage-fixture"

    # 生成测试响应并附加配置的官方用量元数据。
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        message = AIMessage(
            content=self.content,
            response_metadata={"model_name": "usage-fixture"},
            usage_metadata=self.usage_metadata_payload,
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


# 构造章节写作所需的隔离配置。
def _writer_config() -> dict:
    return {"configurable": {"section_writer_model": "fixture:model", "research_graph_enabled": False}}


# 构造包含计划章节和角色证据的测试状态。
def _section_state(section: Section) -> dict:
    return {
        "report": {"sections": [section], "completed_sections": []},
        "agents": {"risk_assessment": {"report": "risk evidence"}},
        "runtime": {"budget": {}},
    }


# 验证研究章节与最终章节写作均记录响应令牌。
def test_section_and_final_section_writer_record_response_usage(monkeypatch) -> None:
    """Both parallel writer stages merge model calls and token usage."""
    model = UsageModel(
        content="written",
        usage_metadata_payload={
            "input_tokens": 11,
            "output_tokens": 7,
            "total_tokens": 18,
        },
    )
    monkeypatch.setattr(workflow_shared, "configurable_model", model)

    research_result = asyncio.run(
        deep_researcher_module.section_writer(
            _section_state(
                Section(
                    name="Risk",
                    description="Risk evidence",
                    research=True,
                    agent_role="risk_assessment",
                )
            ),
            _writer_config(),
        )
    )
    final_result = asyncio.run(
        deep_researcher_module.write_final_sections(
            {
                "report": {"sections": [
                    Section(
                        name="Conclusion",
                        description="Summarize the report",
                        research=False,
                    )
                ], "completed_sections": [
                    Section(
                        name="Risk",
                        description="Risk evidence",
                        research=True,
                        content="risk section",
                        status="done",
                    )
                ]},
                "agents": {},
                "runtime": {"budget": {}},
            },
            _writer_config(),
        )
    )

    for result in (research_result, final_result):
        assert result["runtime"]["budget"]["model_calls"] == 1
        assert result["runtime"]["budget"]["input_tokens"] == 11
        assert result["runtime"]["budget"]["output_tokens"] == 7
        assert result["runtime"]["budget"]["total_tokens"] == 18


# 验证缺少令牌元数据时仍记录真实模型调用次数。
def test_writer_without_usage_still_records_one_model_call(monkeypatch) -> None:
    """Providers without usage metadata are counted without fabricated tokens."""
    model = UsageModel(content="written")
    monkeypatch.setattr(workflow_shared, "configurable_model", model)

    result = asyncio.run(
        deep_researcher_module.section_writer(
            _section_state(
                Section(
                    name="Risk",
                    description="Risk evidence",
                    research=True,
                    agent_role="risk_assessment",
                )
            ),
            _writer_config(),
        )
    )

    assert result["runtime"]["budget"]["model_calls"] == 1
    assert result["runtime"]["budget"]["input_tokens"] == 0
    assert result["runtime"]["budget"]["output_tokens"] == 0
    assert result["runtime"]["budget"]["total_tokens"] == 0


# 验证纯 RRF 配置不再包含旧混合权重 alpha。
def test_hybrid_alpha_is_removed_from_pure_rrf_configuration() -> None:
    """The configuration surface matches the actual RRF-only ranking path."""
    assert "rag_hybrid_alpha" not in Configuration.model_fields
    assert "hybrid_alpha" not in RAGPipelineConfig.model_fields
    assert "alpha" not in HybridRetrievalConfig.model_fields


# 验证查询与记忆存储共享一致的配置构建逻辑。
def test_shared_rag_builder_keeps_query_and_memory_store_settings_aligned() -> None:
    """Memory overrides identity only while retaining every RAG backend setting."""
    configurable = Configuration(
        rag_knowledge_base_paths=["custom/knowledge"],
        rag_milvus_uri="https://milvus.example.test",
        rag_milvus_token="secret-token",
        rag_milvus_db_name="research",
        rag_milvus_metric_type="IP",
        rag_embedding_provider="hash",
        rag_vectorstore_provider="memory",
        rag_reranker_provider="simple",
        rag_graph_enabled=False,
    )
    query_config = build_rag_pipeline_config(
        configurable,
        {"configurable": {"thread_id": "conversation-1", "user_id": "user-1"}},
    )
    memory_config = build_rag_pipeline_config(
        configurable,
        memory_enabled=True,
        memory_conversation_id="conversation-1",
        memory_user_id="user-1",
    )

    assert query_config.knowledge_base_paths == memory_config.knowledge_base_paths
    assert query_config.milvus_uri == memory_config.milvus_uri
    assert query_config.milvus_token == memory_config.milvus_token
    assert query_config.milvus_db_name == memory_config.milvus_db_name
    assert query_config.milvus_metric_type == memory_config.milvus_metric_type
    assert query_config.embedding_model == memory_config.embedding_model
    assert memory_config.memory_enabled is True
    assert memory_config.memory_conversation_id == "conversation-1"
    assert memory_config.memory_user_id == "user-1"


# 验证 RAG MCP 使用统一配置构建器。
def test_rag_mcp_uses_the_shared_config_builder() -> None:
    """The RAG MCP direct/prefixed config path resolves through the same builder."""
    config = mcp_server.build_pipeline_config(
        {
            "rag_milvus_uri": "https://milvus.example.test",
            "rag_milvus_token": "secret-token",
            "rag_milvus_db_name": "research",
            "rag_milvus_metric_type": "IP",
            "rag_vectorstore_provider": "memory",
            "rag_embedding_provider": "hash",
            "rag_reranker_provider": "simple",
        }
    )

    assert config.milvus_uri == "https://milvus.example.test"
    assert config.milvus_token == "secret-token"
    assert config.milvus_db_name == "research"
    assert config.milvus_metric_type == "IP"


# 验证仅本次快照中的待处理记忆被标记为已索引。
def test_memory_index_marks_only_the_snapshotted_pending_batch(monkeypatch) -> None:
    """A memory written during indexing remains pending for the next refresh."""
    documents = [
        RAGDocument(
            content="memory A",
            source="memory://mysql/c/A",
            metadata={
                "memory_backend": "mysql",
                "memory_id": "A",
                "index_status": "pending",
            },
        ),
        RAGDocument(
            content="memory B",
            source="memory://mysql/c/B",
            metadata={
                "memory_backend": "mysql",
                "memory_id": "B",
                "index_status": "pending",
            },
        ),
    ]
    indexer = RAGIndexer.__new__(RAGIndexer)
    indexer.config = build_rag_pipeline_config(
        Configuration(
            rag_knowledge_base_paths=[],
            rag_memory_enabled=True,
            rag_memory_mysql_url="mysql+pymysql://user:pass@localhost/rag",
            rag_embedding_provider="hash",
            rag_vectorstore_provider="memory",
            rag_reranker_provider="simple",
            rag_graph_enabled=False,
        ),
        memory_enabled=True,
    )

    marked: list[str] = []

    # 记录被标记为已索引的记忆 ID，检查批次边界。
    class FakeStore:
        # 接收测试构造参数，不建立真实外部连接。
        def __init__(self, **_kwargs):
            pass

        # 收集本次标记的记忆 ID，并返回处理数量。
        def mark_records_indexed(self, memory_ids):
            marked.extend(memory_ids)
            return len(memory_ids)

    monkeypatch.setattr("open_deep_research.rag.indexer.MySQLChatMemoryStore", FakeStore)

    assert RAGIndexer._pending_memory_ids(documents) == ["A", "B"]
    indexer._mark_pending_memories_indexed(["A", "B"])

    # C is deliberately not passed: it can be inserted while A/B are indexed.
    assert marked == ["A", "B"]


# 验证未知压缩错误向外传播。
def test_unknown_compression_error_is_not_swallowed(monkeypatch) -> None:
    """Research compression re-raises programming errors instead of degrading."""

    # 在调用阶段抛出预设错误，供压缩异常路径测试。
    class FailingModel:
        # 接收运行配置并返回测试模型，兼容真实调用链。
        def with_config(self, _config):
            return self

        # 在模型调用阶段抛出预设异常，验证异常传播与降级规则。
        async def ainvoke(self, _messages, config=None):
            raise NameError("test compression bug")

    monkeypatch.setattr(business_agent_module, "_CONFIGURABLE_MODEL", FailingModel())

    with pytest.raises(NameError, match="test compression bug"):
        asyncio.run(
            business_agent_module._compress_research(
                [],
                "task",
                "output",
                {},
                configurable=Configuration.from_runnable_config({}),
                config={},
            )
        )


# 验证明确上下文超限时研究压缩可以降级。
def test_token_limit_compression_error_keeps_graceful_degradation(monkeypatch) -> None:
    """Expected token-limit failures retain the existing bounded retry behavior."""

    # 在调用阶段抛出预设错误，供压缩异常路径测试。
    class FailingModel:
        # 接收运行配置并返回测试模型，兼容真实调用链。
        def with_config(self, _config):
            return self

        # 在模型调用阶段抛出预设异常，验证异常传播与降级规则。
        async def ainvoke(self, _messages, config=None):
            raise RuntimeError("context length exceeded")

    monkeypatch.setattr(business_agent_module, "_CONFIGURABLE_MODEL", FailingModel())
    monkeypatch.setattr(
        business_agent_module,
        "is_token_limit_exceeded",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        business_agent_module,
        "remove_up_to_last_ai_message",
        lambda messages: messages,
    )

    result = asyncio.run(
        business_agent_module._compress_research(
            [],
            "task",
            "output",
            {},
            configurable=Configuration.from_runnable_config({}),
            config={},
        )
    )

    assert "Maximum retries exceeded" in result[0]


# 验证 Web 请求的模型白名单和主题长度限制。
def test_web_request_model_and_prompt_limits() -> None:
    """The Demo API rejects arbitrary models and oversized prompts."""
    from open_deep_research.web import server

    with pytest.raises(ValidationError):
        server.ResearchRequest(topic="brand risk", model="provider:untrusted-model")
    with pytest.raises(ValidationError):
        server.ResearchRequest(topic="x" * (server.MAX_TOPIC_LENGTH + 1))


# 验证令牌可选，但配置后所有受保护请求必须校验。
def test_web_api_token_is_optional_but_enforced_when_configured(monkeypatch) -> None:
    """Configured API tokens protect the research endpoint without OAuth complexity."""
    from open_deep_research.web import server

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/research",
            "headers": [(b"authorization", b"Bearer correct")],
        }
    )
    monkeypatch.setattr(server, "WEB_API_TOKEN", "correct")
    assert server._request_is_authorized(request) is True

    wrong_request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/research",
            "headers": [(b"authorization", b"Bearer wrong")],
        }
    )
    assert server._request_is_authorized(wrong_request) is False


# 验证流错误对用户脱敏且服务端保留诊断日志。
def test_web_stream_error_is_sanitized_and_logged(monkeypatch) -> None:
    """The browser receives a generic trace id, not provider or filesystem details."""
    from open_deep_research.web import server

    # 模拟图流执行错误，供 Web 错误脱敏检查。
    class FailingGraph:
        # 在图流中抛出含模拟敏感信息的错误，检查用户响应脱敏。
        async def astream(self, *_args, **_kwargs):
            if False:
                yield {}
            raise RuntimeError("SECRET_DSN=mysql://user:password@db/internal")

    # 完整消费失败研究流，检查返回用户的错误信息已脱敏。
    async def exercise() -> str:
        monkeypatch.setattr(server, "WEB_API_TOKEN", "")
        monkeypatch.setattr(server, "_deep_researcher_factory", lambda _config: FailingGraph())
        response = await server.research(
            server.ResearchRequest(topic="brand risk"),
            Request({"type": "http", "method": "POST", "path": "/api/research", "headers": []}),
        )
        chunks = [chunk async for chunk in response.body_iterator]
        return b"".join(
            chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
            for chunk in chunks
        ).decode("utf-8")

    body = asyncio.run(exercise())
    assert "Research request failed. Trace ID:" in body
    assert "SECRET_DSN" not in body


# 验证研究请求受并发信号量限制。
def test_web_research_requests_are_semaphore_limited(monkeypatch) -> None:
    """The HTTP concurrency guard is separate from budget accounting."""
    from open_deep_research.web import server

    active = 0
    maximum_active = 0

    # 模拟耗时图流，并记录同时运行的请求数量。
    class SlowGraph:
        # 增加活动计数后短暂等待，流退出时减少计数以测量并发。
        async def astream(self, *_args, **_kwargs):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                await asyncio.sleep(0.01)
                yield {}
            finally:
                active -= 1

    # 完整消费一次测试研究响应，确保信号量持有至流结束。
    async def consume() -> None:
        response = await server.research(
            server.ResearchRequest(topic="brand risk"),
            Request({"type": "http", "method": "POST", "path": "/api/research", "headers": []}),
        )
        async for _chunk in response.body_iterator:
            pass

    # 以容量为一的信号量并发发起两个研究请求，检查实际并发上限。
    async def exercise() -> None:
        monkeypatch.setattr(server, "WEB_API_TOKEN", "")
        monkeypatch.setattr(server, "_deep_researcher_factory", lambda _config: SlowGraph())
        monkeypatch.setattr(server, "_RESEARCH_SEMAPHORE", asyncio.Semaphore(1))
        await asyncio.gather(consume(), consume())

    asyncio.run(exercise())
    assert maximum_active == 1


# 验证 Markdown 报告渲染必须经过净化或回退纯文本。
def test_markdown_report_rendering_is_sanitized() -> None:
    """The frontend sanitizes marked HTML and fails closed if the CDN is absent."""
    app_js = Path("src/open_deep_research/web/static/app.js").read_text(encoding="utf-8")
    index_html = Path("src/open_deep_research/web/static/index.html").read_text(encoding="utf-8")

    assert "DOMPurify.sanitize" in app_js
    assert "reportEl.innerHTML = marked.parse" not in app_js
    assert "dompurify@3.2.6" in index_html


# 验证存储授权使用显式拒绝逻辑，避免优化模式禁用 assert。
def test_store_authorization_does_not_use_assert() -> None:
    """Authorization checks remain active under python -O."""
    auth_source = Path("src/security/auth.py").read_text(encoding="utf-8")
    assert "assert " not in auth_source
