# 检查成本分析中捕获的工具调用、参数和结果摘要。
"""Inspect captured tool calls in a cost profile artifact."""
import json
import sys
from collections import Counter
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "evaluation/cost_profile/round2_cost_profile.json")
d = json.loads(path.read_text(encoding="utf-8"))

tools = d.get("tool_capture_records") or []
print(f"captured tools: {len(tools)}")
by_name = Counter(t.get("tool") for t in tools)
print(dict(by_name))
print()

reasons = Counter()
for t in tools:
    if t.get("tool") == "tavily_search":
        for r in t.get("args", {}).get("queries", []) or []:
            reasons[str(r)] += 1
print("tavily queries:")
for q, n in reasons.most_common(50):
    print(f"  {n:2d}x {q}")
print()

print("web_search calls:")
for t in tools:
    if t.get("tool") == "web_search":
        args = t.get("args", {})
        res = t.get("result", {})
        print(f"  queries={args.get('queries')} max_results={args.get('max_results')} topic={args.get('topic')} dur={t.get('duration_ms')} res={json.dumps(res, ensure_ascii=False)[:120]}")

