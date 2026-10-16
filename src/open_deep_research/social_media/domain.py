# 复用负面词汇、投诉信号、互动热度和风险评分的领域规则。
"""Shared, behavior-neutral social-media domain primitives."""

from __future__ import annotations

from typing import Any, Mapping

DEFAULT_RISK_RULES = {
    "critical": {"min_negative_ratio": 0.6, "min_negative_engagement": 10000, "min_complaints": 10},
    "high": {"min_negative_ratio": 0.45, "min_negative_engagement": 3000, "min_complaints": 3},
    "medium": {"min_negative_ratio": 0.25, "min_negative_engagement": 800, "min_complaints": 1},
}
NEGATIVE_TERMS = [
    "complaint", "refund", "after-sales", "after_sales", "quality issue", "delay",
    "safety issue", "abnormal", "scam", "fraud", "boycott", "投诉", "维权", "退款",
    "退货", "质量", "售后", "客服", "安全", "异常", "故障", "失控", "自燃", "刹车",
    "漏水", "虚假宣传", "欺诈", "骗", "差评", "避雷", "避坑", "踩坑", "翻车", "背刺",
    "割韭菜", "贬值", "续航虚标", "车机卡顿", "刹不住", "曝光", "召回", "赔偿", "违法",
    "处罚", "数据泄露", "隐私",
]
COMPLAINT_TERMS = [
    "complaint", "blackcat", "refund", "after-sales", "after_sales", "quality", "黑猫",
    "投诉", "维权", "退款", "退货", "质量", "售后", "客服", "赔偿",
]
POSITIVE_TERMS = ["good", "great", "resolved", "recommend", "满意", "解决", "推荐", "好评", "靠谱"]
NEGATIVE_QUERY_TERMS = ["投诉", "质量", "售后", "退款", "维权", "避雷", "避坑"]


# 判断小写文本是否包含任一配置词语。
def contains_any(text: str, terms: list[str]) -> bool:
    """Return whether lower-cased ``text`` contains any configured term."""
    return any(term.lower() in text for term in terms)


# 提取命中的负面及投诉词，去重后稳定排序。
def matched_signal_terms(text: str) -> list[str]:
    """Return the stable sorted set of negative/complaint terms in ``text``."""
    return sorted(
        {term for term in [*NEGATIVE_TERMS, *COMPLAINT_TERMS] if term.lower() in text}
    )


# 用共享负面词汇扩展查询，增加投诉和风险信号召回。
def negative_query_variants(query: Any, variant_count: int) -> list[str]:
    """Expand a query with the shared negative-signal vocabulary."""
    base_query = str(query or "").strip()
    if not base_query:
        return []
    variants = [base_query]
    base_lower = base_query.lower()
    for term in NEGATIVE_QUERY_TERMS:
        if term.lower() in base_lower:
            continue
        variants.append(f"{base_query} {term}")
        if len(variants) >= variant_count:
            break
    return variants


# 根据共享领域规则计算风险分数，避免适配器使用不同口径。
def risk_score(
    negative_ratio: float,
    complaint_count: int,
    negative_engagement: int,
    total_engagement: int,
) -> int:
    """Calculate the shared risk score without selecting adapter-specific rules."""
    has_negative_signal = bool(negative_ratio or complaint_count or negative_engagement)
    heat_component = (
        min(total_engagement / 2000, 10)
        if has_negative_signal
        else min(total_engagement / 5000, 5)
    )
    return min(
        100,
        round(
            negative_ratio * 55
            + min(complaint_count * 8, 25)
            + min(negative_engagement / 1000, 15)
            + heat_component
        ),
    )


# 按互动总量映射为现有阈值定义的热度等级。
def heat_level(total_engagement: int) -> str:
    """Map engagement volume to the existing heat-level thresholds."""
    if total_engagement >= 100000:
        return "critical"
    if total_engagement >= 30000:
        return "high"
    if total_engagement >= 3000:
        return "medium"
    return "low"


# 计算比例并保留四位小数，统一统计输出格式。
def ratio(numerator: int, denominator: int) -> float:
    """Return the existing four-decimal ratio representation."""
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


__all__ = [
    "COMPLAINT_TERMS", "DEFAULT_RISK_RULES", "NEGATIVE_QUERY_TERMS",
    "NEGATIVE_TERMS", "POSITIVE_TERMS", "contains_any", "heat_level",
    "matched_signal_terms", "negative_query_variants", "ratio", "risk_level", "risk_score",
]


def risk_level(
    negative_ratio: float,
    complaint_count: int,
    negative_engagement: int,
    rules: Mapping[str, Mapping[str, Any]],
) -> str:
    """Select the highest matching risk tier from adapter-provided rules."""
    for candidate_level in ("critical", "high", "medium"):
        rule = rules[candidate_level]
        if (
            negative_ratio >= float(rule["min_negative_ratio"])
            or complaint_count >= int(rule["min_complaints"])
            or negative_engagement >= int(rule["min_negative_engagement"])
        ):
            return candidate_level
    return "low"
