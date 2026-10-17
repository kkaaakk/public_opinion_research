# 读取成本分析产物，检查报告质量及可见证据情况。
"""Quality signals for Round 2 reports: citations, coverage, conflicts, gaps."""
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
d = json.loads(path.read_text(encoding="utf-8"))

reports = d.get("role_reports") or {}
print("=== role report quality ===")
for role, text in reports.items():
    urls = set(re.findall(r"https?://[^\s)>\]]+", text))
    dates = set(re.findall(r"20\d{2}[-/年]\d{1,2}", text))
    print(f"{role}: {len(text)} chars, {len(urls)} unique URLs, {len(dates)} distinct date markers")
    print(f"  reports urls sample: {sorted(urls)[:5]}")

print()
print("=== round reports ===")
for r in d.get("round_reports") or []:
    print(f"{r['role']} R{r['round']}: {r['chars']} chars")

print()
print("=== review #1 ===")
reviews = d.get("research_reviews") or []
for i, review in enumerate(reviews, 1):
    print(f"review {i}: complete={review.get('research_complete')}")
    for key in ("confirmed_findings", "unresolved_claims", "conflicts", "research_gaps"):
        values = review.get(key) or []
        print(f"  {key}: {len(values)}")
        for v in values[:2]:
            print(f"    - {str(v)[:150]}")
    tasks = review.get("next_tasks") or []
    print(f"  next_tasks: {len(tasks)}")
    for t in tasks[:4]:
        if isinstance(t, dict):
            print(f"    - [{t.get('target_role') or t.get('role')}] p={t.get('priority')} {str(t.get('objective'))[:110]}")

print()
print("=== completed tasks ===")
for t in d.get("completed_research_tasks") or []:
    if isinstance(t, dict):
        print(f"  [{t.get('target_role') or t.get('role')}] {str(t.get('objective') or t.get('summary'))[:120]}")
