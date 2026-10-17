# 保留历史记忆写入导入路径，实际持久化职责位于 memory 包。
"""Compatibility exports for the memory write workflow."""

from open_deep_research.memory.context import (
    get_conversation_id as conversation_id_from_config,
)
from open_deep_research.memory.writer import persist_conversation_memory

__all__ = ["conversation_id_from_config", "persist_conversation_memory"]

