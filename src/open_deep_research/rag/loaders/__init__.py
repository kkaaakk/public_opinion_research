"""Public RAG loader API.

This package keeps the old `open_deep_research.rag.loaders` import path while
splitting knowledge, file-memory, MySQL-memory, and query-image helpers into
focused modules.
"""

from open_deep_research.rag.loaders import knowledge as _knowledge
from open_deep_research.rag.loaders import query_images as _query_images
from open_deep_research.rag.loaders.file_memory import (
    fingerprint_memory_paths,
    load_memory_documents_from_paths,
)
from open_deep_research.rag.loaders.knowledge import (
    CODE_EXTENSION_LANGUAGE_MAP,
    DEFAULT_RAG_VISION_CLASSIFICATION_PROMPT,
    DEFAULT_RAG_VISION_MODEL,
    DEFAULT_RAG_VISION_PROMPT,
    DIAGRAM_EDGE_DENSITY,
    DIAGRAM_MAX_COLOR_COUNT,
    LOW_INFO_EDGE_DENSITY,
    LOW_INFO_ENTROPY,
    PHOTO_MIN_COLOR_COUNT,
    PHOTO_MIN_ENTROPY,
    SOME_OCR_TEXT_CHARS,
    SUPPORTED_CODE_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    SUPPORTED_IMAGE_EXTENSIONS,
    SUPPORTED_TEXT_EXTENSIONS,
    TEXT_HEAVY_OCR_CHARS,
    analyze_image_bytes_features,
    analyze_image_features,
    classify_image_bytes_with_vision,
    classify_image_with_vision,
    extract_image_bytes_text,
    extract_image_bytes_vision_text,
    extract_image_text,
    extract_image_vision_text,
    extract_routed_image_bytes_text,
    extract_routed_image_text,
    fingerprint_knowledge_base_paths,
    normalize_image_classification_label,
    quick_ocr_probe_image_bytes_text,
    quick_ocr_probe_image_text,
    route_image_for_rag,
)
from open_deep_research.rag.loaders.mysql_memory import (
    fingerprint_mysql_memory,
    load_memory_documents_from_mysql,
    records_to_documents,
)

_KNOWLEDGE_PATCHABLE_NAMES = (
    "analyze_image_bytes_features",
    "analyze_image_features",
    "classify_image_bytes_with_vision",
    "classify_image_with_vision",
    "extract_image_bytes_text",
    "extract_image_bytes_vision_text",
    "extract_image_text",
    "extract_image_vision_text",
    "extract_routed_image_bytes_text",
    "extract_routed_image_text",
    "quick_ocr_probe_image_bytes_text",
    "quick_ocr_probe_image_text",
    "route_image_for_rag",
)


def _sync_knowledge_patchables() -> None:
    for name in _KNOWLEDGE_PATCHABLE_NAMES:
        setattr(_knowledge, name, globals()[name])


def load_documents_from_paths(*args, **kwargs):
    """Compatibility wrapper that preserves old monkeypatch behavior."""
    _sync_knowledge_patchables()
    return _knowledge.load_documents_from_paths(*args, **kwargs)


def build_query_image_context(*args, **kwargs):
    """Compatibility wrapper for query-time image context loading."""
    _query_images.extract_image_bytes_vision_text = extract_image_bytes_vision_text
    return _query_images.build_query_image_context(*args, **kwargs)

__all__ = [
    "CODE_EXTENSION_LANGUAGE_MAP",
    "DEFAULT_RAG_VISION_CLASSIFICATION_PROMPT",
    "DEFAULT_RAG_VISION_MODEL",
    "DEFAULT_RAG_VISION_PROMPT",
    "DIAGRAM_EDGE_DENSITY",
    "DIAGRAM_MAX_COLOR_COUNT",
    "LOW_INFO_EDGE_DENSITY",
    "LOW_INFO_ENTROPY",
    "PHOTO_MIN_COLOR_COUNT",
    "PHOTO_MIN_ENTROPY",
    "SOME_OCR_TEXT_CHARS",
    "SUPPORTED_CODE_EXTENSIONS",
    "SUPPORTED_EXTENSIONS",
    "SUPPORTED_IMAGE_EXTENSIONS",
    "SUPPORTED_TEXT_EXTENSIONS",
    "TEXT_HEAVY_OCR_CHARS",
    "analyze_image_bytes_features",
    "analyze_image_features",
    "build_query_image_context",
    "classify_image_bytes_with_vision",
    "classify_image_with_vision",
    "extract_image_bytes_text",
    "extract_image_bytes_vision_text",
    "extract_image_text",
    "extract_image_vision_text",
    "extract_routed_image_bytes_text",
    "extract_routed_image_text",
    "fingerprint_knowledge_base_paths",
    "fingerprint_memory_paths",
    "fingerprint_mysql_memory",
    "load_documents_from_paths",
    "load_memory_documents_from_mysql",
    "load_memory_documents_from_paths",
    "normalize_image_classification_label",
    "quick_ocr_probe_image_bytes_text",
    "quick_ocr_probe_image_text",
    "records_to_documents",
    "route_image_for_rag",
]
