import asyncio
import json
import sys
import textwrap
import types

import pytest
from langchain_core.messages import HumanMessage
from langchain_text_splitters import Language

from open_deep_research.configuration import Configuration
from open_deep_research.rag import loaders
from open_deep_research.rag import splitter as rag_splitter
from open_deep_research.rag.graph import Neo4jGraphRAGIndex, create_graph_index
from open_deep_research.rag.mysql_memory import (
    build_chat_memory_records,
    normalize_mysql_url,
    normalize_record_types,
    parse_mysql_memory_source,
    records_to_documents,
    validate_table_name,
)
from open_deep_research.rag.reranker import (
    CrossEncoderReranker,
    KeywordOverlapReranker,
)
from open_deep_research.rag.retriever import (
    BM25Index,
    HybridChunkRetriever,
    reciprocal_rank_fusion,
)
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
    apply_authority_adjustment,
    build_rag_index_id,
    reset_rag_pipeline_cache,
)
from open_deep_research.rag.splitter import split_documents
from open_deep_research.rag.types import RAGChunk, RAGDocument, RetrievalResult
from open_deep_research.rag.vectorstore import (
    InMemoryVectorStore,
    create_vectorstore_backend,
)
from open_deep_research.utils import get_all_tools


@pytest.fixture(autouse=True)
def clear_rag_cache():
    reset_rag_pipeline_cache()
    yield
    reset_rag_pipeline_cache()


def write_text_file(path, content: str) -> None:
    path.write_text(textwrap.dedent(content).strip(), encoding="utf-8")


def write_jsonl_file(path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records),
        encoding="utf-8",
    )


def test_rag_defaults_to_milvus_for_persistent_vector_store():
    config = Configuration()
    pipeline_config = RAGPipelineConfig()

    assert config.rag_vectorstore_provider == "milvus"
    assert pipeline_config.vectorstore_provider == "milvus"


def test_rag_defaults_use_canonical_data_directories():
    config = Configuration()
    pipeline_config = RAGPipelineConfig()

    assert config.rag_knowledge_base_paths == ["data/knowledge"]
    assert config.rag_memory_paths == ["data/memory/chat_memory.jsonl"]
    assert config.rag_vectorstore_path == "data/indexes/rag"
    assert config.rag_milvus_uri == "data/indexes/rag/milvus.db"
    assert pipeline_config.knowledge_base_paths == ["data/knowledge"]
    assert pipeline_config.memory_paths == ["data/memory/chat_memory.jsonl"]
    assert pipeline_config.vectorstore_path == "data/indexes/rag"
    assert pipeline_config.milvus_uri == "data/indexes/rag/milvus.db"


def test_rag_multimodal_vision_defaults_are_enabled():
    config = Configuration()
    pipeline_config = RAGPipelineConfig()

    assert config.rag_multimodal_enabled is True
    assert config.rag_multimodal_provider == "ocr"
    assert config.rag_vision_enabled is True
    assert config.rag_vision_model == "openai:gpt-4.1-mini"
    assert config.rag_vision_max_tokens == 512
    assert config.rag_query_image_enabled is True
    assert config.rag_query_image_max_images == 3
    assert pipeline_config.multimodal_enabled is True
    assert pipeline_config.vision_enabled is True
    assert pipeline_config.vision_model == "openai:gpt-4.1-mini"
    assert pipeline_config.vision_max_tokens == 512


def test_rag_query_rewrite_defaults_to_enabled():
    config = Configuration()

    assert config.rag_query_rewrite_enabled is True
    assert config.rag_query_rewrite_model == "deepseek:deepseek-chat"
    assert config.rag_query_rewrite_max_tokens == 256


def test_rag_structured_metadata_weight_defaults_to_enabled():
    config = Configuration()
    pipeline_config = RAGPipelineConfig()

    assert config.rag_structured_metadata_weight == pytest.approx(0.15)
    assert pipeline_config.structured_metadata_weight == pytest.approx(0.15)


def test_graph_rag_defaults_to_neo4j_backend():
    config = Configuration()
    pipeline_config = RAGPipelineConfig()

    assert config.rag_graph_enabled is True
    assert config.rag_graph_backend == "neo4j"
    assert config.rag_neo4j_uri == "bolt://localhost:7687"
    assert config.rag_neo4j_username == "neo4j"
    assert pipeline_config.graph_enabled is False
    assert pipeline_config.graph_backend == "neo4j"
    assert pipeline_config.neo4j_uri == "bolt://localhost:7687"
    assert pipeline_config.neo4j_username == "neo4j"


def test_rag_structured_metadata_weight_rejects_negative_values():
    with pytest.raises(ValueError, match="rag_structured_metadata_weight"):
        Configuration(rag_structured_metadata_weight=-0.1)


# ---------------------------------------------------------------------------
# Sub-config class default values
# ---------------------------------------------------------------------------

def test_sub_config_defaults_match_flat_field_defaults():
    """Sub-config zero-arg construction matches RAGPipelineConfig flat defaults."""
    config = RAGPipelineConfig()

    assert config.embedding == EmbeddingConfig()
    assert config.vectorstore == VectorstoreConfig()
    assert config.reranker == RerankerConfig()
    assert config.multimodal == MultimodalConfig()
    assert config.memory == MemoryConfig()
    assert config.keyword_search == KeywordSearchConfig()
    assert config.hybrid_retrieval == HybridRetrievalConfig()
    assert config.graph_rag == GraphRAGConfig()
    assert config.chunking == ChunkingConfig()


# ---------------------------------------------------------------------------
# Read-only view: flat field -> property derivation
# ---------------------------------------------------------------------------

def test_flat_field_writes_reflect_in_sub_config_property():
    """Setting a flat field should be reflected in the sub-config property."""
    config = RAGPipelineConfig(embedding_provider="hash", vectorstore_provider="memory")
    assert config.embedding.provider == "hash"
    assert config.vectorstore.provider == "memory"

    config2 = RAGPipelineConfig(graph_enabled=True, neo4j_uri="bolt://custom:7687")
    assert config2.graph_rag.enabled is True
    assert config2.graph_rag.neo4j_uri == "bolt://custom:7687"


# ---------------------------------------------------------------------------
# Read-only view: nested sub-config construction -> flat field unpacking
# ---------------------------------------------------------------------------

def test_nested_sub_config_unpacks_to_flat_fields():
    """Passing embedding=EmbeddingConfig(...) should unpack to flat fields."""
    config = RAGPipelineConfig(embedding=EmbeddingConfig(provider="hash"))
    assert config.embedding_provider == "hash"


def test_nested_vectorstore_config_unpacks_and_triggers_milvus_uri_derivation():
    """Passing vectorstore=VectorstoreConfig(persist_path=...) should trigger milvus_uri derivation."""
    config = RAGPipelineConfig(vectorstore=VectorstoreConfig(persist_path="custom/path"))
    assert config.vectorstore_path == "custom/path"
    assert config.milvus_uri == "custom/path/milvus.db"


def test_explicit_flat_field_wins_over_sub_config():
    """When both flat field and sub-config are passed, the flat field wins."""
    config = RAGPipelineConfig(
        embedding=EmbeddingConfig(provider="hash"),
        embedding_provider="sentence_transformers",
    )
    assert config.embedding_provider == "sentence_transformers"


# ---------------------------------------------------------------------------
# model_fields / model_dump invariance
# ---------------------------------------------------------------------------

def test_model_dump_does_not_contain_sub_config_keys():
    """model_dump() should not contain sub-config property names."""
    dumped = RAGPipelineConfig().model_dump()
    assert "embedding" not in dumped
    assert "vectorstore" not in dumped
    assert "reranker" not in dumped
    assert "multimodal" not in dumped
    assert "memory" not in dumped
    assert "keyword_search" not in dumped
    assert "hybrid_retrieval" not in dumped
    assert "graph_rag" not in dumped
    assert "chunking" not in dumped
    assert "embedding_provider" in dumped


def test_model_fields_does_not_contain_sub_config_names():
    """model_fields should not contain sub-config property names."""
    field_names = set(RAGPipelineConfig.model_fields.keys())
    for sub_key in (
        "embedding", "vectorstore", "reranker", "multimodal", "memory",
        "keyword_search", "hybrid_retrieval", "graph_rag", "chunking",
    ):
        assert sub_key not in field_names


def test_rag_loader_package_exports_new_and_legacy_loader_apis():
    import open_deep_research.rag.loaders as loader_package
    from open_deep_research.rag import memory as legacy_memory
    from open_deep_research.rag import query_images as legacy_query_images

    assert loader_package.load_documents_from_paths is loaders.load_documents_from_paths
    assert callable(loader_package.load_memory_documents_from_paths)
    assert callable(loader_package.load_memory_documents_from_mysql)
    assert callable(loader_package.build_query_image_context)
    assert (
        legacy_memory.load_memory_documents_from_paths
        is loader_package.load_memory_documents_from_paths
    )
    assert callable(legacy_query_images.build_query_image_context)


def test_rag_legacy_paths_warn_but_remain_usable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "examples" / "rag_data").mkdir(parents=True)
    (tmp_path / ".rag_index").mkdir()

    with pytest.warns(UserWarning, match="legacy RAG path"):
        config = Configuration(
            rag_knowledge_base_paths=["examples/rag_data"],
            rag_vectorstore_path=".rag_index",
            rag_milvus_uri=".rag_index/milvus.db",
        )

    assert config.rag_knowledge_base_paths == ["examples/rag_data"]
    assert config.rag_vectorstore_path == ".rag_index"
    assert config.rag_milvus_uri == ".rag_index/milvus.db"


def test_rag_defaults_fall_back_to_legacy_knowledge_path_when_needed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "examples" / "rag_data").mkdir(parents=True)

    with pytest.warns(UserWarning, match="rag_knowledge_base_paths uses legacy RAG path"):
        config = Configuration()

    assert config.rag_knowledge_base_paths == ["examples/rag_data"]
    assert config.rag_vectorstore_path == "data/indexes/rag"
    assert config.rag_milvus_uri == "data/indexes/rag/milvus.db"


def test_milvus_vectorstore_backend_adds_and_searches_chunks(monkeypatch, tmp_path):
    class FakeMilvusClient:
        def __init__(self, uri, token=None, db_name=None):
            self.uri = uri
            self.token = token
            self.db_name = db_name
            self.collections = {}

        def has_collection(self, collection_name):
            return collection_name in self.collections

        def create_collection(self, collection_name, dimension, **kwargs):
            self.collections[collection_name] = {
                "dimension": dimension,
                "rows": [],
            }

        def drop_collection(self, collection_name):
            self.collections.pop(collection_name, None)

        def insert(self, collection_name, data):
            self.collections[collection_name]["rows"].extend(data)

        def get_collection_stats(self, collection_name):
            return {"row_count": len(self.collections[collection_name]["rows"])}

        def search(self, collection_name, data, limit, output_fields, **kwargs):
            query_vector = data[0]
            rows = self.collections[collection_name]["rows"]

            def dot(row):
                return sum(
                    left * right
                    for left, right in zip(query_vector, row["embedding"])
                )

            hits = []
            for row in sorted(rows, key=dot, reverse=True)[:limit]:
                hits.append(
                    {
                        "id": row["id"],
                        "distance": dot(row),
                        "entity": {
                            field: row[field]
                            for field in output_fields
                            if field in row
                        },
                    }
                )
            return [hits]

    monkeypatch.setitem(
        sys.modules,
        "pymilvus",
        types.SimpleNamespace(MilvusClient=FakeMilvusClient),
    )

    store = create_vectorstore_backend(
        provider="milvus",
        persist_path=str(tmp_path / "milvus.db"),
        collection_name="test_collection",
        index_id="abc123",
    )
    chunks = [
        RAGChunk(
            content="NimbusCRM pricing page mentions a free tier.",
            source="internal://nimbus",
            title="NimbusCRM",
            chunk_id="chunk-a",
        ),
        RAGChunk(
            content="AtlasCRM emphasizes enterprise governance.",
            source="internal://atlas",
            title="AtlasCRM",
            chunk_id="chunk-b",
        ),
    ]

    store.add(chunks, [[1.0, 0.0], [0.0, 1.0]])
    results = store.search([1.0, 0.0], top_k=1)

    assert store.is_ready()
    assert results[0].chunk.chunk_id == "chunk-a"
    assert results[0].vector_score == pytest.approx(1.0)


def test_graph_rag_expands_related_competitor_evidence():
    seed_chunk = RAGChunk(
        content="NimbusCRM launched a free tier for SMB teams in 2025.",
        source="internal://product",
        title="NimbusCRM launch",
        chunk_id="seed",
    )
    related_chunk = RAGChunk(
        content="The free tier created pricing pressure in the SMB sales pipeline.",
        source="internal://sales",
        title="Sales feedback",
        chunk_id="related",
    )
    unrelated_chunk = RAGChunk(
        content="AtlasCRM released a mobile dashboard for field teams.",
        source="internal://atlas",
        title="Atlas release",
        chunk_id="unrelated",
    )
    chunks = [seed_chunk, related_chunk, unrelated_chunk]
    vectorstore = InMemoryVectorStore()
    vectorstore.add(chunks, [[1.0, 0.0], [0.0, 1.0], [0.0, -1.0]])

    retriever = HybridChunkRetriever(
        vectorstore=vectorstore,
        keyword_index=BM25Index(chunks),
        graph_enabled=True,
        graph_max_neighbors=2,
        graph_weight=0.45,
    )

    results = retriever.retrieve(
        query="NimbusCRM launch risk",
        query_vector=[1.0, 0.0],
        top_k=3,
        keyword_top_k=3,
    )
    by_id = {result.chunk.chunk_id: result for result in results}

    assert "related" in by_id
    assert by_id["related"].graph_score is not None
    assert by_id["related"].graph_score > 0
    assert by_id["related"].score > 0


def test_reciprocal_rank_fusion_prefers_results_seen_by_both_retrievers():
    vector_only = RAGChunk(
        content="Dense-only result with a very large raw vector score.",
        source="internal://dense",
        title="Dense",
        chunk_id="dense-only",
    )
    shared = RAGChunk(
        content="Shared result returned by dense and BM25.",
        source="internal://shared",
        title="Shared",
        chunk_id="shared",
    )

    fused = reciprocal_rank_fusion(
        vector_results=[
            RetrievalResult(chunk=vector_only, score=100.0, vector_score=100.0),
            RetrievalResult(chunk=shared, score=1.0, vector_score=1.0),
        ],
        keyword_results=[
            RetrievalResult(chunk=shared, score=1.0, keyword_score=1.0),
        ],
        rank_constant=60,
    )

    assert [result.chunk.chunk_id for result in fused] == ["shared", "dense-only"]
    assert fused[0].score == pytest.approx((1 / 62) + (1 / 61))
    assert fused[0].vector_score == 1.0
    assert fused[0].keyword_score == 1.0


def test_hybrid_retriever_boosts_structured_metadata_matches():
    vector_preferred = RAGChunk(
        content="General operations note.",
        source="docs/support.md",
        title=None,
        chunk_id="vector-preferred",
        metadata={"file_type": "md", "h1": "Support"},
    )
    metadata_match = RAGChunk(
        content="General operations note.",
        source="docs/pricing.md",
        title=None,
        chunk_id="metadata-match",
        metadata={"file_type": "md", "h1": "Pricing", "h2": "FAQ"},
    )
    chunks = [vector_preferred, metadata_match]
    vectorstore = InMemoryVectorStore()
    vectorstore.add(chunks, [[1.0, 0.0], [0.99, 0.0]])
    retriever = HybridChunkRetriever(
        vectorstore=vectorstore,
        keyword_index=BM25Index(chunks),
        structured_metadata_weight=0.2,
    )

    results = retriever.retrieve(
        query="pricing faq",
        query_vector=[1.0, 0.0],
        top_k=2,
        keyword_top_k=2,
    )

    assert [result.chunk.chunk_id for result in results] == [
        "metadata-match",
        "vector-preferred",
    ]
    assert results[0].structured_score == pytest.approx(1.0)
    assert results[0].score > results[1].score


def test_keyword_reranker_uses_structured_metadata_context():
    content_only = RetrievalResult(
        chunk=RAGChunk(
            content="General operations note.",
            source="docs/support.md",
            title="Support",
            chunk_id="content-only",
            metadata={"file_type": "md", "h1": "Support"},
        ),
        score=1.0,
    )
    metadata_match = RetrievalResult(
        chunk=RAGChunk(
            content="General operations note.",
            source="docs/pricing.md",
            title="Pricing",
            chunk_id="metadata-match",
            metadata={"file_type": "md", "h1": "Pricing", "h2": "FAQ"},
        ),
        score=1.0,
    )

    reranked = KeywordOverlapReranker().rerank(
        query="pricing faq",
        results=[content_only, metadata_match],
        top_k=2,
    )

    assert [result.chunk.chunk_id for result in reranked] == [
        "metadata-match",
        "content-only",
    ]
    assert reranked[0].rerank_score == pytest.approx(1.0)


def test_query_rewriter_cleans_model_output_and_preserves_prompt():
    from open_deep_research.rag.query_rewriter import rewrite_query_with_model

    captured = {}

    class FakeModel:
        def invoke(self, messages, config=None):
            captured["messages"] = messages
            return types.SimpleNamespace(
                content="Rewritten query: Milvus vector index path"
            )

    def fake_model_factory(**kwargs):
        captured["kwargs"] = kwargs
        return FakeModel()

    rewritten_query = rewrite_query_with_model(
        "where is it?",
        model_name="fake:model",
        max_tokens=64,
        api_key="test-key",
        model_factory=fake_model_factory,
    )

    assert rewritten_query == "Milvus vector index path"
    assert captured["kwargs"]["model"] == "fake:model"
    assert captured["kwargs"]["max_tokens"] == 64
    assert captured["kwargs"]["api_key"] == "test-key"
    assert "where is it?" in captured["messages"][0].content


def test_query_rewriter_falls_back_to_original_query_on_failure():
    from open_deep_research.rag.query_rewriter import rewrite_query_with_model

    def failing_model_factory(**kwargs):
        raise RuntimeError("model unavailable")

    assert (
        rewrite_query_with_model(
            "original query",
            model_name="fake:model",
            max_tokens=64,
            api_key=None,
            model_factory=failing_model_factory,
        )
        == "original query"
    )


def test_cross_encoder_reranker_includes_structured_metadata_in_pairs(monkeypatch):
    captured_pairs = []

    class FakeCrossEncoder:
        def __init__(self, model_name, device=None):
            self.model_name = model_name
            self.device = device

        def predict(self, pairs, show_progress_bar=False):
            captured_pairs.extend(pairs)
            return [0.42]

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(CrossEncoder=FakeCrossEncoder),
    )
    reranker = CrossEncoderReranker(model_name="fake-reranker", device="cpu")
    result = RetrievalResult(
        chunk=RAGChunk(
            content="The discount rules live here.",
            source="docs/pricing.md",
            title="Pricing",
            chunk_id="pricing-1",
            metadata={
                "file_type": "md",
                "h1": "Pricing",
                "h2": "FAQ",
                "json_path": "$.posts[0].content",
            },
        ),
        score=1.0,
    )

    reranked = reranker.rerank(
        query="pricing faq post content",
        results=[result],
        top_k=1,
    )

    assert reranked[0].rerank_score == pytest.approx(0.42)
    assert "h1: Pricing" in captured_pairs[0][1]
    assert "h2: FAQ" in captured_pairs[0][1]
    assert "json_path: $.posts[0].content" in captured_pairs[0][1]


def test_neo4j_graph_rag_writes_chunks_and_expands_neighbors():
    class FakeSession:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def run(self, query, **params):
            self.calls.append((query, params))
            if "RETURN neighbor.chunk_id" in query:
                return [{"chunk_id": "related", "shared_terms": 2}]
            return []

    class FakeDriver:
        def __init__(self):
            self.session_obj = FakeSession()

        def session(self, database=None):
            self.database = database
            return self.session_obj

    fake_driver = FakeDriver()
    chunks = [
        RAGChunk(
            content="NimbusCRM launched a free tier.",
            source="internal://product",
            title="NimbusCRM launch",
            chunk_id="seed",
        ),
        RAGChunk(
            content="Free tier pricing pressure appeared in sales notes.",
            source="internal://sales",
            title="Sales note",
            chunk_id="related",
        ),
    ]

    graph_index = create_graph_index(
        chunks,
        backend="neo4j",
        index_id="idx-1",
        neo4j_uri="bolt://localhost:7687",
        neo4j_username="neo4j",
        neo4j_password="password",
        neo4j_database="neo4j",
        driver_factory=lambda *_args, **_kwargs: fake_driver,
    )
    results = graph_index.expand(
        query="NimbusCRM pricing",
        seed_results=[
            RetrievalResult(chunk=chunks[0], score=0.5, vector_score=0.5),
        ],
        max_neighbors=2,
        graph_weight=0.4,
    )

    assert isinstance(graph_index, Neo4jGraphRAGIndex)
    assert any(
        "MERGE (chunk:RagChunk" in query
        for query, _params in fake_driver.session_obj.calls
    )
    by_id = {result.chunk.chunk_id: result for result in results}
    assert "related" in by_id
    assert by_id["related"].graph_score == pytest.approx(0.4 * 0.5 * (2 / 3))


def test_multimodal_image_loader_extracts_ocr_text(monkeypatch, tmp_path):
    image_path = tmp_path / "pricing.png"
    image_path.write_bytes(b"fake image bytes")

    monkeypatch.setattr(
        loaders,
        "extract_image_text",
        lambda path, provider, ocr_languages: "NimbusCRM pricing screenshot shows a free tier.",
    )

    documents = loaders.load_documents_from_paths(
        [str(image_path)],
        multimodal_enabled=True,
        multimodal_provider="ocr",
        ocr_languages="eng",
    )

    assert len(documents) == 1
    assert "free tier" in documents[0].content
    assert documents[0].metadata["modality"] == "image"
    assert documents[0].metadata["ocr_provider"] == "ocr"


def test_code_file_loader_marks_language_metadata(tmp_path):
    source_path = tmp_path / "auth_service.py"
    write_text_file(
        source_path,
        """
        def authenticate_user(token: str) -> bool:
            return token.startswith("user_")
        """,
    )

    documents = loaders.load_documents_from_paths(
        [str(tmp_path)],
        multimodal_enabled=False,
    )

    assert len(documents) == 1
    assert documents[0].source.endswith("auth_service.py")
    assert documents[0].title == "auth_service.py"
    assert documents[0].metadata["extension"] == ".py"
    assert documents[0].metadata["file_type"] == "code"
    assert documents[0].metadata["language"] == "python"


def test_image_routing_uses_ocr_only_for_text_heavy_images(monkeypatch, tmp_path):
    image_path = tmp_path / "text_heavy.png"
    image_path.write_bytes(b"fake image bytes")
    vision_calls = []

    monkeypatch.setattr(
        loaders,
        "analyze_image_features",
        lambda path: {"edge_density": 0.05, "color_count": 24, "entropy": 3.0},
        raising=False,
    )
    monkeypatch.setattr(
        loaders,
        "quick_ocr_probe_image_text",
        lambda path, provider, ocr_languages: "Quarterly pricing policy " * 8,
        raising=False,
    )
    monkeypatch.setattr(
        loaders,
        "extract_image_text",
        lambda path, provider, ocr_languages: "Full OCR text about quarterly pricing policy.",
    )
    monkeypatch.setattr(
        loaders,
        "extract_image_vision_text",
        lambda *args, **kwargs: vision_calls.append(args) or "vision should not run",
        raising=False,
    )

    documents = loaders.load_documents_from_paths(
        [str(image_path)],
        multimodal_enabled=True,
        multimodal_provider="ocr",
        ocr_languages="eng",
        vision_enabled=True,
    )

    assert len(documents) == 1
    assert "Full OCR text" in documents[0].content
    assert "Vision description" not in documents[0].content
    assert documents[0].metadata["image_route"] == "ocr"
    assert vision_calls == []


def test_image_routing_uses_ocr_and_vision_for_diagrams(monkeypatch, tmp_path):
    image_path = tmp_path / "architecture.png"
    image_path.write_bytes(b"fake image bytes")

    monkeypatch.setattr(
        loaders,
        "analyze_image_features",
        lambda path: {"edge_density": 0.22, "color_count": 8, "entropy": 2.6},
        raising=False,
    )
    monkeypatch.setattr(
        loaders,
        "quick_ocr_probe_image_text",
        lambda path, provider, ocr_languages: "API Gateway",
        raising=False,
    )
    monkeypatch.setattr(
        loaders,
        "extract_image_text",
        lambda path, provider, ocr_languages: "API Gateway\nWorker\nMilvus",
    )
    monkeypatch.setattr(
        loaders,
        "extract_image_vision_text",
        lambda path, model, prompt, max_tokens: "Architecture diagram showing request flow.",
        raising=False,
    )

    documents = loaders.load_documents_from_paths(
        [str(image_path)],
        multimodal_enabled=True,
        multimodal_provider="ocr",
        ocr_languages="eng",
        vision_enabled=True,
        vision_model="openai:gpt-4.1-mini",
    )

    assert len(documents) == 1
    assert "OCR text:" in documents[0].content
    assert "API Gateway" in documents[0].content
    assert "Vision description:" in documents[0].content
    assert "request flow" in documents[0].content
    assert documents[0].metadata["image_route"] == "ocr_vision"


def test_image_routing_uses_vision_only_for_photos(monkeypatch, tmp_path):
    image_path = tmp_path / "scene.jpg"
    image_path.write_bytes(b"fake image bytes")
    ocr_calls = []

    monkeypatch.setattr(
        loaders,
        "analyze_image_features",
        lambda path: {"edge_density": 0.08, "color_count": 192, "entropy": 7.2},
        raising=False,
    )
    monkeypatch.setattr(
        loaders,
        "quick_ocr_probe_image_text",
        lambda path, provider, ocr_languages: "",
        raising=False,
    )
    monkeypatch.setattr(
        loaders,
        "extract_image_text",
        lambda *args, **kwargs: ocr_calls.append(args) or "ocr should not run",
    )
    monkeypatch.setattr(
        loaders,
        "extract_image_vision_text",
        lambda path, model, prompt, max_tokens: "Photo of a maintenance team inspecting equipment.",
        raising=False,
    )

    documents = loaders.load_documents_from_paths(
        [str(image_path)],
        multimodal_enabled=True,
        multimodal_provider="ocr",
        ocr_languages="eng",
        vision_enabled=True,
    )

    assert len(documents) == 1
    assert "maintenance team" in documents[0].content
    assert "OCR text:" not in documents[0].content
    assert documents[0].metadata["image_route"] == "vision"
    assert ocr_calls == []


def test_image_routing_skips_low_information_images(monkeypatch, tmp_path):
    image_path = tmp_path / "blank.png"
    image_path.write_bytes(b"fake image bytes")
    extraction_calls = []

    monkeypatch.setattr(
        loaders,
        "analyze_image_features",
        lambda path: {"edge_density": 0.002, "color_count": 1, "entropy": 0.05},
        raising=False,
    )
    monkeypatch.setattr(
        loaders,
        "quick_ocr_probe_image_text",
        lambda path, provider, ocr_languages: "",
        raising=False,
    )
    monkeypatch.setattr(
        loaders,
        "extract_image_text",
        lambda *args, **kwargs: extraction_calls.append("ocr") or "",
    )
    monkeypatch.setattr(
        loaders,
        "extract_image_vision_text",
        lambda *args, **kwargs: extraction_calls.append("vision") or "",
        raising=False,
    )

    documents = loaders.load_documents_from_paths(
        [str(image_path)],
        multimodal_enabled=True,
        multimodal_provider="ocr",
        ocr_languages="eng",
        vision_enabled=True,
    )

    assert documents == []
    assert extraction_calls == []


def test_image_routing_uses_vision_classification_when_rules_are_uncertain(
    monkeypatch,
    tmp_path,
):
    image_path = tmp_path / "uncertain.png"
    image_path.write_bytes(b"fake image bytes")
    classification_calls = []

    monkeypatch.setattr(
        loaders,
        "analyze_image_features",
        lambda path: {"edge_density": 0.11, "color_count": 48, "entropy": 4.2},
        raising=False,
    )
    monkeypatch.setattr(
        loaders,
        "quick_ocr_probe_image_text",
        lambda path, provider, ocr_languages: "queue",
        raising=False,
    )
    monkeypatch.setattr(
        loaders,
        "classify_image_with_vision",
        lambda path, model, prompt, max_tokens: classification_calls.append(path) or "ui",
        raising=False,
    )
    monkeypatch.setattr(
        loaders,
        "extract_image_text",
        lambda path, provider, ocr_languages: "Queue depth: 92",
    )
    monkeypatch.setattr(
        loaders,
        "extract_image_vision_text",
        lambda path, model, prompt, max_tokens: "Monitoring dashboard with queue depth panels.",
        raising=False,
    )

    documents = loaders.load_documents_from_paths(
        [str(image_path)],
        multimodal_enabled=True,
        multimodal_provider="ocr",
        ocr_languages="eng",
        vision_enabled=True,
    )

    assert len(documents) == 1
    assert classification_calls == [image_path.resolve()]
    assert documents[0].metadata["image_route"] == "ocr_vision"
    assert "queue depth panels" in documents[0].content


def test_image_routing_falls_back_to_ocr_when_vision_fails(monkeypatch, tmp_path):
    image_path = tmp_path / "diagram.png"
    image_path.write_bytes(b"fake image bytes")

    monkeypatch.setattr(
        loaders,
        "analyze_image_features",
        lambda path: {"edge_density": 0.24, "color_count": 10, "entropy": 2.8},
        raising=False,
    )
    monkeypatch.setattr(
        loaders,
        "quick_ocr_probe_image_text",
        lambda path, provider, ocr_languages: "Worker",
        raising=False,
    )
    monkeypatch.setattr(
        loaders,
        "extract_image_text",
        lambda path, provider, ocr_languages: "Worker -> Indexer",
    )

    def failing_vision(*_args, **_kwargs):
        raise RuntimeError("vision unavailable")

    monkeypatch.setattr(loaders, "extract_image_vision_text", failing_vision, raising=False)

    documents = loaders.load_documents_from_paths(
        [str(image_path)],
        multimodal_enabled=True,
        multimodal_provider="ocr",
        ocr_languages="eng",
        vision_enabled=True,
    )

    assert len(documents) == 1
    assert "Worker -> Indexer" in documents[0].content
    assert documents[0].metadata["image_route"] == "ocr_vision"
    assert documents[0].metadata["vision_error"] == "vision unavailable"


def test_query_image_context_reuses_routed_image_bytes(monkeypatch):
    from open_deep_research.rag import query_images

    calls = []

    def fake_extract(
        image_bytes,
        provider,
        ocr_languages,
        vision_enabled,
        vision_model,
        vision_prompt,
        vision_max_tokens,
    ):
        calls.append(
            {
                "image_bytes": image_bytes,
                "provider": provider,
                "ocr_languages": ocr_languages,
                "vision_enabled": vision_enabled,
                "vision_model": vision_model,
                "vision_prompt": vision_prompt,
                "vision_max_tokens": vision_max_tokens,
            }
        )
        return (
            "OCR text:\nRevenue funnel\n\nVision description:\nChart shows conversion drop-off.",
            {"image_route": "ocr_vision", "image_route_reason": "diagram_like"},
        )

    monkeypatch.setattr(
        query_images,
        "extract_routed_image_bytes_text",
        fake_extract,
    )

    context = query_images.build_query_image_context(
        [
            HumanMessage(
                content=[
                    {"type": "text", "text": "这张图和知识库里的漏斗指标有什么关系？"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,ZmFrZSBpbWFnZSBieXRlcw=="
                        },
                    },
                ]
            )
        ],
        provider="ocr",
        ocr_languages="eng+chi_sim",
        vision_enabled=True,
        vision_model="openai:gpt-4.1-mini",
        vision_prompt="Describe the image.",
        vision_max_tokens=512,
        max_images=3,
    )

    assert "User image 1" in context
    assert "Route: ocr_vision" in context
    assert "conversion drop-off" in context
    assert calls == [
        {
            "image_bytes": b"fake image bytes",
            "provider": "ocr",
            "ocr_languages": "eng+chi_sim",
            "vision_enabled": True,
            "vision_model": "openai:gpt-4.1-mini",
            "vision_prompt": "Describe the image.",
            "vision_max_tokens": 512,
        }
    ]


def test_enrich_query_images_appends_synthetic_context_once(monkeypatch):
    from open_deep_research import deep_researcher

    calls = []

    def fake_context(messages, **kwargs):
        calls.append({"messages": messages, "kwargs": kwargs})
        return "User image 1\nRoute: vision\nVision description:\nUploaded UI screenshot."

    monkeypatch.setattr(
        deep_researcher.query_images,
        "build_query_image_context",
        fake_context,
    )

    state = {
        "messages": [
            HumanMessage(
                content=[
                    {"type": "text", "text": "这个截图说明了什么？"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,ZmFrZQ=="},
                    },
                ]
            )
        ]
    }

    result = asyncio.run(deep_researcher.enrich_query_images(state, config={}))
    second_result = asyncio.run(
        deep_researcher.enrich_query_images(
            {"messages": [*state["messages"], *result["messages"]]},
            config={},
        )
    )

    assert len(result["messages"]) == 1
    assert "Recognized image context from the user's question" in result["messages"][0].content
    assert "Uploaded UI screenshot" in result["messages"][0].content
    assert result["messages"][0].additional_kwargs["rag_query_image_context"] is True
    assert second_result == {}
    assert len(calls) == 1


def test_split_documents_creates_chunk_ids_and_preserves_metadata():
    document = RAGDocument(
        content=(
            "Atlas Launch requires quarterly risk reviews.\n\n"
            "The release process includes two approvals, a rollback plan, and a release note. "
            * 8
        ),
        source="memory://atlas",
        title="Atlas handbook",
        metadata={"team": "atlas"},
    )

    chunks = split_documents([document], chunk_size=120, chunk_overlap=20)

    assert len(chunks) >= 2
    assert all(chunk.chunk_id for chunk in chunks)
    assert all(chunk.source == "memory://atlas" for chunk in chunks)
    assert all(chunk.metadata["team"] == "atlas" for chunk in chunks)
    assert all("line_start" in chunk.metadata for chunk in chunks)
    assert all("heading_path" in chunk.metadata for chunk in chunks)
    assert all(len(chunk.content) <= 120 for chunk in chunks)


def test_split_documents_uses_recursive_character_boundaries():
    document = RAGDocument(
        content=(
            "Alpha sentence one. Beta sentence two. "
            "Gamma sentence three. Delta sentence four."
        ),
        source="data/knowledge/sentences.md",
        title="Sentences",
    )

    chunks = split_documents([document], chunk_size=35, chunk_overlap=8)

    assert [chunk.content for chunk in chunks] == [
        "Alpha sentence one.",
        "Beta sentence two.",
        "Gamma sentence three.",
        "Delta sentence four.",
    ]
    assert [chunk.metadata["char_start"] for chunk in chunks] == [0, 20, 39, 61]


def test_split_documents_splits_markdown_by_headers_without_crossing_sections():
    document = RAGDocument(
        content=textwrap.dedent(
            """
            # Product Guide

            Intro overview belongs only to the product guide.

            ## Auth

            Auth overview belongs only to auth.

            ### Login

            Login details stay under the login section.

            ## Billing

            Billing details stay under the billing section.
            """
        ).strip(),
        source="data/knowledge/product.md",
        title="Product Guide",
        metadata={"extension": ".md", "path": "data/knowledge/product.md"},
    )

    chunks = split_documents([document], chunk_size=80, chunk_overlap=10)
    login_chunks = [
        chunk for chunk in chunks if chunk.metadata.get("h3") == "Login"
    ]
    billing_chunks = [
        chunk for chunk in chunks if chunk.metadata.get("h2") == "Billing"
    ]

    assert login_chunks
    assert billing_chunks
    assert login_chunks[0].metadata["h1"] == "Product Guide"
    assert login_chunks[0].metadata["h2"] == "Auth"
    assert login_chunks[0].metadata["file_type"] == "markdown"
    assert all("Billing details" not in chunk.content for chunk in login_chunks)
    assert all("Login details" not in chunk.content for chunk in billing_chunks)
    assert all("char_start" in chunk.metadata for chunk in chunks)
    assert all("char_end" in chunk.metadata for chunk in chunks)


def test_split_documents_groups_json_item_context_and_splits_long_fields():
    long_text = " ".join(f"alpha-{index}" for index in range(80))
    document = RAGDocument(
        content=json.dumps(
            {
                "posts": [
                    {"title": "First item title", "content": long_text},
                    {"title": "Second item title", "published": True},
                ],
                "count": 2,
            },
            ensure_ascii=False,
        ),
        source="data/knowledge/posts.json",
        title="Posts",
        metadata={"extension": ".json", "path": "data/knowledge/posts.json"},
    )

    chunks = split_documents([document], chunk_size=80, chunk_overlap=10)
    by_path = {}
    for chunk in chunks:
        by_path.setdefault(chunk.metadata.get("json_path"), []).append(chunk)

    assert "$.posts[0]" in by_path
    assert "$.posts[1]" in by_path
    assert "$.count" in by_path
    assert len(by_path["$.posts[0]"]) > 1
    assert all(
        chunk.metadata["primary_json_path"] == "$.posts[0].content"
        for chunk in by_path["$.posts[0]"]
    )
    assert all(
        chunk.metadata["field_paths"] == ["$.posts[0].title", "$.posts[0].content"]
        for chunk in by_path["$.posts[0]"]
    )
    assert all("title: First item title" in chunk.content for chunk in by_path["$.posts[0]"])
    assert any("content:" in chunk.content for chunk in by_path["$.posts[0]"])
    assert by_path["$.posts[1]"][0].metadata["field_paths"] == [
        "$.posts[1].title",
        "$.posts[1].published",
    ]
    assert "title: Second item title" in by_path["$.posts[1]"][0].content
    assert "published: true" in by_path["$.posts[1]"][0].content
    assert by_path["$.count"][0].content == "count: 2"
    assert all(chunk.metadata["file_type"] == "json" for chunk in chunks)
    assert all(
        "Second item title" not in chunk.content
        for chunk in by_path["$.posts[0]"]
    )


def test_split_documents_keeps_json_scalar_array_items_separate():
    document = RAGDocument(
        content=json.dumps({"tags": ["alpha", "beta"]}, ensure_ascii=False),
        source="data/knowledge/tags.json",
        title="Tags",
        metadata={"extension": ".json", "path": "data/knowledge/tags.json"},
    )

    chunks = split_documents([document], chunk_size=80, chunk_overlap=10)
    by_path = {chunk.metadata.get("json_path"): chunk for chunk in chunks}

    assert by_path["$.tags[0]"].content == "[0]: alpha"
    assert by_path["$.tags[1]"].content == "[1]: beta"


def test_split_documents_keeps_text_metadata_chinese_boundaries_and_overlap():
    document = RAGDocument(
        content=(
            "第一句说明甲。第二句说明乙？"
            + "连续文本" * 30
            + "，最后一句收尾。"
        ),
        source="data/knowledge/notes.txt",
        title="Notes",
        metadata={"extension": ".txt", "path": "data/knowledge/notes.txt"},
    )

    chunks = split_documents([document], chunk_size=35, chunk_overlap=8)

    assert len(chunks) > 2
    assert all(len(chunk.content) <= 35 for chunk in chunks)
    assert all(chunk.metadata["file_type"] == "text" for chunk in chunks)
    assert all(chunk.metadata["source"] == "data/knowledge/notes.txt" for chunk in chunks)
    assert any(chunk.content.endswith(("。", "？")) for chunk in chunks)
    assert any(
        chunks[index].metadata["char_start"] < chunks[index - 1].metadata["char_end"]
        for index in range(1, len(chunks))
    )


def test_split_documents_uses_recursive_language_splitter_for_code(monkeypatch):
    calls = []
    original_from_language = rag_splitter.RecursiveCharacterTextSplitter.from_language

    def recording_from_language(language, **kwargs):
        calls.append((language, kwargs))
        return original_from_language(language=language, **kwargs)

    monkeypatch.setattr(
        rag_splitter.RecursiveCharacterTextSplitter,
        "from_language",
        staticmethod(recording_from_language),
    )
    document = RAGDocument(
        content=textwrap.dedent(
            """
            import hashlib

            def authenticate_user(token: str) -> bool:
                digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
                return digest.startswith("00")

            def rotate_api_key(user_id: str) -> str:
                return f"rotated-{user_id}"
            """
        ).strip(),
        source="data/knowledge/auth_service.py",
        title="auth_service.py",
        metadata={
            "extension": ".py",
            "file_type": "code",
            "language": "python",
        },
    )

    chunks = split_documents([document], chunk_size=120, chunk_overlap=10)

    assert calls
    assert calls[0][0] == Language.PYTHON
    assert calls[0][1]["chunk_size"] == 120
    assert calls[0][1]["chunk_overlap"] == 10
    assert all(chunk.metadata["file_type"] == "code" for chunk in chunks)
    assert all(chunk.metadata["language"] == "python" for chunk in chunks)
    assert all("line_start" in chunk.metadata for chunk in chunks)
    assert any("def authenticate_user" in chunk.content for chunk in chunks)


def test_knowledge_json_loader_preserves_raw_json_for_structured_split(tmp_path):
    json_path = tmp_path / "faq.json"
    json_path.write_text(
        json.dumps(
            {"faq": [{"question": "How many approvals?", "answer": "Two approvals."}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    documents = loaders.load_documents_from_paths([str(json_path)])
    chunks = split_documents(documents, chunk_size=120, chunk_overlap=10)

    assert documents[0].content.lstrip().startswith("{")
    assert any(
        "$.faq[0].answer" in chunk.metadata.get("field_paths", [])
        for chunk in chunks
    )


def test_split_documents_marks_source_authority_metadata():
    documents = [
        RAGDocument(
            content="# Atlas Handbook\n\nCurrent release policy.",
            source="C:/repo/data/knowledge/team_handbook.md",
            title="Atlas handbook",
        ),
        RAGDocument(
            content="# Atlas Handbook\n\n## Deprecated guidance archive\n\nOld release policy.",
            source="C:/repo/data/knowledge/team_handbook.md",
            title="Atlas handbook",
        ),
        RAGDocument(
            content="# Atlas Launch Misleading Archive\n\n1. [deprecated] Wrong rule.",
            source="C:/repo/data/knowledge/misleading_archive.md",
            title="Misleading archive",
        ),
    ]

    chunks = split_documents(documents, chunk_size=300, chunk_overlap=20)

    assert chunks[0].metadata["source_status"] == "authoritative"
    assert chunks[0].metadata["authority_weight"] == 1.0
    assert chunks[1].metadata["source_status"] == "deprecated"
    assert chunks[1].metadata["authority_score_penalty"] < 0
    assert chunks[2].metadata["source_status"] == "misleading"
    assert chunks[2].metadata["authority_score_penalty"] < chunks[1].metadata["authority_score_penalty"]


def test_authority_adjustment_downranks_misleading_rerank_hits():
    authoritative = RAGChunk(
        content="Current rule: rollback owners stay online until verification completes.",
        source="runbook.txt",
        title="Runbook",
        chunk_id="auth",
        metadata={"source_status": "authoritative", "authority_score_penalty": 0.0},
    )
    misleading = RAGChunk(
        content="Misleading archive repeats the same rollback owner terms.",
        source="misleading_archive.md",
        title="Archive",
        chunk_id="misleading",
        metadata={"source_status": "misleading", "authority_score_penalty": -8.0},
    )
    results = [
        RetrievalResult(
            chunk=misleading,
            score=0.9,
            vector_score=0.9,
            rerank_score=8.0,
        ),
        RetrievalResult(
            chunk=authoritative,
            score=0.4,
            vector_score=0.4,
            rerank_score=4.0,
        ),
    ]

    adjusted = apply_authority_adjustment(results)

    assert [result.chunk.chunk_id for result in adjusted] == ["auth", "misleading"]
    assert adjusted[0].score == pytest.approx(4.0)
    assert adjusted[1].score == pytest.approx(0.0)
    assert adjusted[1].chunk.metadata["authority_adjusted_score"] == pytest.approx(0.0)


def test_authority_filter_blocks_misleading_final_context():
    pipeline = object.__new__(RAGPipeline)
    pipeline.config = types.SimpleNamespace(authority_rerank_enabled=True)
    authoritative = RetrievalResult(
        chunk=RAGChunk(
            content="Current release policy.",
            source="runbook.txt",
            title="Runbook",
            chunk_id="auth",
            metadata={"source_status": "authoritative"},
        ),
        score=1.0,
        rerank_score=1.0,
    )
    misleading = RetrievalResult(
        chunk=RAGChunk(
            content="Wrong release policy.",
            source="misleading_archive.md",
            title="Archive",
            chunk_id="misleading",
            metadata={"source_status": "misleading"},
        ),
        score=10.0,
        rerank_score=10.0,
    )

    filtered = pipeline._filter_results([misleading, authoritative])

    assert [result.chunk.chunk_id for result in filtered] == ["auth"]


def test_rag_pipeline_retrieves_relevant_local_context(tmp_path):
    write_text_file(
        tmp_path / "handbook.md",
        """
        # Atlas Launch Handbook

        Atlas Launch requires quarterly risk reviews and two approvals before production releases.
        """,
    )
    write_text_file(
        tmp_path / "notes.txt",
        """
        The cafeteria serves noodles on Tuesdays.
        """,
    )

    pipeline = RAGPipeline(
        RAGPipelineConfig(
            knowledge_base_paths=[str(tmp_path)],
            chunk_size=180,
            chunk_overlap=20,
            top_k=2,
            rerank_top_n=3,
            embedding_provider="sentence_transformers",
            vectorstore_provider="memory",
            reranker_provider="simple",
        )
    )

    result = pipeline.query("Which team requires quarterly risk reviews before releases?")

    assert result.citations
    assert "quarterly risk reviews" in result.context.lower()
    assert any("handbook.md" in citation.source for citation in result.citations)


def test_rag_pipeline_retrieves_code_file_context(tmp_path):
    write_text_file(
        tmp_path / "auth_service.py",
        """
        def authenticate_user(token: str) -> bool:
            if not token.startswith("user_"):
                return False
            return validate_token_signature(token)


        def validate_token_signature(token: str) -> bool:
            return token.endswith("_signed")
        """,
    )

    pipeline = RAGPipeline(
        RAGPipelineConfig(
            knowledge_base_paths=[str(tmp_path)],
            chunk_size=220,
            chunk_overlap=20,
            top_k=2,
            rerank_top_n=3,
            embedding_provider="hash",
            vectorstore_provider="memory",
            reranker_provider="simple",
            multimodal_enabled=False,
            vision_enabled=False,
        )
    )

    result = pipeline.query("Where is authenticate_user token signature checked?")

    assert result.citations
    assert "authenticate_user" in result.context
    assert "validate_token_signature" in result.context
    assert any("auth_service.py" in citation.source for citation in result.citations)


def test_rag_pipeline_uses_rewritten_query_but_returns_original_query():
    chunk = RAGChunk(
        content="Milvus Lite stores the vector index under data/indexes/rag.",
        source="data/knowledge/rag.md",
        title="RAG Notes",
        chunk_id="chunk-1",
        metadata={"source": "data/knowledge/rag.md"},
    )
    retrieval_result = RetrievalResult(chunk=chunk, score=0.5, vector_score=0.5)
    calls = {}

    class FakeEmbeddingBackend:
        def embed_query(self, text):
            calls["embedded_query"] = text
            return [1.0]

    class FakeRetriever:
        def retrieve(self, *, query, query_vector, top_k, keyword_top_k):
            calls["retriever_query"] = query
            calls["query_vector"] = query_vector
            return [retrieval_result]

    class FakeReranker:
        def rerank(self, query, results, top_k):
            calls["reranker_query"] = query
            return [
                item.model_copy(update={"rerank_score": 1.0})
                for item in results
            ]

    pipeline = RAGPipeline(
        RAGPipelineConfig(
            knowledge_base_paths=[],
            embedding_provider="hash",
            vectorstore_provider="memory",
            reranker_provider="simple",
            authority_rerank_enabled=False,
        )
    )
    pipeline.indexer.ensure_ready = lambda force=False: None
    pipeline.indexer.documents = [
        RAGDocument(
            content=chunk.content,
            source=chunk.source,
            title=chunk.title,
            metadata={},
        )
    ]
    pipeline.indexer.chunks = [chunk]
    pipeline.indexer.embedding_backend = FakeEmbeddingBackend()
    pipeline.indexer.retriever = FakeRetriever()
    pipeline.reranker = FakeReranker()

    answer = pipeline.query(
        "Milvus vector index path",
        original_query="where is it?",
    )

    assert answer.query == "where is it?"
    assert calls["embedded_query"] == "Milvus vector index path"
    assert calls["retriever_query"] == "Milvus vector index path"
    assert calls["reranker_query"] == "Milvus vector index path"


def test_rag_pipeline_handles_empty_documents(tmp_path):
    write_text_file(tmp_path / "empty.md", "")

    pipeline = RAGPipeline(
        RAGPipelineConfig(
            knowledge_base_paths=[str(tmp_path)],
            chunk_size=100,
            chunk_overlap=10,
            embedding_provider="sentence_transformers",
            vectorstore_provider="memory",
            reranker_provider="simple",
        )
    )

    result = pipeline.query("anything")

    assert "No local RAG documents or memory records were loaded" in result.context
    assert result.citations == []


def test_rag_pipeline_handles_empty_results(tmp_path):
    write_text_file(
        tmp_path / "policy.txt",
        """
        Budget approvals are reviewed by the finance committee every month.
        """,
    )

    pipeline = RAGPipeline(
        RAGPipelineConfig(
            knowledge_base_paths=[str(tmp_path)],
            chunk_size=140,
            chunk_overlap=10,
            top_k=2,
            rerank_top_n=3,
            embedding_provider="sentence_transformers",
            vectorstore_provider="memory",
            reranker_provider="simple",
        )
    )

    result = pipeline.query("quantum entanglement experiment schedule")

    assert "No relevant local knowledge base results were found" in result.context
    assert result.citations == []


def test_rag_pipeline_retrieves_chat_memory(tmp_path):
    memory_path = tmp_path / "chat_memory.jsonl"
    write_jsonl_file(
        memory_path,
        [
            {
                "memory_id": "pref-1",
                "conversation_id": "thread-1",
                "memory_type": "user_preference",
                "content": "The user prefers Chinese explanations and likes process-first technical walkthroughs.",
                "created_at": "2026-05-14T09:00:00+08:00",
                "confidence": 0.95,
            },
            {
                "memory_id": "noise-1",
                "conversation_id": "thread-1",
                "memory_type": "temporary_note",
                "content": "Lunch discussion about noodles.",
            },
        ],
    )

    pipeline = RAGPipeline(
        RAGPipelineConfig(
            knowledge_base_paths=[],
            memory_enabled=True,
            memory_paths=[str(memory_path)],
            chunk_size=180,
            chunk_overlap=20,
            top_k=2,
            rerank_top_n=3,
            embedding_provider="sentence_transformers",
            vectorstore_provider="memory",
            reranker_provider="simple",
        )
    )

    result = pipeline.query("What explanation style does the user prefer?")

    assert result.citations
    assert "Chinese explanations" in result.context
    assert any(citation.source.startswith("memory://thread-1/") for citation in result.citations)
    assert "source_type=memory" in result.context


def test_rag_index_id_stays_stable_when_file_changes(tmp_path):
    document_path = tmp_path / "policy.txt"
    write_text_file(document_path, "first policy")
    config = RAGPipelineConfig(
        knowledge_base_paths=[str(tmp_path)],
        embedding_provider="sentence_transformers",
        vectorstore_provider="memory",
        reranker_provider="simple",
    )

    first_index_id = build_rag_index_id(config)
    write_text_file(document_path, "changed policy")
    second_index_id = build_rag_index_id(config)

    assert first_index_id == second_index_id


def test_pipeline_refreshes_same_index_when_file_changes(tmp_path):
    document_path = tmp_path / "policy.txt"
    write_text_file(document_path, "first policy requires alpha review")
    pipeline = RAGPipeline(
        RAGPipelineConfig(
            knowledge_base_paths=[str(tmp_path)],
            embedding_provider="sentence_transformers",
            vectorstore_provider="memory",
            reranker_provider="simple",
            top_k=1,
        )
    )

    first_result = pipeline.query("alpha review")
    write_text_file(document_path, "changed policy requires beta review")
    second_result = pipeline.query("beta review")

    assert "alpha review" in first_result.context
    assert "beta review" in second_result.context


def test_rag_index_id_stays_stable_when_memory_file_changes(tmp_path):
    memory_path = tmp_path / "chat_memory.jsonl"
    write_jsonl_file(memory_path, [{"memory_id": "m1", "content": "first memory"}])
    config = RAGPipelineConfig(
        knowledge_base_paths=[],
        memory_enabled=True,
        memory_paths=[str(memory_path)],
        embedding_provider="sentence_transformers",
        vectorstore_provider="memory",
        reranker_provider="simple",
    )

    first_index_id = build_rag_index_id(config)
    write_jsonl_file(memory_path, [{"memory_id": "m1", "content": "changed memory"}])
    second_index_id = build_rag_index_id(config)

    assert first_index_id == second_index_id


def test_mysql_memory_records_convert_to_rag_documents():
    records = build_chat_memory_records(
        conversation_id="thread-1",
        chat_content="user: Please answer in Chinese.",
        summary="The user asked for a Chinese RAG walkthrough.",
        memories=["The user prefers Chinese technical explanations."],
        metadata={"owner": "user-1"},
    )

    assert [record.record_type for record in records] == [
        "chat_raw",
        "summary",
        "project_fact",
    ]
    assert len({record.memory_id for record in records}) == 3

    documents = records_to_documents(records)

    assert [document.metadata["memory_type"] for document in documents] == [
        "summary",
        "project_fact",
    ]
    assert all(document.source.startswith("memory://mysql/thread-1/") for document in documents)
    assert all(document.metadata["source_type"] == "memory" for document in documents)
    assert all(document.metadata["memory_backend"] == "mysql" for document in documents)
    assert "Chinese technical explanations" in documents[-1].content

    all_documents = records_to_documents(
        records,
        index_record_types=["chat_raw", "summary", "project_fact"],
    )
    assert [document.metadata["memory_type"] for document in all_documents] == [
        "chat_raw",
        "summary",
        "project_fact",
    ]
    conversation_id, memory_id = parse_mysql_memory_source(documents[-1].source)
    assert conversation_id == "thread-1"
    assert memory_id == records[-1].memory_id
    assert parse_mysql_memory_source("memory://thread-1/not-mysql") is None
    assert parse_mysql_memory_source("memory://mysql/thread/with/slash/memory-1") == (
        "thread/with/slash",
        "memory-1",
    )


def test_mysql_memory_helpers_validate_connection_settings():
    assert (
        normalize_mysql_url("mysql://user:pass@localhost:3306/rag")
        == "mysql+pymysql://user:pass@localhost:3306/rag"
    )
    assert validate_table_name("rag_chat_memories") == "rag_chat_memories"
    assert normalize_record_types(["summary", "memory", "memory"]) == [
        "summary",
        "project_fact",
    ]
    with pytest.raises(ValueError):
        validate_table_name("rag_chat_memories;drop")
    with pytest.raises(ValueError):
        normalize_record_types(["debug"])


def test_rag_index_id_stays_stable_for_mysql_memory_scope():
    config = RAGPipelineConfig(
        knowledge_base_paths=[],
        memory_enabled=True,
        memory_mysql_url="mysql+pymysql://user:pass@localhost:3306/rag",
        embedding_provider="sentence_transformers",
        vectorstore_provider="memory",
        reranker_provider="simple",
    )

    first_index_id = build_rag_index_id(config)
    second_index_id = build_rag_index_id(config)

    assert first_index_id == second_index_id


def test_rag_index_id_changes_for_vision_extraction_settings():
    base_config = RAGPipelineConfig(
        knowledge_base_paths=["data/knowledge"],
        embedding_provider="sentence_transformers",
        vectorstore_provider="memory",
        reranker_provider="simple",
    )
    changed_model_config = base_config.model_copy(
        update={"vision_model": "openai:gpt-4.1"}
    )
    changed_prompt_config = base_config.model_copy(
        update={"vision_prompt": "Describe charts and UI only."}
    )

    assert build_rag_index_id(base_config) != build_rag_index_id(changed_model_config)
    assert build_rag_index_id(base_config) != build_rag_index_id(changed_prompt_config)


def test_hybrid_mode_exposes_web_and_rag_tools(tmp_path):
    write_text_file(
        tmp_path / "handbook.md",
        """
        Atlas Launch requires quarterly risk reviews.
        """,
    )

    hybrid_tools = asyncio.run(
        get_all_tools(
            {
                "configurable": {
                    "search_api": "tavily",
                    "rag_enabled": True,
                    "retrieval_mode": "hybrid",
                    "rag_knowledge_base_paths": [str(tmp_path)],
                    "rag_embedding_provider": "sentence_transformers",
                    "rag_vectorstore_provider": "memory",
                    "rag_reranker_provider": "simple",
                }
            }
        )
    )
    hybrid_names = [
        tool.name if hasattr(tool, "name") else tool.get("name", "web_search")
        for tool in hybrid_tools
    ]

    rag_only_tools = asyncio.run(
        get_all_tools(
            {
                "configurable": {
                    "search_api": "tavily",
                    "rag_enabled": True,
                    "retrieval_mode": "rag_only",
                    "rag_knowledge_base_paths": [str(tmp_path)],
                    "rag_embedding_provider": "sentence_transformers",
                    "rag_vectorstore_provider": "memory",
                    "rag_reranker_provider": "simple",
                }
            }
        )
    )
    rag_only_names = [
        tool.name if hasattr(tool, "name") else tool.get("name", "web_search")
        for tool in rag_only_tools
    ]

    assert "web_search" in hybrid_names
    assert "rag_search" in hybrid_names
    assert "rag_search" in rag_only_names
    assert "web_search" not in rag_only_names


def test_hybrid_mode_with_budget_exposes_web_and_rag_tools(tmp_path):
    write_text_file(
        tmp_path / "handbook.md",
        """
        Atlas Launch requires quarterly risk reviews.
        """,
    )

    tools = asyncio.run(
        get_all_tools(
            {
                "configurable": {
                    "search_api": "tavily",
                    "rag_enabled": True,
                    "retrieval_mode": "hybrid",
                    "rag_knowledge_base_paths": [str(tmp_path)],
                    "rag_embedding_provider": "sentence_transformers",
                    "rag_vectorstore_provider": "memory",
                    "rag_reranker_provider": "simple",
                    "budget_enabled": True,
                    "max_tool_calls": 4,
                    "max_search_calls": 4,
                }
            }
        )
    )
    tool_names = [
        tool.name if hasattr(tool, "name") else tool.get("name", "web_search")
        for tool in tools
    ]

    assert "web_search" in tool_names
    assert "rag_search" in tool_names


def test_rag_only_memory_exposes_rag_tool_without_document_paths(tmp_path):
    memory_path = tmp_path / "chat_memory.jsonl"
    write_jsonl_file(memory_path, [{"memory_id": "m1", "content": "The user prefers concise answers."}])

    tools = asyncio.run(
        get_all_tools(
            {
                "configurable": {
                    "search_api": "none",
                    "rag_enabled": True,
                    "retrieval_mode": "rag_only",
                    "rag_memory_enabled": True,
                    "rag_memory_paths": [str(memory_path)],
                    "rag_embedding_provider": "sentence_transformers",
                    "rag_vectorstore_provider": "memory",
                    "rag_reranker_provider": "simple",
                }
            }
        )
    )
    tool_names = [
        tool.name if hasattr(tool, "name") else tool.get("name", "web_search")
        for tool in tools
    ]

    assert "rag_search" in tool_names
    assert "web_search" not in tool_names


def test_rag_only_mysql_memory_exposes_rag_tool_without_document_paths():
    tools = asyncio.run(
        get_all_tools(
            {
                "configurable": {
                    "search_api": "none",
                    "rag_enabled": True,
                    "retrieval_mode": "rag_only",
                    "rag_memory_enabled": True,
                    "rag_memory_mysql_url": "mysql+pymysql://user:pass@localhost:3306/rag",
                    "rag_embedding_provider": "sentence_transformers",
                    "rag_vectorstore_provider": "memory",
                    "rag_reranker_provider": "simple",
                }
            }
        )
    )
    tool_names = [
        tool.name if hasattr(tool, "name") else tool.get("name", "web_search")
        for tool in tools
    ]

    assert "rag_search" in tool_names
    assert "web_search" not in tool_names
