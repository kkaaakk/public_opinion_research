# 验证 RAG MCP 工具注册、配置兼容、引用结果和索引管理。
import asyncio
import textwrap

import pytest

from open_deep_research.rag.service import reset_rag_pipeline_cache


# 清理进程内管线缓存，隔离 MCP 测试。
@pytest.fixture(autouse=True)
def clear_rag_cache():
    reset_rag_pipeline_cache()
    yield
    reset_rag_pipeline_cache()


# 写入供 MCP 检索使用的临时知识文本。
def write_text_file(path, content: str) -> None:
    path.write_text(textwrap.dedent(content).strip(), encoding="utf-8")


# 验证服务注册查询及索引管理工具。
def test_rag_mcp_server_registers_management_tools():
    from open_deep_research.rag import mcp_server

    tools = asyncio.run(mcp_server.mcp.list_tools())
    tool_names = {tool.name for tool in tools}

    assert {
        "rag_search",
        "rag_ensure_indexed",
        "rag_index_pending_memories",
        "rag_status",
        "rag_list_sources",
        "rag_reset_cache",
    }.issubset(tool_names)


# 验证直接字段与 rag_ 前缀配置均可解析。
def test_build_pipeline_config_accepts_direct_and_rag_prefixed_keys():
    from open_deep_research.rag import mcp_server

    config = mcp_server.build_pipeline_config(
        {
            "rag_knowledge_base_paths": ["data/custom-knowledge"],
            "rag_top_k": 3,
            "embedding_provider": "hash",
            "rag_vectorstore_provider": "memory",
            "rag_reranker_provider": "simple",
            "rag_multimodal_enabled": False,
            "rag_vision_enabled": False,
        }
    )

    assert config.knowledge_base_paths == ["data/custom-knowledge"]
    assert config.top_k == 3
    assert config.embedding_provider == "hash"
    assert config.vectorstore_provider == "memory"
    assert config.reranker_provider == "simple"
    assert config.multimodal_enabled is False
    assert config.vision_enabled is False


# 验证 MCP 查询返回带来源引用的文本。
def test_rag_search_tool_returns_cited_context(tmp_path):
    from open_deep_research.rag import mcp_server

    write_text_file(
        tmp_path / "handbook.md",
        """
        # Atlas Launch Handbook

        Atlas Launch requires quarterly risk reviews and two approvals before production releases.
        """,
    )

    result = mcp_server.rag_search(
        "Which release process requires quarterly risk reviews?",
        config={
            "rag_knowledge_base_paths": [str(tmp_path)],
            "rag_embedding_provider": "hash",
            "rag_vectorstore_provider": "memory",
            "rag_reranker_provider": "simple",
            "rag_multimodal_enabled": False,
            "rag_vision_enabled": False,
            "rag_top_k": 2,
            "rag_rerank_top_n": 3,
        },
    )

    assert result["ok"] is True
    assert result["query"] == "Which release process requires quarterly risk reviews?"
    assert "quarterly risk reviews" in result["context"]
    assert result["citations"]
    assert result["citations"][0]["source"].endswith("handbook.md")


# 验证建索引、读取状态和清缓存之间的行为。
def test_rag_index_status_and_reset_cache(tmp_path):
    from open_deep_research.rag import mcp_server

    write_text_file(
        tmp_path / "policy.txt",
        "The finance release gate requires a budget owner approval.",
    )
    config = {
        "rag_knowledge_base_paths": [str(tmp_path)],
        "rag_embedding_provider": "hash",
        "rag_vectorstore_provider": "memory",
        "rag_reranker_provider": "simple",
        "rag_multimodal_enabled": False,
        "rag_vision_enabled": False,
    }

    before = mcp_server.rag_status(config=config)
    indexed = mcp_server.rag_ensure_indexed(config=config)
    after = mcp_server.rag_status(config=config)
    reset = mcp_server.rag_reset_cache()
    after_reset = mcp_server.rag_status(config=config)

    assert before["ok"] is True
    assert before["cached"] is False
    assert before["ready"] is False
    assert indexed["ok"] is True
    assert indexed["ready"] is True
    assert indexed["document_count"] == 1
    assert indexed["chunk_count"] >= 1
    assert after["cached"] is True
    assert after["ready"] is True
    assert reset["ok"] is True
    assert after_reset["cached"] is False
    assert after_reset["ready"] is False


# 验证列来源只加载文档，不触发索引构建。
def test_rag_list_sources_uses_loaders_without_indexing(tmp_path):
    from open_deep_research.rag import mcp_server

    write_text_file(
        tmp_path / "source.md",
        """
        # Support Playbook

        Escalations use the blue queue.
        """,
    )

    result = mcp_server.rag_list_sources(
        config={
            "knowledge_base_paths": [str(tmp_path)],
            "embedding_provider": "hash",
            "vectorstore_provider": "memory",
            "reranker_provider": "simple",
            "multimodal_enabled": False,
            "vision_enabled": False,
        },
        limit=5,
    )

    assert result["ok"] is True
    assert result["document_count"] == 1
    assert result["returned_count"] == 1
    assert result["sources"][0]["source"].endswith("source.md")
    assert result["sources"][0]["title"] == "Support Playbook"
