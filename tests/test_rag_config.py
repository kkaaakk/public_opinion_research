"""Regression coverage for the external-to-canonical RAG config boundary."""

import pytest

from open_deep_research.configuration import Configuration
from open_deep_research.rag.config import RAGConfig, rag_config_to_flat_dict
from open_deep_research.rag.service import (
    RAGPipelineConfig,
    build_rag_config,
    build_rag_pipeline_config,
    rag_config_from_configuration,
)


def _external_rag_values(config: Configuration) -> dict[str, object]:
    return {
        field_name.removeprefix("rag_"): getattr(config, field_name)
        for field_name in type(config).model_fields
        if field_name.startswith("rag_")
    }


def test_configuration_defaults_map_to_canonical_without_field_loss() -> None:
    external = Configuration()
    canonical = rag_config_from_configuration(external)
    flat = rag_config_to_flat_dict(canonical)

    assert isinstance(canonical, RAGConfig)
    assert _external_rag_values(external).items() <= flat.items()
    assert set(_external_rag_values(external)) == set(flat) - {
        "memory_conversation_id",
        "memory_user_id",
    }


@pytest.mark.parametrize(
    ("group", "overrides"),
    [
        (
            "embedding",
            {
                "rag_embedding_provider": "hash",
                "rag_embedding_model": "custom-embedding",
                "rag_embedding_device": "cpu",
                "rag_hash_embedding_dimensions": 64,
            },
        ),
        (
            "vectorstore",
            {
                "rag_vectorstore_provider": "memory",
                "rag_vectorstore_path": "custom/index",
                "rag_collection_name": "custom_collection",
                "rag_milvus_uri": "https://milvus.example.test",
                "rag_milvus_token": "token",
                "rag_milvus_db_name": "research",
                "rag_milvus_metric_type": "IP",
            },
        ),
        (
            "reranker",
            {
                "rag_reranker_provider": "simple",
                "rag_reranker_model": "custom-reranker",
                "rag_reranker_device": "cpu",
            },
        ),
        (
            "multimodal",
            {
                "rag_multimodal_enabled": False,
                "rag_multimodal_provider": "disabled",
                "rag_ocr_languages": "eng",
                "rag_vision_enabled": False,
                "rag_vision_model": "openai:gpt-4.1",
                "rag_vision_prompt": "Describe this image.",
                "rag_vision_max_tokens": 128,
            },
        ),
        (
            "memory",
            {
                "rag_memory_enabled": True,
                "rag_memory_paths": ["memory.jsonl"],
                "rag_memory_json_text_fields": ["content"],
                "rag_memory_mysql_url": "mysql+pymysql://user:pass@host/db",
                "rag_memory_mysql_table": "memories",
                "rag_memory_mysql_limit": 25,
                "rag_memory_mysql_index_record_types": ["chat", "summary"],
                "rag_memory_write_enabled": True,
                "rag_memory_write_sync_index": False,
            },
        ),
        (
            "keyword",
            {
                "rag_keyword_top_k": 9,
                "rag_keyword_backend": "elasticsearch",
                "rag_elasticsearch_url": "http://elastic.example.test:9200",
                "rag_elasticsearch_index": "custom_chunks",
            },
        ),
        (
            "hybrid",
            {
                "rag_rrf_rank_constant": 33,
                "rag_structured_metadata_weight": 0.4,
            },
        ),
        (
            "graph",
            {
                "rag_graph_enabled": True,
                "rag_graph_backend": "memory",
                "rag_graph_max_neighbors": 2,
                "rag_graph_weight": 0.2,
                "rag_graph_ner_enabled": False,
                "rag_graph_idf_enabled": False,
                "rag_graph_idf_threshold_percentile": 70.0,
                "rag_graph_confidence_threshold": 0.3,
                "rag_structural_edges_enabled": True,
                "rag_neo4j_uri": "bolt://neo4j.example.test:7687",
                "rag_neo4j_username": "researcher",
                "rag_neo4j_password": "secret",
                "rag_neo4j_database": "research",
            },
        ),
        (
            "chunking",
            {
                "rag_knowledge_base_paths": ["knowledge"],
                "rag_chunk_size": 800,
                "rag_chunk_overlap": 80,
                "rag_top_k": 7,
                "rag_rerank_top_n": 14,
                "rag_json_text_fields": ["body"],
                "rag_authority_rerank_enabled": False,
            },
        ),
        (
            "query",
            {
                "rag_query_image_enabled": False,
                "rag_query_image_max_images": 1,
                "rag_query_image_max_bytes": 1024,
                "rag_query_rewrite_enabled": False,
                "rag_query_rewrite_model": "openai:gpt-4.1-mini",
                "rag_query_rewrite_max_tokens": 64,
            },
        ),
    ],
)
def test_grouped_configuration_overrides_reach_canonical_config(
    group: str,
    overrides: dict[str, object],
) -> None:
    del group
    canonical = rag_config_from_configuration(Configuration(**overrides))
    flat = rag_config_to_flat_dict(canonical)

    for external_name, expected in overrides.items():
        assert flat[external_name.removeprefix("rag_")] == expected


def test_partial_mapping_override_preserves_every_other_default() -> None:
    defaults = build_rag_config()
    overridden = build_rag_config({"rag_embedding_provider": "hash"})

    assert overridden.embedding.provider == "hash"
    assert overridden.embedding.model == defaults.embedding.model
    assert overridden.vectorstore == defaults.vectorstore
    assert overridden.chunking == defaults.chunking
    assert overridden.query == defaults.query


def test_runnable_config_conversion_preserves_values_and_runtime_identity() -> None:
    runtime_config = {
        "configurable": {
            "rag_embedding_provider": "hash",
            "rag_top_k": 6,
            "conversation_id": "conversation-7",
            "user_id": "user-3",
        }
    }
    external = Configuration.from_runnable_config(runtime_config)
    canonical = build_rag_config(external, runtime_config)

    assert canonical.embedding.provider == "hash"
    assert canonical.chunking.top_k == 6
    assert canonical.memory.conversation_id == "conversation-7"
    assert canonical.memory.user_id == "user-3"


def test_pipeline_config_remains_a_flat_compatibility_facade() -> None:
    legacy = RAGPipelineConfig(
        embedding_provider="hash",
        vectorstore_path="custom/index",
        graph_enabled=True,
    )

    assert legacy.embedding_provider == "hash"
    assert legacy.embedding.provider == "hash"
    assert legacy.vectorstore_path == "custom/index"
    assert legacy.vectorstore.persist_path == "custom/index"
    assert legacy.milvus_uri == "custom/index/milvus.db"
    assert legacy.graph_enabled is True
    assert "embedding" not in legacy.model_dump()
    assert legacy.model_dump()["embedding_provider"] == "hash"

    validated = RAGPipelineConfig.model_validate(legacy.model_dump())
    assert validated.embedding_provider == "hash"
    assert validated.vectorstore_path == "custom/index"


def test_compatibility_round_trip_keeps_all_historical_pipeline_fields() -> None:
    original = RAGPipelineConfig(
        embedding_provider="hash",
        vectorstore_provider="memory",
        memory_enabled=True,
        memory_paths=["memory.jsonl"],
    )
    rebuilt = build_rag_pipeline_config(original.model_dump())

    assert rebuilt.model_dump() == original.model_dump()
    assert rebuilt.embedding == original.embedding
    assert rebuilt.memory == original.memory
