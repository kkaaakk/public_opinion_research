# 以只追加调试日志保存原始工具结果及压缩事件，不将日志回注模型上下文。
"""Optional raw transcript sink kept outside model context."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# 维护一个运行的原始材料和压缩事件调试记录。
class ResearchTranscript:
    """Append-only debug transcript for raw tool results and compaction events."""

    # 根据运行标识配置只追加日志路径。
    def __init__(self, directory: str | None, run_id: str) -> None:
        """Configure an append-only transcript path for one research run."""
        self.directory = Path(directory or ".tmp/runtime/transcripts")
        self.run_id = _safe_path_part(run_id)
        self.path = self.directory / f"{self.run_id}.jsonl"

    # 追加一条 JSON 事件，仅用于调试与回溯。
    def append(self, event_type: str, payload: dict[str, Any]) -> None:
        """Append a JSON event without ever returning it to model context."""
        self.directory.mkdir(parents=True, exist_ok=True)
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "run_id": self.run_id,
            **payload,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


# 清理路径片段中的不安全字符，生成合法调试文件名。
def _safe_path_part(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in str(value))
    return safe or "research-run"


__all__ = ["ResearchTranscript"]
