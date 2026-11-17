# 导出本地 RAG 的配置与查询入口，并延迟解析工具以避免循环导入。
"""Open Deep Research 的本地 RAG 子模块导出入口。"""

from open_deep_research.rag.config import (
    ChunkingConfig,
    EmbeddingConfig,
    HybridRetrievalConfig,
    KeywordSearchConfig,
    MultimodalConfig,
    RerankerConfig,
    VectorstoreConfig,
)
from open_deep_research.rag.service import (
    RAGPipeline,
    RAGPipelineConfig,
    build_rag_pipeline_config,
    get_or_create_rag_pipeline,
    reset_rag_pipeline_cache,
)


# 在包初始化后按需解析公共工具属性，兼容延迟导入。
def __getattr__(name: str):
    """Resolve the public tool after the RAG package has initialized."""
    if name == "rag_search":
        from open_deep_research.tools.rag_tool import rag_search

        return rag_search
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ChunkingConfig",
    "EmbeddingConfig",
    "HybridRetrievalConfig",
    "KeywordSearchConfig",
    "MultimodalConfig",
    "RAGPipeline",
    "RAGPipelineConfig",
    "build_rag_pipeline_config",
    "RerankerConfig",
    "VectorstoreConfig",
    "get_or_create_rag_pipeline",
    "rag_search",
    "reset_rag_pipeline_cache",
]
