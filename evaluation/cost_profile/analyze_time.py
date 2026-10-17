# 分析成本测量记录中的阶段耗时与执行时间分布。
"""Time and token distribution by call type for cost profile artifacts."""
import json
import sys
from collections import defaultdict
from pathlib import Path


# 读取指定版本的成本分析 JSON 数据。
def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# 汇总并输出一个版本的耗时分析。
def report(path):
    d = load(path)
    print(f"=== {Path(path).name} status={d['status']} elapsed_ms={d['elapsed_ms']}")
    by_type = defaultdict(lambda: {"calls": 0, "dur": 0, "in": 0, "out": 0, "missing": 0})
    for c in d["model_calls"]:
        t = by_type[c.get("call_type") or "unknown"]
        t["calls"] += 1
        t["dur"] += c.get("duration_ms") or 0
        if c.get("input_tokens") is not None:
            t["in"] += c["input_tokens"]
            t["out"] += c.get("output_tokens") or 0
        else:
            t["missing"] += 1
    for name, t in sorted(by_type.items(), key=lambda kv: -kv[1]["dur"]):
        print(
            f"  {name:26s} calls={t['calls']:4d} dur={t['dur']/1000:9.1f}s "
            f"in={t['in']:9d} out={t['out']:8d} missing_usage={t['missing']}"
        )
    tool_dur = defaultdict(lambda: {"calls": 0, "dur": 0})
    for t in d["tool_calls"]:
        name = t.get("call_type") or t.get("tool") or "unknown"
        tool_dur[name]["calls"] += 1
        tool_dur[name]["dur"] += t.get("duration_ms") or 0
    for name, t in sorted(tool_dur.items(), key=lambda kv: -kv[1]["dur"]):
        print(f"  tool {name:22s} calls={t['calls']:4d} dur={t['dur']/1000:9.1f}s")
    print()


for p in sys.argv[1:]:
    report(p)
