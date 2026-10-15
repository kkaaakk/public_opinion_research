# 定义知识文档、分块、检索结果、引用及可直接用于回答的上下文结构。
"""本地 RAG 管线共享数据结构。

这些类型是 RAG 子系统内部的“数据契约”：

- `RAGDocument` 表示从磁盘文件中加载出来的原始文档单元。
- `RAGChunk` 表示可被 embedding、检索、引用的最小文本块。
- `RetrievalResult` 表示一次召回或重排后的候选结果。
- `Citation` 表示给 LLM 展示时使用的引用信息。
- `AnswerReadyContext` 表示最终返回给 researcher agent 的带引用上下文。

注意：这里不放复杂逻辑，只定义字段形状，方便 loader、splitter、
retriever、reranker、citation formatter 之间稳定传递数据。
"""

from typing import Any, Optional

from pydantic import BaseModel, Field


class RAGDocument(BaseModel):
    """从知识库路径中加载出来的原始文档。

    一个 `RAGDocument` 不一定对应一个物理文件：

    - `.txt` / `.md` 通常是一个文件对应一个 document。
    - `.json` 如果根节点是数组，则每个 item 会变成一个 document。
    - `.pdf` 会按页拆成多个 document，这样 citation 可以带页码。

    `metadata` 用于保留加载阶段的信息，例如扩展名、文件路径、页码、
    JSON 字段路径等，后续 chunk 会继承这些信息。
    """

    content: str
    source: str
    title: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RAGChunk(BaseModel):
    """从 `RAGDocument` 切分出来的检索文本块。

    chunk 是真正进入 embedding、向量库、BM25 和 reranker 的单位。
    `chunk_id` 在一次索引构建中稳定生成，格式类似 `doc-1-chunk-3`，
    方便检索去重、引用展示和测试断言。

    `metadata` 会合并文档级 metadata 和切块阶段 metadata，例如：
    页码、标题层级、字符范围、行号范围、JSON 字段路径。
    """

    content: str
    source: str
    title: Optional[str] = None
    chunk_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalResult(BaseModel):
    """检索或重排阶段产生的候选结果。

    字段含义：

    - `score`：当前阶段使用的综合分数。hybrid fusion 后它是融合分数。
    - `vector_score`：向量召回归一化后的分数。
    - `keyword_score`：BM25/关键词召回归一化后的分数。
    - `structured_score`：source、标题、json_path 等结构化 metadata 命中的分数。
    - `rerank_score`：cross-encoder/BGE 或 simple reranker 给出的重排分数。

    保留这些分数字段，可以让后续调试时判断结果是由语义召回命中、
    关键词召回命中，还是 reranker 拉上来的。
    """

    chunk: RAGChunk
    score: float
    vector_score: Optional[float] = None
    keyword_score: Optional[float] = None
    graph_score: Optional[float] = None
    graph_expansion_decision: Optional[dict] = None
    structured_score: Optional[float] = None
    rerank_score: Optional[float] = None


class Citation(BaseModel):
    """面向 LLM 的引用对象。

    `Citation` 是从 `RetrievalResult` 转换而来，用于生成工具返回文本。
    它只保留回答时需要展示的信息：标题、来源、chunk id、摘录和 metadata。
    """

    citation_id: int
    title: str
    source: str
    chunk_id: str
    excerpt: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AnswerReadyContext(BaseModel):
    """可以直接注入 researcher 工具结果的上下文。

    `context` 是最终给 LLM 看的字符串，内部包含 SOURCE、CHUNK ID、
    EXCERPT 和 Sources 列表。`citations` 与 `matched_chunks` 保留结构化
    信息，方便测试和未来扩展。
    """

    query: str
    context: str
    citations: list[Citation] = Field(default_factory=list)
    matched_chunks: list[RetrievalResult] = Field(default_factory=list)
