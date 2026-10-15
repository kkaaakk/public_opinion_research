# 对比不同研究轮次的报告长度和输入跨度，分析上下文增长。
"""Per-span ReAct input growth for Round 2 vs After profiles."""
import json
import sys
from pathlib import Path


# 计算记录中各阶段跨度的增长量，供成本变化比较。
def spans_growth(path, label):
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    calls = d["model_calls"]
    by_span = {}
    for c in calls:
        if c.get("call_type") != "react_reasoning":
            continue
        if c.get("input_tokens") is None:
            continue
        by_span.setdefault(c.get("span_id"), []).append(c)
    print(f"=== {label}")
    for span_id, rows in sorted(by_span.items(), key=lambda kv: kv[1][0].get("sequence") or 0):
        seq = [r["input_tokens"] for r in rows]
        role = rows[0].get("agent_role")
        print(f"  {role} span={str(span_id)[-6:]} calls={len(seq)} first={seq[0]} last={seq[-1]} max={max(seq)} min={min(seq)}")
    print()


spans_growth("evaluation/cost_profile/round2_cost_profile.json", "Round 2 (timeout @2400s)")
spans_growth("evaluation/cost_profile/after_cost_profile.json", "After 5989480")
