# 保留历史文件记忆加载路径，实际实现位于 loaders/file_memory.py。
"""Compatibility exports for file-backed RAG memory loaders."""

from open_deep_research.rag.loaders.file_memory import (
    fingerprint_memory_paths,
    load_memory_documents_from_paths,
)

__all__ = [
    "fingerprint_memory_paths",
    "load_memory_documents_from_paths",
]
