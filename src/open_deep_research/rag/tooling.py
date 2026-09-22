"""Compatibility exports for the RAG agent tool.

New code should import `rag_search` from `open_deep_research.tools.rag_tool`.
"""

from open_deep_research.memory.context import (
    get_conversation_id as _conversation_id_from_config,
)
from open_deep_research.tools.rag_tool import RAG_SEARCH_DESCRIPTION, rag_search

__all__ = ["RAG_SEARCH_DESCRIPTION", "_conversation_id_from_config", "rag_search"]

