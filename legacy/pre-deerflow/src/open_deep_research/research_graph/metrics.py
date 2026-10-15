# 维护研究图写入、检索、抽取和上下文管理的指标及数据质量标记。
"""Local Research Graph metrics with exact/estimated quality markers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

METRIC_NAMES = (
    "graph_extraction_calls",
    "graph_extraction_input_tokens",
    "graph_extraction_output_tokens",
    "graph_nodes_written",
    "graph_edges_written",
    "graph_write_latency",
    "graph_retrieval_calls",
    "graph_retrieval_latency",
    "retrieved_nodes",
    "retrieved_edges",
    "context_manager_calls",
    "context_manager_input_tokens",
    "context_manager_output_tokens",
    "working_context_tokens",
    "micro_compact_count",
    "micro_compact_tokens_removed",
    "rolling_compact_count",
    "rolling_compact_input_tokens",
    "rolling_compact_output_tokens",
    "raw_tool_tokens_before_compact",
    "raw_tool_tokens_after_compact",
    "cache_hit_url",
    "cache_miss_url",
    "duplicate_source_skipped",
)


# 为单个角色累积可序列化的研究图指标。
@dataclass
class ResearchGraphMetrics:
    """Mutable per-agent metrics accumulator."""

    values: dict[str, int | float] = field(
        default_factory=lambda: {name: 0 for name in METRIC_NAMES}
    )
    quality: dict[str, str] = field(default_factory=dict)

    # 累计数值观察量，并可附加该数值的质量说明。
    def add(self, name: str, value: int | float = 1, *, quality: str | None = None) -> None:
        """Add a numeric observation and optionally record its quality."""
        if name not in self.values:
            self.values[name] = 0
        self.values[name] += value
        if quality:
            self.quality[name] = quality

    # 设置指标当前值，并更新可选质量标记。
    def set(self, name: str, value: int | float, *, quality: str | None = None) -> None:
        """Set a metric value and optionally record its quality."""
        self.values[name] = value
        if quality:
            self.quality[name] = quality

    # 把另一份指标载荷中的数值累计到当前实例。
    def merge(self, other: dict[str, Any] | None) -> None:
        """Merge numeric metrics from another accumulator payload."""
        if not isinstance(other, dict):
            return
        for name, value in other.items():
            if name == "quality":
                if isinstance(value, dict):
                    self.quality.update({str(k): str(v) for k, v in value.items()})
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.add(str(name), value)

    # 导出可序列化指标及质量字段。
    def as_dict(self) -> dict[str, Any]:
        """Return serializable metric values and quality markers."""
        return {**self.values, "quality": dict(self.quality)}


# 作为 reducer 合并并发角色输出的可加指标。
def merge_research_graph_metrics(
    current_value: Any,
    new_value: Any,
) -> dict[str, Any]:
    """Reducer for additive metrics emitted by parallel role agents."""
    result = ResearchGraphMetrics()
    result.merge(current_value if isinstance(current_value, dict) else {})
    result.merge(new_value if isinstance(new_value, dict) else {})
    if isinstance(current_value, dict) and isinstance(current_value.get("quality"), dict):
        result.quality.update({str(k): str(v) for k, v in current_value["quality"].items()})
    if isinstance(new_value, dict) and isinstance(new_value.get("quality"), dict):
        result.quality.update({str(k): str(v) for k, v in new_value["quality"].items()})
    return result.as_dict()


__all__ = ["METRIC_NAMES", "ResearchGraphMetrics", "merge_research_graph_metrics"]
