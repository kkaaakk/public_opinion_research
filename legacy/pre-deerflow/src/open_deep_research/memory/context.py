# 统一解析记忆读写使用的会话与用户范围，避免写入和检索身份不一致。
"""Runtime identity helpers for memory.

All memory writers, readers, and RAG tools should call this module so the same
conversation is used when writing MySQL rows and when retrieving indexed memory.
"""

from typing import Any, Mapping, Optional

from langchain_core.runnables import RunnableConfig


# 按配置及元数据中的会话、线程标识解析记忆范围，最后回退到用户或默认值。
def get_conversation_id(config: RunnableConfig | Mapping[str, Any] | None) -> str:
    """Return the stable conversation/thread id used to scope memory."""
    configurable, metadata = _config_parts(config)
    return (
        _first_text(
            configurable.get("conversation_id"),
            configurable.get("thread_id"),
            metadata.get("conversation_id"),
            metadata.get("thread_id"),
        )
        or get_user_id(config)
        or "default"
    )


# 解析用户标识，并兼容历史 owner 元数据。
def get_user_id(config: RunnableConfig | Mapping[str, Any] | None) -> Optional[str]:
    """Return the user id when the runtime config exposes one.

    Older flows used `metadata.owner` as the closest user-level scope, so it is
    kept as a compatibility fallback.
    """
    configurable, metadata = _config_parts(config)
    return _first_text(
        configurable.get("user_id"),
        metadata.get("user_id"),
        metadata.get("owner"),
        configurable.get("owner"),
    )


# 安全取出运行配置中的 configurable 和 metadata 两个映射。
def _config_parts(
    config: RunnableConfig | Mapping[str, Any] | None,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    config = config or {}
    return (
        config.get("configurable", {}) or {},
        config.get("metadata", {}) or {},
    )


# 按优先顺序返回首个去空白后非空的文本值。
def _first_text(*values: Any) -> Optional[str]:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None

