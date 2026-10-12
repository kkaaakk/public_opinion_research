"""Compatibility exports for query-time RAG image helpers."""

from open_deep_research.rag.loaders import query_images as _query_images
from open_deep_research.rag.loaders.knowledge import extract_image_bytes_vision_text


def build_query_image_context(*args, **kwargs):
    """Recognize user query images while preserving legacy monkeypatch hooks."""
    _query_images.extract_image_bytes_vision_text = extract_image_bytes_vision_text
    return _query_images.build_query_image_context(*args, **kwargs)


__all__ = [
    "build_query_image_context",
    "extract_image_bytes_vision_text",
]
