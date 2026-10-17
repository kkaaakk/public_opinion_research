# 提供原序、关键词和交叉编码器重排后端，统一候选重排接口。
"""本地 RAG reranker 实现。

retriever 负责“召回候选”，reranker 负责“精排候选”。召回阶段更追求不漏，
重排阶段更追求相关性。

当前支持：

- `CrossEncoderReranker`：默认生产路径，适合 BGE/cross-encoder 模型。
- `KeywordOverlapReranker`：轻量 fallback，适合测试。
- `NoOpReranker`：不重排，保留召回顺序。
"""

import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

from open_deep_research.rag.metadata import build_structured_context_text
from open_deep_research.rag.types import RetrievalResult

if TYPE_CHECKING:
    from open_deep_research.rag.config import RerankerConfig

TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)


class Reranker(ABC):
    """reranker 抽象接口。"""

    @abstractmethod
    def rerank(self, query: str, results: list[RetrievalResult], top_k: int) -> list[RetrievalResult]:
        """对召回候选进行重排，并返回前 top_k 条。"""


class NoOpReranker(Reranker):
    """空操作 reranker。

    用于禁用重排时保留 retriever 的结果顺序。
    """

    # 保留原召回顺序并按数量截取结果。
    def rerank(self, query: str, results: list[RetrievalResult], top_k: int) -> list[RetrievalResult]:
        return results[:top_k]


class KeywordOverlapReranker(Reranker):
    """基于关键词重合度的轻量 reranker。

    它不需要模型，只比较 query token 与 chunk title/content token 的重合比例。
    适合单测和低依赖环境，但相关性判断能力弱于 cross-encoder。
    """

    # 按查询与候选文本的关键词重合程度重新排序。
    def rerank(self, query: str, results: list[RetrievalResult], top_k: int) -> list[RetrievalResult]:
        query_terms = set(_tokenize(query))
        reranked_results = []

        for result in results:
            content_terms = set(_tokenize(_reranker_document_text(result)))
            overlap_score = 0.0
            if query_terms and content_terms:
                overlap_score = len(query_terms & content_terms) / len(query_terms)
            reranked_results.append(
                # model_copy 保留原来的 vector_score/keyword_score，同时补上 rerank_score。
                result.model_copy(update={"rerank_score": overlap_score})
            )

        reranked_results.sort(
            key=lambda item: ((item.rerank_score or 0.0), item.score),
            reverse=True,
        )
        return reranked_results[:top_k]


class CrossEncoderReranker(Reranker):
    """Cross-encoder reranker，适合 BGE reranker 类模型。

    与 embedding 双塔模型不同，cross-encoder 会同时读取 `(query, chunk)`，
    直接输出相关性分数。它通常更准，但推理成本也更高，所以只对召回后的
    少量候选执行。
    """

    # 加载交叉编码器模型，并选择执行设备。
    def __init__(self, model_name: str, device: Optional[str] = None):
        try:
            # sentence-transformers 是重依赖，运行到 cross-encoder 模式时才导入。
            from sentence_transformers import CrossEncoder
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ImportError(
                "Install sentence-transformers to use cross-encoder RAG reranking."
            ) from exc

        self.model = CrossEncoder(model_name, device=device)

    def rerank(self, query: str, results: list[RetrievalResult], top_k: int) -> list[RetrievalResult]:
        """使用 cross-encoder 对候选 chunk 精排。"""
        if not results:
            return []
        # CrossEncoder 输入是 pair 列表。把 title 和 content 一起给模型，
        # 可以让章节名/页面标题参与相关性判断。
        scores = self.model.predict(
            [(query, _reranker_document_text(result)) for result in results],
            show_progress_bar=False,
        )
        reranked_results = [
            result.model_copy(update={"rerank_score": float(score)})
            for result, score in zip(results, scores)
        ]
        reranked_results.sort(key=lambda item: item.rerank_score or 0.0, reverse=True)
        return reranked_results[:top_k]


def create_reranker(
    provider: str,
    model_name: str,
    device: Optional[str] = None,
    *,
    config: "RerankerConfig | None" = None,
) -> Reranker:
    """根据配置创建 reranker 后端。

    可以传入 RerankerConfig 对象，也可以传入分散的参数。当 ``config``
    不为 ``None`` 时，会用 config 的值覆盖位置参数传入的值。

    provider 别名：
    - `cross_encoder` / `cross-encoder` / `bge` / `bge-reranker`
    - `simple`
    - `none` / `disabled`
    """
    if config is not None:
        provider = config.provider
        model_name = config.model
        device = config.device
    normalized_provider = provider.lower().strip()
    if normalized_provider in {"none", "disabled"}:
        return NoOpReranker()
    if normalized_provider == "simple":
        return KeywordOverlapReranker()
    if normalized_provider in {"cross_encoder", "cross-encoder", "bge", "bge-reranker"}:
        return CrossEncoderReranker(model_name=model_name, device=device)
    raise ValueError(
        f"Unsupported RAG reranker provider '{provider}'. "
        "Add a new backend implementation in open_deep_research.rag.reranker."
    )


def _tokenize(text: str) -> list[str]:
    """轻量 tokenizer，逻辑与 retriever 中的 BM25 tokenizer 保持一致。"""
    tokens = []
    for token in TOKEN_PATTERN.findall(text.lower()):
        if any(_is_cjk(char) for char in token):
            tokens.extend(char for char in token if _is_cjk(char))
        else:
            tokens.append(token)
    return tokens


# 合并候选正文与结构化上下文，作为重排模型的输入文本。
def _reranker_document_text(result: RetrievalResult) -> str:
    """Build the candidate text seen by simple and cross-encoder rerankers."""
    chunk = result.chunk
    structured_context = build_structured_context_text(
        source=chunk.source,
        title=chunk.title,
        metadata=chunk.metadata,
    )
    if structured_context:
        return f"{structured_context}\ncontent:\n{chunk.content}"
    return chunk.content


def _is_cjk(char: str) -> bool:
    """判断字符是否属于常见 CJK 统一表意文字范围。"""
    return "\u4e00" <= char <= "\u9fff"
