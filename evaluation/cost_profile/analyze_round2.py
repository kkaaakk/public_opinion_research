# 汇总第二轮成本分析数据，检查节点、工具及复核结果。
"""Summarize a cost-profile artifact (Round 2 analysis helper)."""
import json
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "evaluation/cost_profile/round2_cost_profile.json")
d = json.loads(path.read_text(encoding="utf-8"))

print(f"file={path.name} status={d['status']} elapsed_ms={d['elapsed_ms']}")
print(f"commit={d.get('commit')} final_report_len={len(d.get('final_report',''))}")
print(f"attempts={json.dumps(d.get('attempts'), ensure_ascii=False)[:400]}")
print()

rows = d["flow"]["node_rows"]
print(f"{'node':34s} {'exec':>4s} {'calls':>5s} {'tools':>5s} {'in_tok':>9s} {'out_tok':>8s}")
for r in rows:
    t = r["tokens"]
    print(
        f"{r['node']:34s} {r['executions']:4d} {r['model_calls']:5d} {r['tool_calls']:5d} "
        f"{t['known_input_tokens']:9d} {t['known_output_tokens']:8d}"
    )
print()

tot_in = sum(r["tokens"]["known_input_tokens"] for r in rows)
tot_out = sum(r["tokens"]["known_output_tokens"] for r in rows)
print(f"known totals: input={tot_in} output={tot_out} total={tot_in+tot_out}")
print()

print("call-type breakdown:")
from collections import Counter  # noqa: E402

types = Counter(c.get("call_type") for c in d["model_calls"])
for k, v in types.most_common():
    print(f"  {k}: {v}")
print()

print("react input progression:")
for item in d["analysis"]["react_input_progression"]:
    seq = [v for v in (item.get("input_tokens") or []) if v is not None]
    print(f"  {item.get('agent')} R{item.get('round')}: calls={item.get('calls')} first={seq[0] if seq else None} last={seq[-1] if seq else None} max={max(seq) if seq else None}")
print()

print("per agent x call_type:")
from collections import Counter  # noqa: E402

combo = Counter((c.get("agent_role"), c.get("call_type")) for c in d["model_calls"])
for (role, ctype), count in sorted(combo.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
    print(f"  {role} x {ctype}: {count}")
print()

print("per agent x round x call_type for react:")
combo2 = Counter(
    (c.get("agent_role"), c.get("round"), c.get("call_type"))
    for c in d["model_calls"]
    if c.get("call_type") in {"react_reasoning", "webpage_summarization", "research_compression"}
)
for key, count in sorted(combo2.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]), str(kv[0][2]))):
    print(f"  {key[0]} R{key[1]} {key[2]}: {count}")
print()

print("role_reports reuse (estimated):")
rr = d["analysis"]["role_reports_reuse"]
for consumer in rr.get("consumers", rr if isinstance(rr, list) else []):
    print("  ", json.dumps(consumer, ensure_ascii=False)[:220])
print()

print("token shares:", json.dumps(d["analysis"]["token_shares"], ensure_ascii=False)[:600])
print()
print("diagnostics:", json.dumps(d["diagnostics"], ensure_ascii=False))
