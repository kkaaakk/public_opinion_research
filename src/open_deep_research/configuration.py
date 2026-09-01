"""Configuration management for the Open Deep Research system."""

import json
import os
import warnings
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field, field_validator, model_validator


class SearchAPI(Enum):
    """Enumeration of available search API providers."""
    
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    TAVILY = "tavily"
    NONE = "none"


class RetrievalMode(Enum):
    """Enumeration of available research retrieval modes."""

    WEB_ONLY = "web_only"
    RAG_ONLY = "rag_only"
    HYBRID = "hybrid"


DEFAULT_RAG_KNOWLEDGE_BASE_PATHS = ["data/knowledge"]
DEFAULT_RAG_MEMORY_PATHS = ["data/memory/chat_memory.jsonl"]
DEFAULT_RAG_VECTORSTORE_PATH = "data/indexes/rag"
DEFAULT_RAG_MILVUS_URI = "data/indexes/rag/milvus.db"
DEFAULT_RAG_GRAPH_ENABLED = True
DEFAULT_RAG_GRAPH_BACKEND = "neo4j"
DEFAULT_RAG_NEO4J_URI = "bolt://localhost:7687"
DEFAULT_RAG_NEO4J_USERNAME = "neo4j"
DEFAULT_RAG_VISION_MODEL = "openai:gpt-4.1-mini"
DEFAULT_RAG_VISION_PROMPT = (
    "Describe only the visible information in this image for RAG indexing. "
    "Include chart meaning, UI structure, diagrams, relationships, objects, and scene context. "
    "Do not invent details; say when something is unclear. Keep it concise and searchable."
)
DEFAULT_PUBLIC_OPINION_AGENTS = [
    "public_signal",
    "internal_knowledge",
    "risk_assessment",
    "response_strategy",
]
PUBLIC_OPINION_AGENT_ALIASES = {
    "news_intelligence": "public_signal",
    "social_sentiment": "public_signal",
    "competitor_impact": "public_signal",
    "fact_verification": "risk_assessment",
    "compliance_risk": "risk_assessment",
    "pr_strategy": "response_strategy",
}
DEFAULT_REPORT_STRUCTURE = """Use this structure to create a report on the user-provided topic:

1. Introduction (no research needed)
   - Brief overview of the topic area

2. Main Body Sections:
   - Each section should focus on a sub-topic of the user-provided topic

3. Conclusion
   - Aim for 1 structural element (either a list or table) that distills the main body sections
   - Provide a concise summary of the report"""
LEGACY_RAG_KNOWLEDGE_BASE_PATH = "examples/rag_data"
LEGACY_RAG_VECTORSTORE_PATH = ".rag_index"
LEGACY_RAG_MILVUS_URI = ".rag_index/milvus.db"

class MCPConfig(BaseModel):
    """Configuration for Model Context Protocol (MCP) servers."""
    
    url: Optional[str] = Field(
        default=None,
        optional=True,
    )
    """The URL of the MCP server"""
    tools: Optional[List[str]] = Field(
        default=None,
        optional=True,
    )
    """The tools to make available to the LLM"""
    auth_required: Optional[bool] = Field(
        default=False,
        optional=True,
    )
    """Whether the MCP server requires authentication"""

class Configuration(BaseModel):
    """Main configuration class for the Deep Research agent."""
    
    # General Configuration
    max_structured_output_retries: int = Field(
        default=3,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 3,
                "min": 1,
                "max": 10,
                "description": "Maximum number of retries for structured output calls from models"
            }
        }
    )
    allow_clarification: bool = Field(
        default=True,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": True,
                "description": "Whether to allow the researcher to ask the user clarifying questions before starting research"
            }
        }
    )
    # Optional Agent Observer sidecar configuration. The integration is
    # deliberately disabled by default and must never affect business output.
    agent_observer_enabled: bool = Field(
        default=False,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": False,
                "description": "Enable optional Agent Observer v0.2 telemetry for this run.",
            }
        },
    )
    agent_observer_endpoint: str = Field(
        default="http://127.0.0.1:8766",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "http://127.0.0.1:8766",
                "description": "Agent Observer HTTP endpoint.",
            }
        },
    )
    agent_observer_project: str = Field(
        default="public-opinion-research",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "public-opinion-research",
                "description": "Agent Observer project name.",
            }
        },
    )
    agent_observer_timeout: float = Field(
        default=0.35,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 0.35,
                "description": "Maximum per-request Observer sidecar timeout in seconds.",
            }
        },
    )
    # Research Configuration
    search_api: SearchAPI = Field(
        default=SearchAPI.TAVILY,
        metadata={
            "x_oap_ui_config": {
                "type": "select",
                "default": "tavily",
                "description": "Search API to use for research. NOTE: Make sure your Researcher Model supports the selected search API.",
                "options": [
                    {"label": "Tavily", "value": SearchAPI.TAVILY.value},
                    {"label": "OpenAI Native Web Search", "value": SearchAPI.OPENAI.value},
                    {"label": "Anthropic Native Web Search", "value": SearchAPI.ANTHROPIC.value},
                    {"label": "None", "value": SearchAPI.NONE.value}
                ]
            }
        }
    )
    max_react_tool_calls: int = Field(
        default=10,
        metadata={
            "x_oap_ui_config": {
                "type": "slider",
                "default": 10,
                "min": 1,
                "max": 30,
                "step": 1,
                "description": "Maximum number of tool calling iterations to make in a single researcher step."
            }
        }
    )
    max_research_rounds: int = Field(
        default=2,
        metadata={
            "x_oap_ui_config": {
                "type": "slider",
                "default": 2,
                "min": 1,
                "max": 10,
                "step": 1,
                "description": (
                    "Maximum public-opinion research rounds, including the initial round. "
                    "This is a workflow safety limit, not a token or call budget."
                ),
            }
        },
    )
    # Business Scenario Configuration
    business_scenario: str = Field(
        default="public_opinion_risk",
        metadata={
            "x_oap_ui_config": {
                "type": "select",
                "default": "public_opinion_risk",
                "description": "Business workflow profile for enterprise public-opinion and brand-risk monitoring.",
                "options": [
                    {"label": "Public opinion risk", "value": "public_opinion_risk"},
                ],
            }
        },
    )
    organization_context: Optional[str] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "textarea",
                "description": "Optional organization, brand, product-line, region, and stakeholder context for business scenario prompts.",
            }
        },
    )
    public_opinion_monitoring_window: str = Field(
        default="last 7 days unless the user specifies another window",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "last 7 days unless the user specifies another window",
                "description": "Default monitoring time window for public opinion risk analysis.",
            }
        },
    )
    enabled_business_agents: Optional[List[str]] = Field(
        default_factory=lambda: list(DEFAULT_PUBLIC_OPINION_AGENTS),
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": DEFAULT_PUBLIC_OPINION_AGENTS,
                "description": "Comma-separated business sub-agent roles available in public_opinion_risk mode.",
            }
        },
    )
    # Budget Guard Configuration
    budget_enabled: bool = Field(
        default=False,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": False,
                "description": "Enable token and call-count budget controls for research runs."
            }
        }
    )
    max_model_calls: Optional[int] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "description": "Maximum model calls allowed for a run. Leave empty for no limit."
            }
        }
    )
    max_tool_calls: Optional[int] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "description": "Maximum executed tool calls allowed for a run. Leave empty for no limit."
            }
        }
    )
    max_search_calls: Optional[int] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "description": "Maximum executed web, native web, or local RAG search calls allowed for a run. Leave empty for no limit."
            }
        }
    )
    max_input_tokens: Optional[int] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "description": "Maximum observed input tokens for a run. Leave empty for no limit."
            }
        }
    )
    max_output_tokens: Optional[int] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "description": "Maximum observed output tokens for a run. Leave empty for no limit."
            }
        }
    )
    budget_warning_ratio: float = Field(
        default=0.8,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 0.8,
                "min": 0.0,
                "max": 1.0,
                "description": "Warn when a configured budget reaches this fraction."
            }
        }
    )
    reserve_final_report_call: bool = Field(
        default=True,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": True,
                "description": "Reserve one model call for final report generation when max_model_calls is configured."
            }
        }
    )
    rag_enabled: bool = Field(
        default=False,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": False,
                "description": "Whether to enable the optional local RAG retrieval pipeline."
            }
        }
    )
    retrieval_mode: RetrievalMode = Field(
        default=RetrievalMode.WEB_ONLY,
        metadata={
            "x_oap_ui_config": {
                "type": "select",
                "default": RetrievalMode.WEB_ONLY.value,
                "description": "Research retrieval mode. Use web_only to preserve the current behavior, rag_only to use only the local knowledge base, or hybrid to combine both.",
                "options": [
                    {"label": "Web only", "value": RetrievalMode.WEB_ONLY.value},
                    {"label": "RAG only", "value": RetrievalMode.RAG_ONLY.value},
                    {"label": "Hybrid", "value": RetrievalMode.HYBRID.value}
                ]
            }
        }
    )
    rag_top_k: int = Field(
        default=4,
        metadata={
            "x_oap_ui_config": {
                "type": "slider",
                "default": 4,
                "min": 1,
                "max": 20,
                "step": 1,
                "description": "Maximum number of local RAG chunks to return for each query."
            }
        }
    )
    rag_chunk_size: int = Field(
        default=1200,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 1200,
                "min": 200,
                "max": 8000,
                "description": "Chunk size for local RAG document splitting."
            }
        }
    )
    rag_chunk_overlap: int = Field(
        default=200,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 200,
                "min": 0,
                "max": 2000,
                "description": "Chunk overlap for local RAG document splitting."
            }
        }
    )
    rag_rerank_top_n: int = Field(
        default=20,
        metadata={
            "x_oap_ui_config": {
                "type": "slider",
                "default": 20,
                "min": 1,
                "max": 50,
                "step": 1,
                "description": "How many initial local retrieval results to rerank before keeping the final top-k."
            }
        }
    )
    rag_embedding_provider: str = Field(
        default="sentence_transformers",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "sentence_transformers",
                "description": "Embedding backend for local RAG. Use sentence_transformers for semantic embeddings."
            }
        }
    )
    rag_embedding_model: str = Field(
        default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                "description": "SentenceTransformers model used for local semantic RAG embeddings."
            }
        }
    )
    rag_embedding_device: Optional[str] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "description": "Optional device for local RAG embeddings, such as cpu or cuda."
            }
        }
    )
    rag_vectorstore_provider: str = Field(
        default="milvus",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "milvus",
                "description": "Vector store backend for local RAG. Use milvus for persistent indexes, chroma/faiss for legacy local indexes, or memory for tests."
            }
        }
    )
    rag_vectorstore_path: str = Field(
        default=DEFAULT_RAG_VECTORSTORE_PATH,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": DEFAULT_RAG_VECTORSTORE_PATH,
                "description": "Directory used for rebuildable local RAG vector indexes."
            }
        }
    )
    rag_collection_name: str = Field(
        default="open_deep_research",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "open_deep_research",
                "description": "Collection name prefix for persistent local RAG vector stores."
            }
        }
    )
    rag_milvus_uri: Optional[str] = Field(
        default=DEFAULT_RAG_MILVUS_URI,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": DEFAULT_RAG_MILVUS_URI,
                "description": "Optional Milvus URI. Use http://host:19530 for a server or a .db path for Milvus Lite. Defaults to data/indexes/rag/milvus.db."
            }
        }
    )
    rag_milvus_token: Optional[str] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "description": "Optional Milvus token for authenticated deployments."
            }
        }
    )
    rag_milvus_db_name: Optional[str] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "description": "Optional Milvus database name."
            }
        }
    )
    rag_milvus_metric_type: str = Field(
        default="COSINE",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "COSINE",
                "description": "Milvus vector metric type, usually COSINE for normalized embeddings."
            }
        }
    )
    rag_reranker_provider: str = Field(
        default="cross_encoder",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "cross_encoder",
                "description": "Reranker backend for local RAG. Use cross_encoder or bge-reranker for semantic reranking."
            }
        }
    )
    rag_reranker_model: str = Field(
        default="BAAI/bge-reranker-base",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "BAAI/bge-reranker-base",
                "description": "Cross-encoder/BGE reranker model for local RAG."
            }
        }
    )
    rag_reranker_device: Optional[str] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "description": "Optional device for local RAG reranking, such as cpu or cuda."
            }
        }
    )
    rag_keyword_top_k: int = Field(
        default=12,
        metadata={
            "x_oap_ui_config": {
                "type": "slider",
                "default": 12,
                "min": 1,
                "max": 50,
                "step": 1,
                "description": "Number of BM25 keyword candidates to fuse with vector retrieval."
            }
        }
    )
    rag_keyword_backend: str = Field(
        default="memory",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "memory",
                "description": "Keyword retrieval backend for local RAG. Use memory for built-in BM25 or elasticsearch for Elasticsearch BM25."
            }
        }
    )
    rag_elasticsearch_url: str = Field(
        default="http://localhost:9200",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "http://localhost:9200",
                "description": "Elasticsearch URL used when rag_keyword_backend is elasticsearch."
            }
        }
    )
    rag_elasticsearch_index: str = Field(
        default="rag_chunks",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "rag_chunks",
                "description": "Elasticsearch index used for RAG keyword/BM25 chunk retrieval."
            }
        }
    )
    rag_rrf_rank_constant: int = Field(
        default=60,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 60,
                "min": 1,
                "max": 200,
                "description": "RRF rank constant used to fuse dense vector and BM25 retrieval results."
            }
        }
    )
    rag_structured_metadata_weight: float = Field(
        default=0.15,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 0.15,
                "min": 0.0,
                "max": 1.0,
                "description": (
                    "Small retrieval score boost when a query matches chunk "
                    "metadata such as headings, json_path, file type, source, "
                    "or memory type."
                )
            }
        }
    )
    rag_graph_enabled: bool = Field(
        default=DEFAULT_RAG_GRAPH_ENABLED,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": DEFAULT_RAG_GRAPH_ENABLED,
                "description": "Enable lightweight GraphRAG expansion over local chunks after vector+BM25 recall."
            }
        }
    )
    rag_graph_backend: str = Field(
        default=DEFAULT_RAG_GRAPH_BACKEND,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": DEFAULT_RAG_GRAPH_BACKEND,
                "description": "GraphRAG backend: memory for in-process graph expansion or neo4j for a Neo4j knowledge graph."
            }
        }
    )
    rag_graph_max_neighbors: int = Field(
        default=4,
        metadata={
            "x_oap_ui_config": {
                "type": "slider",
                "default": 4,
                "min": 0,
                "max": 20,
                "step": 1,
                "description": "Maximum graph-neighbor chunks to add during GraphRAG expansion."
            }
        }
    )
    rag_graph_weight: float = Field(
        default=0.35,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 0.35,
                "min": 0.0,
                "max": 1.0,
                "description": "GraphRAG score weight for evidence connected to seed chunks."
            }
        }
    )
    rag_graph_ner_enabled: bool = Field(
        default=True,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": True,
                "description": "Enable spaCy NER for graph term extraction. Requires spaCy and en_core_web_sm."
            }
        }
    )
    rag_graph_idf_enabled: bool = Field(
        default=True,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": True,
                "description": "Enable IDF-weighted filtering of common terms during graph term extraction."
            }
        }
    )
    rag_graph_idf_threshold_percentile: float = Field(
        default=85.0,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 85.0,
                "min": 0.0,
                "max": 100.0,
                "description": "IDF percentile threshold. Terms below this percentile are filtered out."
            }
        }
    )
    rag_graph_confidence_threshold: float = Field(
        default=0.15,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 0.15,
                "min": 0.0,
                "max": 1.0,
                "description": "Minimum confidence for graph expansion candidates. Candidates below this threshold are filtered out."
            }
        }
    )
    rag_structural_edges_enabled: bool = Field(
        default=False,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": False,
                "description": "Enable structural edges (same source, adjacent chunks, shared metadata) in GraphRAG expansion."
            }
        }
    )
    rag_neo4j_uri: Optional[str] = Field(
        default=DEFAULT_RAG_NEO4J_URI,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": DEFAULT_RAG_NEO4J_URI,
                "description": "Neo4j bolt URI for graph_backend=neo4j, for example bolt://localhost:7687."
            }
        }
    )
    rag_neo4j_username: Optional[str] = Field(
        default=DEFAULT_RAG_NEO4J_USERNAME,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": DEFAULT_RAG_NEO4J_USERNAME,
                "description": "Neo4j username for graph_backend=neo4j."
            }
        }
    )
    rag_neo4j_password: Optional[str] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "description": "Neo4j password for graph_backend=neo4j."
            }
        }
    )
    rag_neo4j_database: Optional[str] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "description": "Optional Neo4j database name for graph_backend=neo4j."
            }
        }
    )
    rag_authority_rerank_enabled: bool = Field(
        default=True,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": True,
                "description": "Downrank deprecated, misleading, or unanswerable-trap local RAG chunks after semantic reranking."
            }
        }
    )
    rag_knowledge_base_paths: Optional[List[str]] = Field(
        default_factory=lambda: list(DEFAULT_RAG_KNOWLEDGE_BASE_PATHS),
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": DEFAULT_RAG_KNOWLEDGE_BASE_PATHS,
                "description": "Comma-separated paths or a JSON array of local files/directories to index for RAG."
            }
        }
    )
    rag_multimodal_enabled: bool = Field(
        default=True,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": True,
                "description": "Whether to route image files and image-only PDF pages through local OCR and optional Vision extraction for RAG."
            }
        }
    )
    rag_multimodal_provider: str = Field(
        default="ocr",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "ocr",
                "description": "Multimodal extraction provider for local RAG. Use ocr/local_ocr/tesseract, or disabled."
            }
        }
    )
    rag_ocr_languages: str = Field(
        default="eng+chi_sim",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "eng+chi_sim",
                "description": "Tesseract OCR languages for multimodal RAG image and scanned-PDF extraction."
            }
        }
    )
    rag_vision_enabled: bool = Field(
        default=True,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": True,
                "description": "Whether image routing can use a Vision LLM for diagrams, UI, photos, and uncertain image classification."
            }
        }
    )
    rag_vision_model: str = Field(
        default=DEFAULT_RAG_VISION_MODEL,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": DEFAULT_RAG_VISION_MODEL,
                "description": "Vision-capable chat model used for RAG image understanding."
            }
        }
    )
    rag_vision_prompt: str = Field(
        default=DEFAULT_RAG_VISION_PROMPT,
        metadata={
            "x_oap_ui_config": {
                "type": "textarea",
                "default": DEFAULT_RAG_VISION_PROMPT,
                "description": "Prompt used to turn image content into concise searchable RAG text."
            }
        }
    )
    rag_vision_max_tokens: int = Field(
        default=512,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 512,
                "description": "Maximum output tokens for Vision LLM image descriptions and classification."
            }
        }
    )
    rag_query_image_enabled: bool = Field(
        default=True,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": True,
                "description": "Whether images attached to the user's question are recognized as temporary query context."
            }
        }
    )
    rag_query_image_max_images: int = Field(
        default=3,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 3,
                "description": "Maximum number of user-question images to recognize per request."
            }
        }
    )
    rag_query_image_max_bytes: int = Field(
        default=5_000_000,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 5_000_000,
                "description": "Maximum size in bytes for each user-question image recognized by RAG."
            }
        }
    )
    rag_query_rewrite_enabled: bool = Field(
        default=True,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": True,
                "description": "Whether to rewrite RAG tool queries into standalone retrieval queries before vector/BM25 search."
            }
        }
    )
    rag_query_rewrite_model: str = Field(
        default="deepseek:deepseek-chat",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "openai:gpt-4.1-mini",
                "description": "Model used to rewrite RAG tool queries before retrieval."
            }
        }
    )
    rag_query_rewrite_max_tokens: int = Field(
        default=256,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 256,
                "description": "Maximum output tokens for RAG query rewriting."
            }
        }
    )
    rag_memory_enabled: bool = Field(
        default=False,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": False,
                "description": "Whether to include chat memory records as an additional local RAG source."
            }
        }
    )
    rag_memory_paths: Optional[List[str]] = Field(
        default_factory=lambda: list(DEFAULT_RAG_MEMORY_PATHS),
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": DEFAULT_RAG_MEMORY_PATHS,
                "description": "Comma-separated paths or a JSON array of .json/.jsonl chat memory files or directories to index for RAG."
            }
        }
    )
    rag_memory_json_text_fields: Optional[List[str]] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "description": "Optional comma-separated JSON field paths to extract from chat memory records. Leave empty to use common memory fields and messages."
            }
        }
    )
    rag_memory_mysql_url: Optional[str] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "description": "MySQL SQLAlchemy URL for storing raw chat memory, for example mysql+pymysql://user:pass@host:3306/db."
            }
        }
    )
    rag_memory_mysql_table: str = Field(
        default="rag_chat_memories",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "rag_chat_memories",
                "description": "MySQL table used to store raw chat, summary, and long-term memory records."
            }
        }
    )
    rag_memory_mysql_limit: int = Field(
        default=1000,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 1000,
                "min": 1,
                "max": 10000,
                "description": "Maximum number of MySQL chat memory rows to load into the RAG index."
            }
        }
    )
    rag_memory_mysql_index_record_types: Optional[List[str]] = Field(
        default=[
            "summary",
            "preference",
            "project_fact",
            "decision",
            "constraint",
            "deprecated",
        ],
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "description": "Comma-separated MySQL memory record types to vectorize for RAG. Full chat transcripts stay only in MySQL as chat_raw."
            }
        }
    )
    rag_memory_write_enabled: bool = Field(
        default=False,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": False,
                "description": "Whether to persist completed conversations into MySQL chat memory."
            }
        }
    )
    rag_memory_write_sync_index: bool = Field(
        default=True,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": True,
                "description": "Trigger a non-blocking RAG index refresh after writing chat memory to MySQL. Query-time indexing still acts as a fallback."
            }
        }
    )
    rag_json_text_fields: Optional[List[str]] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "description": "Optional comma-separated JSON field paths to extract for RAG ingestion. Leave empty to recursively collect string fields."
            }
        }
    )
    rag_hash_embedding_dimensions: int = Field(
        default=256,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 256,
                "min": 32,
                "max": 2048,
                "description": "Embedding vector width for the built-in hash embedding backend."
            }
        }
    )
    # Model Configuration
    summarization_model: str = Field(
        default="deepseek:deepseek-chat",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "openai:gpt-4.1-mini",
                "description": "Model for summarizing research results from Tavily search results"
            }
        }
    )
    summarization_model_max_tokens: int = Field(
        default=8192,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 8192,
                "description": "Maximum output tokens for summarization model"
            }
        }
    )
    max_content_length: int = Field(
        default=50000,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 50000,
                "min": 1000,
                "max": 200000,
                "description": "Maximum character length for webpage content before summarization"
            }
        }
    )
    research_model: str = Field(
        default="deepseek:deepseek-chat",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "openai:gpt-4.1",
                "description": "Model for conducting research. NOTE: Make sure your Researcher Model supports the selected search API."
            }
        }
    )
    research_model_max_tokens: int = Field(
        default=10000,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 10000,
                "description": "Maximum output tokens for research model"
            }
        }
    )
    compression_model: str = Field(
        default="deepseek:deepseek-chat",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "openai:gpt-4.1",
                "description": "Model for compressing research findings from sub-agents. NOTE: Make sure your Compression Model supports the selected search API."
            }
        }
    )
    compression_model_max_tokens: int = Field(
        default=8192,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 8192,
                "description": "Maximum output tokens for compression model"
            }
        }
    )
    final_report_model: str = Field(
        default="deepseek:deepseek-chat",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "openai:gpt-4.1",
                "description": "Model for writing the final report from all research findings"
            }
        }
    )
    final_report_model_max_tokens: int = Field(
        default=10000,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 10000,
                "description": "Maximum output tokens for final report model"
            }
        }
    )
    # MCP server configuration
    mcp_config: Optional[MCPConfig] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "mcp",
                "description": "MCP server configuration"
            }
        }
    )
    mcp_prompt: Optional[str] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "description": "Any additional instructions to pass along to the Agent regarding the MCP tools that are available to it."
            }
        }
    )
    # ------------------------------------------------------------------
    # Bytebase DBHub MCP — database query tools
    # ------------------------------------------------------------------

    dbhub_enabled: bool = Field(
        default=False,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": False,
                "description": "Enable Bytebase DBHub MCP server for database queries (execute_sql, search_objects). Requires Node.js >= 22.5.0.",
            }
        },
    )
    dbhub_dsn: Optional[str] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "description": "Database DSN for DBHub, e.g. mysql://user:pass@host:3306/db or postgres://user:pass@host:5432/db. Password is masked in logs.",
            }
        },
    )
    dbhub_transport: str = Field(
        default="stdio",
        metadata={
            "x_oap_ui_config": {
                "type": "select",
                "default": "stdio",
                "description": "DBHub transport mode. Use 'stdio' to auto-start DBHub via npx, or 'http' to connect to an already-running DBHub instance.",
                "options": [
                    {"label": "stdio (auto-start)", "value": "stdio"},
                    {"label": "HTTP (external server)", "value": "http"},
                ],
            }
        },
    )
    dbhub_http_port: int = Field(
        default=8080,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "default": 8080,
                "min": 1024,
                "max": 65535,
                "description": "Port for DBHub HTTP mode (only used when dbhub_transport is 'http').",
            }
        },
    )

    # ------------------------------------------------------------------
    # Microsoft MarkItDown MCP — file-to-markdown conversion
    # ------------------------------------------------------------------

    markitdown_enabled: bool = Field(
        default=False,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": False,
                "description": "Enable Microsoft official MarkItDown MCP server (markitdown-mcp) to convert files to Markdown via convert_to_markdown tool.",
            }
        },
    )

    # ------------------------------------------------------------------
    # Feishu / Lark Official MCP — Feishu API tools
    # ------------------------------------------------------------------

    feishu_enabled: bool = Field(
        default=False,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": False,
                "description": "Enable Feishu/Lark official MCP server (@larksuiteoapi/lark-mcp) for Feishu/Lark API operations.",
            }
        },
    )
    feishu_app_id: Optional[str] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "description": "Feishu/Lark App ID (from open.feishu.cn or open.larksuite.com developer console).",
            }
        },
    )
    feishu_app_secret: Optional[str] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "description": "Feishu/Lark App Secret. Keep this value secret.",
            }
        },
    )
    feishu_domain: str = Field(
        default="https://open.feishu.cn",
        metadata={
            "x_oap_ui_config": {
                "type": "select",
                "default": "https://open.feishu.cn",
                "description": "Feishu/Lark API domain. Use https://open.feishu.cn for China, https://open.larksuite.com for international.",
                "options": [
                    {"label": "Feishu (China)", "value": "https://open.feishu.cn"},
                    {"label": "Lark (International)", "value": "https://open.larksuite.com"},
                ],
            }
        },
    )
    feishu_mcp_preset: str = Field(
        default="preset.light",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "preset.light",
                "description": "Feishu MCP tool preset (whitelist). Use 'preset.light' (~10 tools), 'preset.default' (~20+ tools), or a comma-separated list of explicit API paths like 'im.v1.message.create,docx.v1.document.rawContent'.",
            }
        },
    )
    feishu_oauth_enabled: bool = Field(
        default=False,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": False,
                "description": "Enable OAuth user-access-token mode for Feishu MCP (required to access user-private resources like personal docs and IMs). Requires running 'npx @larksuiteoapi/lark-mcp login' first.",
            }
        },
    )

    # ------------------------------------------------------------------
    # Plan-and-Execute Configuration (public-opinion mode)
    # ------------------------------------------------------------------

    allow_plan_feedback: bool = Field(
        default=False,
        metadata={
            "x_oap_ui_config": {
                "type": "boolean",
                "default": False,
                "description": "Whether to pause after planning report sections to allow human feedback on the plan (public-opinion mode only).",
            }
        },
    )
    report_structure: str = Field(
        default=DEFAULT_REPORT_STRUCTURE,
        metadata={
            "x_oap_ui_config": {
                "type": "textarea",
                "default": DEFAULT_REPORT_STRUCTURE,
                "description": "Report structure guideline passed to the section planner.",
            }
        },
    )
    planner_model: str = Field(
        default="",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "",
                "description": "Model used to plan report sections. Leave empty to fall back to research_model.",
            }
        },
    )
    planner_model_max_tokens: Optional[int] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "description": "Maximum output tokens for the planner model. Leave empty for provider default.",
            }
        },
    )
    section_writer_model: str = Field(
        default="",
        metadata={
            "x_oap_ui_config": {
                "type": "text",
                "default": "",
                "description": "Model used to write report sections from role evidence. Leave empty to fall back to final_report_model.",
            }
        },
    )
    section_writer_model_max_tokens: Optional[int] = Field(
        default=None,
        optional=True,
        metadata={
            "x_oap_ui_config": {
                "type": "number",
                "description": "Maximum output tokens for the section writer model. Leave empty for provider default.",
            }
        },
    )

    @field_validator(
        "rag_knowledge_base_paths",
        "rag_json_text_fields",
        "rag_memory_paths",
        "rag_memory_json_text_fields",
        "rag_memory_mysql_index_record_types",
        "enabled_business_agents",
        mode="before",
    )
    @classmethod
    def normalize_list_settings(cls, value: Any) -> Optional[list[str]]:
        """Normalize list-like configuration provided through env vars or runnable config."""
        if value in (None, ""):
            return None
        if isinstance(value, list):
            normalized_items = [str(item).strip() for item in value if str(item).strip()]
            return normalized_items or None
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    normalized_items = [str(item).strip() for item in parsed if str(item).strip()]
                    return normalized_items or None
            normalized_items = [item.strip() for item in stripped.split(",") if item.strip()]
            return normalized_items or None
        return value

    @model_validator(mode="after")
    def validate_rag_chunk_settings(self) -> "Configuration":
        """Validate local RAG chunking and budget settings."""
        _apply_rag_path_compatibility(self)
        if self.max_research_rounds <= 0:
            raise ValueError("max_research_rounds must be greater than 0.")
        if self.rag_chunk_size <= 0:
            raise ValueError("rag_chunk_size must be greater than 0.")
        if self.rag_chunk_overlap < 0:
            raise ValueError("rag_chunk_overlap must be greater than or equal to 0.")
        if self.rag_chunk_overlap >= self.rag_chunk_size:
            raise ValueError("rag_chunk_overlap must be smaller than rag_chunk_size.")
        if self.rag_keyword_top_k <= 0:
            raise ValueError("rag_keyword_top_k must be greater than 0.")
        if self.rag_keyword_backend.strip().lower() not in {"memory", "elasticsearch"}:
            raise ValueError("rag_keyword_backend must be either memory or elasticsearch.")
        if not self.rag_elasticsearch_index.strip():
            raise ValueError("rag_elasticsearch_index must not be empty.")
        if self.rag_memory_mysql_limit <= 0:
            raise ValueError("rag_memory_mysql_limit must be greater than 0.")
        if self.rag_memory_mysql_index_record_types:
            allowed_memory_record_types = {
                "chat",
                "chat_raw",
                "summary",
                "memory",
                "preference",
                "project_fact",
                "decision",
                "constraint",
                "deprecated",
            }
            invalid_memory_record_types = [
                record_type
                for record_type in self.rag_memory_mysql_index_record_types
                if str(record_type).strip().lower() not in allowed_memory_record_types
            ]
            if invalid_memory_record_types:
                raise ValueError(
                    "rag_memory_mysql_index_record_types contains an unsupported memory type."
                )
        if self.rag_rrf_rank_constant <= 0:
            raise ValueError("rag_rrf_rank_constant must be greater than 0.")
        if not 0 <= self.rag_structured_metadata_weight <= 1:
            raise ValueError("rag_structured_metadata_weight must be between 0 and 1.")
        if self.rag_query_rewrite_max_tokens <= 0:
            raise ValueError("rag_query_rewrite_max_tokens must be greater than 0.")
        if self.rag_graph_backend.strip().lower() not in {"memory", "neo4j"}:
            raise ValueError("rag_graph_backend must be either memory or neo4j.")
        if self.rag_graph_max_neighbors < 0:
            raise ValueError("rag_graph_max_neighbors must be greater than or equal to 0.")
        if not 0 <= self.rag_graph_weight <= 1:
            raise ValueError("rag_graph_weight must be between 0 and 1.")
        if not 0.0 <= self.rag_graph_idf_threshold_percentile <= 100.0:
            raise ValueError("rag_graph_idf_threshold_percentile must be between 0 and 100.")
        if not 0.0 <= self.rag_graph_confidence_threshold <= 1.0:
            raise ValueError("rag_graph_confidence_threshold must be between 0 and 1.")
        if self.rag_milvus_metric_type.strip().upper() not in {"COSINE", "IP", "L2"}:
            raise ValueError("rag_milvus_metric_type must be COSINE, IP, or L2.")
        if self.rag_multimodal_provider.strip().lower() not in {
            "ocr",
            "tesseract",
            "local_ocr",
            "none",
            "disabled",
        }:
            raise ValueError(
                "rag_multimodal_provider must be ocr, tesseract, local_ocr, none, or disabled."
            )
        if self.rag_vision_enabled:
            if not self.rag_vision_model.strip():
                raise ValueError("rag_vision_model must not be empty when vision is enabled.")
            if not self.rag_vision_prompt.strip():
                raise ValueError("rag_vision_prompt must not be empty when vision is enabled.")
            if self.rag_vision_max_tokens <= 0:
                raise ValueError("rag_vision_max_tokens must be greater than 0.")
        if self.rag_query_image_max_images < 0:
            raise ValueError("rag_query_image_max_images must be greater than or equal to 0.")
        if self.rag_query_image_max_bytes <= 0:
            raise ValueError("rag_query_image_max_bytes must be greater than 0.")
        budget_fields = (
            "max_model_calls",
            "max_tool_calls",
            "max_search_calls",
            "max_input_tokens",
            "max_output_tokens",
        )
        for field_name in budget_fields:
            field_value = getattr(self, field_name)
            if field_value is not None and field_value < 0:
                raise ValueError(f"{field_name} must be greater than or equal to 0.")
        if not 0 <= self.budget_warning_ratio <= 1:
            raise ValueError("budget_warning_ratio must be between 0 and 1.")
        supported_business_scenarios = {"public_opinion_risk"}
        if self.business_scenario.strip().lower() not in supported_business_scenarios:
            raise ValueError(
                "business_scenario must be public_opinion_risk."
            )
        allowed_business_agents = {
            *DEFAULT_PUBLIC_OPINION_AGENTS,
            *PUBLIC_OPINION_AGENT_ALIASES,
        }
        if self.enabled_business_agents:
            invalid_business_agents = [
                agent
                for agent in self.enabled_business_agents
                if str(agent).strip().lower() not in allowed_business_agents
            ]
            if invalid_business_agents:
                raise ValueError(
                    "enabled_business_agents contains an unsupported business agent role."
                )
            normalized_agents = []
            for agent in self.enabled_business_agents:
                normalized_agent = PUBLIC_OPINION_AGENT_ALIASES.get(
                    str(agent).strip().lower(),
                    str(agent).strip().lower(),
                )
                if normalized_agent not in normalized_agents:
                    normalized_agents.append(normalized_agent)
            self.enabled_business_agents = normalized_agents or list(DEFAULT_PUBLIC_OPINION_AGENTS)
        return self


    @classmethod
    def from_runnable_config(
        cls, config: Optional[RunnableConfig] = None
    ) -> "Configuration":
        """Create a Configuration instance from a RunnableConfig."""
        configurable = config.get("configurable", {}) if config else {}
        field_names = list(cls.model_fields.keys())
        values: dict[str, Any] = {
            field_name: os.environ.get(field_name.upper(), configurable.get(field_name))
            for field_name in field_names
        }
        return cls(**{k: v for k, v in values.items() if v is not None})

    class Config:
        """Pydantic configuration."""
        
        arbitrary_types_allowed = True


def _apply_rag_path_compatibility(config: Configuration) -> None:
    """Keep old RAG paths usable while nudging callers toward canonical data dirs."""
    fields_set = config.model_fields_set
    if (
        "rag_knowledge_base_paths" not in fields_set
        and config.rag_knowledge_base_paths == DEFAULT_RAG_KNOWLEDGE_BASE_PATHS
        and not _any_local_path_exists(DEFAULT_RAG_KNOWLEDGE_BASE_PATHS)
        and _local_path_exists(LEGACY_RAG_KNOWLEDGE_BASE_PATH)
    ):
        _warn_legacy_path(
            setting="rag_knowledge_base_paths",
            value=LEGACY_RAG_KNOWLEDGE_BASE_PATH,
            replacement=DEFAULT_RAG_KNOWLEDGE_BASE_PATHS[0],
        )
        config.rag_knowledge_base_paths = [LEGACY_RAG_KNOWLEDGE_BASE_PATH]

    for knowledge_path in config.rag_knowledge_base_paths or []:
        if _is_legacy_path(knowledge_path, LEGACY_RAG_KNOWLEDGE_BASE_PATH):
            _warn_legacy_path(
                setting="rag_knowledge_base_paths",
                value=knowledge_path,
                replacement=DEFAULT_RAG_KNOWLEDGE_BASE_PATHS[0],
            )

    if (
        "rag_milvus_uri" not in fields_set
        and "rag_vectorstore_path" in fields_set
    ):
        config.rag_milvus_uri = _milvus_uri_from_vectorstore_path(
            config.rag_vectorstore_path
        )

    if _is_legacy_path(config.rag_vectorstore_path, LEGACY_RAG_VECTORSTORE_PATH):
        _warn_legacy_path(
            setting="rag_vectorstore_path",
            value=config.rag_vectorstore_path,
            replacement=DEFAULT_RAG_VECTORSTORE_PATH,
        )

    if config.rag_milvus_uri and _is_legacy_path(
        config.rag_milvus_uri,
        LEGACY_RAG_MILVUS_URI,
    ):
        _warn_legacy_path(
            setting="rag_milvus_uri",
            value=config.rag_milvus_uri,
            replacement=DEFAULT_RAG_MILVUS_URI,
        )


def _any_local_path_exists(paths: list[str]) -> bool:
    return any(_local_path_exists(path) for path in paths)


def _local_path_exists(value: str) -> bool:
    if "://" in str(value):
        return False
    return Path(str(value)).expanduser().exists()


def _is_legacy_path(value: str, legacy_value: str) -> bool:
    normalized = _normalize_local_path(value)
    legacy = _normalize_local_path(legacy_value)
    return normalized == legacy or normalized.endswith(f"/{legacy}")


def _normalize_local_path(value: str) -> str:
    normalized = str(value).replace("\\", "/").strip().rstrip("/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _milvus_uri_from_vectorstore_path(vectorstore_path: str) -> str:
    stripped = str(vectorstore_path).strip()
    if "://" in stripped or stripped.endswith(".db"):
        return stripped
    return str(Path(stripped).expanduser() / "milvus.db").replace("\\", "/")


def _warn_legacy_path(setting: str, value: str, replacement: str) -> None:
    if not _local_path_exists(value):
        return
    warnings.warn(
        (
            f"{setting} uses legacy RAG path '{value}'. "
            f"Prefer '{replacement}'. Existing legacy paths remain supported."
        ),
        UserWarning,
        stacklevel=3,
    )
