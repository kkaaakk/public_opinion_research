# 对比三个配置或版本的研究成本、报告规模与运行结果。
"""Compare After 5989480 (deepseek-chat), After 5989480 (v4-flash), and Round 2 (main + v4-flash)."""
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent

FILES = {
    "after_chat": HERE / "after_cost_profile.json",
    "after_v4flash": HERE / "after_v4flash_cost_profile.json",
    "round2_v4flash": HERE / "round2_full_cost_profile.json",
}


# 提取单份测量产物的可比较统计值。
def stats(path):
    d = json.loads(path.read_text(encoding="utf-8"))
    calls = d["model_calls"]
    types = Counter(c.get("call_type") for c in calls)
    known_in = sum(c["input_tokens"] for c in calls if isinstance(c.get("input_tokens"), int))
    known_out = sum(c.get("output_tokens") or 0 for c in calls if isinstance(c.get("input_tokens"), int))
    dur = sum(c.get("duration_ms") or 0 for c in calls)
    roles = Counter(c.get("agent_role") for c in calls if c.get("agent_role"))
    react = [c for c in calls if c.get("call_type") == "react_reasoning" and isinstance(c.get("input_tokens"), int)]
    return {
        "status": d["status"],
        "elapsed_s": round(d["elapsed_ms"] / 1000, 1),
        "model_calls": len(calls),
        "tool_calls": len(d["tool_calls"]),
        "types": dict(types.most_common()),
        "roles": dict(roles.most_common()),
        "known_in": known_in,
        "known_out": known_out,
        "known_total": known_in + known_out,
        "sum_call_duration_s": round(dur / 1000, 1),
        "react_max_input": max((c["input_tokens"] for c in react), default=None),
        "react_calls": len(react),
        "final_report_chars": len(d.get("final_report") or ""),
        "reviews": len(d.get("research_reviews") or []),
        "completed_tasks": len(d.get("completed_research_tasks") or []),
    }


rows = {}
for name, path in FILES.items():
    if path.exists():
        rows[name] = stats(path)
    else:
        rows[name] = None

keys = [
    "status", "elapsed_s", "model_calls", "tool_calls", "known_in", "known_out",
    "known_total", "sum_call_duration_s", "react_calls", "react_max_input",
    "final_report_chars", "reviews", "completed_tasks",
]
print(f"{'metric':24s} {'after_chat':>16s} {'after_v4flash':>16s} {'round2_v4flash':>16s}")
for key in keys:
    values = []
    for name in FILES:
        row = rows[name]
        values.append("-" if row is None else str(row.get(key)))
    print(f"{key:24s} {values[0]:>16s} {values[1]:>16s} {values[2]:>16s}")

print()
for name in FILES:
    row = rows[name]
    if row:
        print(f"{name} call types: {row['types']}")
        print(f"{name} roles: {row['roles']}")
        print()
