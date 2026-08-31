"""Focused regression tests for the P1 reliability and safety fixes."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from starlette.requests import Request

import open_deep_research.deep_researcher as deep_researcher_module
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


class UsageModel:
    """Minimal async model fixture that returns a predefined response."""

    def __init__(self, response):
        self.response = response
        self.prompts: list[str] = []

    def with_config(self, _config):
        return self

    async def ainvoke(self, messages):
        self.prompts.append(str(messages[0].content))
        return self.response


def _writer_config() -> dict:
    return {"configurable": {"section_writer_model": "fixture:model"}}


def _section_state(section: Section) -> dict:
    return {
        "sections": [section],
        "role_reports": {"risk_assessment": "risk evidence"},
        "agent_memories": {},
        "completed_sections": [],
        "budget_usage": {},
    }


def test_section_and_final_section_writer_record_response_usage(monkeypatch) -> None:
    """Both parallel writer stages merge model calls and token usage."""
    response = AIMessage(
        content="written",
        usage_metadata={
            "input_tokens": 11,
            "output_tokens": 7,
            "total_tokens": 18,
        },
    )
    model = UsageModel(response)
    monkeypatch.setattr(deep_researcher_module, "configurable_model", model)

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
                "sections": [
                    Section(
                        name="Conclusion",
                        description="Summarize the report",
                        research=False,
                    )
                ],
                "completed_sections": [
                    Section(
                        name="Risk",
                        description="Risk evidence",
                        research=True,
                        content="risk section",
                        status="done",
                    )
                ],
                "role_reports": {},
                "budget_usage": {},
            },
            _writer_config(),
        )
    )

    for result in (research_result, final_result):
        assert result["budget_usage"]["model_calls"] == 1
        assert result["budget_usage"]["input_tokens"] == 11
        assert result["budget_usage"]["output_tokens"] == 7
        assert result["budget_usage"]["total_tokens"] == 18


def test_writer_without_usage_still_records_one_model_call(monkeypatch) -> None:
    """Providers without usage metadata are counted without fabricated tokens."""
    model = UsageModel(SimpleNamespace(content="written"))
    monkeypatch.setattr(deep_researcher_module, "configurable_model", model)

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

    assert result["budget_usage"]["model_calls"] == 1
    assert result["budget_usage"]["input_tokens"] == 0
    assert result["budget_usage"]["output_tokens"] == 0
    assert result["budget_usage"]["total_tokens"] == 0


def test_hybrid_alpha_is_removed_from_pure_rrf_configuration() -> None:
    """The configuration surface matches the actual RRF-only ranking path."""
    assert "rag_hybrid_alpha" not in Configuration.model_fields
    assert "hybrid_alpha" not in RAGPipelineConfig.model_fields
    assert "alpha" not in HybridRetrievalConfig.model_fields


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

    class FakeStore:
        def __init__(self, **_kwargs):
            pass

        def mark_records_indexed(self, memory_ids):
            marked.extend(memory_ids)
            return len(memory_ids)

    monkeypatch.setattr("open_deep_research.rag.indexer.MySQLChatMemoryStore", FakeStore)

    assert RAGIndexer._pending_memory_ids(documents) == ["A", "B"]
    indexer._mark_pending_memories_indexed(["A", "B"])

    # C is deliberately not passed: it can be inserted while A/B are indexed.
    assert marked == ["A", "B"]


def test_unknown_compression_error_is_not_swallowed(monkeypatch) -> None:
    """Research compression re-raises programming errors instead of degrading."""

    class FailingModel:
        def with_config(self, _config):
            return self

        async def ainvoke(self, _messages):
            raise NameError("test compression bug")

    monkeypatch.setattr(deep_researcher_module, "configurable_model", FailingModel())

    with pytest.raises(NameError, match="test compression bug"):
        asyncio.run(
            deep_researcher_module.compress_research(
                {"researcher_messages": [], "budget_usage": {}}, {}
            )
        )


def test_token_limit_compression_error_keeps_graceful_degradation(monkeypatch) -> None:
    """Expected token-limit failures retain the existing bounded retry behavior."""

    class FailingModel:
        def with_config(self, _config):
            return self

        async def ainvoke(self, _messages):
            raise RuntimeError("context length exceeded")

    monkeypatch.setattr(deep_researcher_module, "configurable_model", FailingModel())
    monkeypatch.setattr(
        deep_researcher_module,
        "is_token_limit_exceeded",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        deep_researcher_module,
        "remove_up_to_last_ai_message",
        lambda messages: messages,
    )

    result = asyncio.run(
        deep_researcher_module.compress_research(
            {"researcher_messages": [], "budget_usage": {}}, {}
        )
    )

    assert "Maximum retries exceeded" in result["compressed_research"]


def test_web_request_model_and_prompt_limits() -> None:
    """The Demo API rejects arbitrary models and oversized prompts."""
    from open_deep_research.web import server

    with pytest.raises(ValidationError):
        server.ResearchRequest(topic="brand risk", model="provider:untrusted-model")
    with pytest.raises(ValidationError):
        server.ResearchRequest(topic="x" * (server.MAX_TOPIC_LENGTH + 1))


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


def test_web_stream_error_is_sanitized_and_logged(monkeypatch) -> None:
    """The browser receives a generic trace id, not provider or filesystem details."""
    from open_deep_research.web import server

    class FailingGraph:
        async def astream(self, *_args, **_kwargs):
            if False:
                yield {}
            raise RuntimeError("SECRET_DSN=mysql://user:password@db/internal")

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


def test_web_research_requests_are_semaphore_limited(monkeypatch) -> None:
    """The HTTP concurrency guard is separate from budget accounting."""
    from open_deep_research.web import server

    active = 0
    maximum_active = 0

    class SlowGraph:
        async def astream(self, *_args, **_kwargs):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                await asyncio.sleep(0.01)
                yield {}
            finally:
                active -= 1

    async def consume() -> None:
        response = await server.research(
            server.ResearchRequest(topic="brand risk"),
            Request({"type": "http", "method": "POST", "path": "/api/research", "headers": []}),
        )
        async for _chunk in response.body_iterator:
            pass

    async def exercise() -> None:
        monkeypatch.setattr(server, "WEB_API_TOKEN", "")
        monkeypatch.setattr(server, "_deep_researcher_factory", lambda _config: SlowGraph())
        monkeypatch.setattr(server, "_RESEARCH_SEMAPHORE", asyncio.Semaphore(1))
        await asyncio.gather(consume(), consume())

    asyncio.run(exercise())
    assert maximum_active == 1


def test_markdown_report_rendering_is_sanitized() -> None:
    """The frontend sanitizes marked HTML and fails closed if the CDN is absent."""
    app_js = Path("src/open_deep_research/web/static/app.js").read_text(encoding="utf-8")
    index_html = Path("src/open_deep_research/web/static/index.html").read_text(encoding="utf-8")

    assert "DOMPurify.sanitize" in app_js
    assert "reportEl.innerHTML = marked.parse" not in app_js
    assert "dompurify@3.2.6" in index_html


def test_store_authorization_does_not_use_assert() -> None:
    """Authorization checks remain active under python -O."""
    auth_source = Path("src/security/auth.py").read_text(encoding="utf-8")
    assert "assert " not in auth_source
