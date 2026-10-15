# 导出本地 RAG 的配置与查询入口，并延迟解析工具以避免循环导入。
"""Open Deep Research 的本地 RAG 子模块导出入口。

这里集中导出最常用的 RAG API，方便外部代码用：

```python
from open_deep_research.rag import rag_search, RAGPipeline
```

普通主流程只需要 `rag_search`；单测、示例或扩展开发才会直接使用
`RAGPipeline` / `RAGPipelineConfig`。
"""

from open_deep_research.memory.store import (
    load_memory_record_by_id,
    load_memory_record_by_source,
    load_memory_record_by_source_id,
    parse_mysql_memory_source,
)
from open_deep_research.memory.writer import persist_conversation_memory
from open_deep_research.rag.config import (
    ChunkingConfig,
    EmbeddingConfig,
    GraphRAGConfig,
    HybridRetrievalConfig,
    KeywordSearchConfig,
    MemoryConfig,
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
    "GraphRAGConfig",
    "HybridRetrievalConfig",
    "KeywordSearchConfig",
    "MemoryConfig",
    "MultimodalConfig",
    "RAGPipeline",
    "RAGPipelineConfig",
    "build_rag_pipeline_config",
    "RerankerConfig",
    "VectorstoreConfig",
    "get_or_create_rag_pipeline",
    "load_memory_record_by_id",
    "load_memory_record_by_source",
    "load_memory_record_by_source_id",
    "parse_mysql_memory_source",
    "persist_conversation_memory",
    "rag_search",
    "reset_rag_pipeline_cache",
]
