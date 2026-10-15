# 提供哈希和 SentenceTransformer 嵌入后端，统一批量文本向量化接口。
"""本地 RAG embedding 后端。

本模块把文本转换成向量，供向量库召回使用。当前提供两类后端：

- `SentenceTransformerEmbeddingBackend`：真实语义 embedding，默认生产路径。
- `HashEmbeddingBackend`：确定性 hash 向量，仅用于显式离线诊断，不用于检索指标测试。

后端都实现同一个 `EmbeddingBackend` 接口，方便以后接入 OpenAI、Voyage、
BAAI、Jina 等其它 embedding 服务。
"""

import hashlib
import math
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from open_deep_research.rag.config import EmbeddingConfig

TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)


class EmbeddingBackend(ABC):
    """embedding 后端抽象接口。

    RAG 管线只依赖这两个方法，不关心底层是本地模型、远程 API，
    还是显式离线诊断用 hash 实现。
    """

    @abstractmethod
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量向量化文档 chunk 文本。"""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """向量化单条用户查询。"""


class HashEmbeddingBackend(EmbeddingBackend):
    """确定性 hash embedding 后端。

    这个后端不具备真正语义理解能力，但它有两个优点：

    - 无需下载模型或调用外部服务。
    - 同样输入永远得到同样向量，适合单测。

    正常 RAG 使用应优先选择 SentenceTransformers 或其它语义 embedding。
    """

    # 保存哈希向量维数，准备无需下载模型的嵌入后端。
    def __init__(self, dimensions: int = 256):
        if dimensions <= 0:
            raise ValueError("Hash embedding dimensions must be greater than 0.")
        self.dimensions = dimensions

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量生成 hash 向量。"""
        return [self._embed_text(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        """为查询生成 hash 向量。"""
        return self._embed_text(text)

    def _embed_text(self, text: str) -> list[float]:
        """将 token hash 到固定维度的归一化稠密向量。

        简化逻辑：
        1. 用正则提取 token。
        2. 对每个 token 做 sha256。
        3. hash 前几位决定向量位置，另一位决定正负号。
        4. 用 token 长度给一个轻微权重。
        5. 最后做 L2 归一化，便于余弦相似度计算。
        """
        vector = [0.0] * self.dimensions
        tokens = TOKEN_PATTERN.findall(text.lower())
        if not tokens:
            return vector

        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            position = int.from_bytes(digest[:4], byteorder="big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            weight = 1.0 + math.log1p(len(token))
            vector[position] += sign * weight

        magnitude = math.sqrt(sum(component * component for component in vector))
        if magnitude == 0:
            return vector
        return [component / magnitude for component in vector]


class SentenceTransformerEmbeddingBackend(EmbeddingBackend):
    """基于 sentence-transformers 的语义 embedding 后端。

    这是默认后端。它会懒加载本地/远程模型，`model_name` 可以是
    Hugging Face 模型名或本地模型路径。

    `normalize_embeddings=True` 会让向量长度归一化，便于 Chroma cosine
    或 FAISS inner product 检索。
    """

    # 加载指定 SentenceTransformer 模型，并选择推理设备。
    def __init__(
        self,
        model_name: str,
        device: Optional[str] = None,
        normalize_embeddings: bool = True,
    ):
        try:
            # sentence-transformers 是重依赖，放在运行时导入，避免只跑测试
            # 或使用 hash backend 时强制安装/加载。
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise ImportError(
                "Install sentence-transformers to use semantic RAG embeddings."
            ) from exc

        self.model = SentenceTransformer(model_name, device=device)
        self.normalize_embeddings = normalize_embeddings

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量生成语义向量。"""
        if not texts:
            return []
        vectors = self.model.encode(
            texts,
            normalize_embeddings=self.normalize_embeddings,
            show_progress_bar=False,
        )
        return vectors.tolist()

    def embed_query(self, text: str) -> list[float]:
        """生成查询语义向量。"""
        return self.embed_texts([text])[0]


def create_embedding_backend(
    provider: str,
    model_name: str,
    device: Optional[str] = None,
    hash_dimensions: int = 256,
    *,
    config: "EmbeddingConfig | None" = None,
) -> EmbeddingBackend:
    """根据配置创建 embedding 后端。

    可以传入 EmbeddingConfig 对象，也可以传入分散的参数。当 ``config``
    不为 ``None`` 时，会用 config 的值覆盖位置参数传入的值。

    provider 别名：
    - `sentence_transformers` / `sentence-transformers` / `st` / `semantic`
    - `hash`
    """
    if config is not None:
        provider = config.provider
        model_name = config.model
        device = config.device
        hash_dimensions = config.hash_dimensions
    normalized_provider = provider.lower().strip()
    if normalized_provider in {"sentence_transformers", "sentence-transformers", "st", "semantic"}:
        return SentenceTransformerEmbeddingBackend(model_name=model_name, device=device)
    if normalized_provider == "hash":
        return HashEmbeddingBackend(dimensions=hash_dimensions)
    raise ValueError(
        f"Unsupported RAG embedding provider '{provider}'. "
        "Add a new backend implementation in open_deep_research.rag.embeddings."
    )
