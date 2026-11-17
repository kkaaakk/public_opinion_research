# 由业务标识内容生成稳定、可重建的短 ID。
"""Stable identifiers derived from business facts."""

from __future__ import annotations

import hashlib
from typing import Any


# 拼接规范化后的业务事实并取前缀哈希，保证同一事实得到同一 ID。
def stable_id(prefix: str, *parts: Any, length: int = 20) -> str:
    """Return a deterministic prefixed identifier for the supplied facts."""
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


__all__ = ["stable_id"]
