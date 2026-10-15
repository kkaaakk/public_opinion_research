# 从已捕获流事件恢复角色报告，生成便于人工检查的报告文件。
"""Rebuild role-report/final-report analysis from raw Round 2 stream updates.

The current main stream emits v2 `(namespace, data)` tuple chunks that the
shared collector did not normalize, so `stream_updates` in the Round 2 artifact
holds raw `[[namespace...], {node: update}]` entries.  This script re-normalizes
them and recomputes the report-level analysis the original runner could not.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_cost_profile as base  # noqa: E402

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))


# 把存储的流条目统一为命名空间、节点和状态变化字典。
def normalize(entry):
    """Return (namespace, node, changes_dict) for a stored stream entry."""
    changes = entry.get("changes")
    if (
        isinstance(changes, list)
        and len(changes) == 2
        and isinstance(changes[1], dict)
        and entry.get("node") == "<non_mapping_update>"
    ):
        namespace = changes[0] if isinstance(changes[0], list) else []
        for node, update in changes[1].items():
            if isinstance(update, dict):
                return namespace, node, update
        return namespace, "<empty>", {}
    return entry.get("namespace") or [], entry.get("node"), changes


role_reports: dict[str, str] = {}
round_reports: list[dict] = []
reviews: list[dict] = []
completed_tasks: list[dict] = []
final_report = ""
sections_seen: list[dict] = []
seen_reports: set[tuple] = set()

for entry in data.get("stream_updates", []):
    namespace, node, changes = normalize(entry)
    if not isinstance(changes, dict):
        continue
    # role reports now live under the ownership-grouped `agents` state key
    agents = changes.get("agents")
    if isinstance(agents, dict):
        for role, payload in agents.items():
            if not isinstance(payload, dict):
                continue
            report = str(payload.get("report") or "")
            if not report:
                continue
            round_match = re.search(r"Research round:\s*(\d+)", report, re.IGNORECASE)
            report_round = int(round_match.group(1)) if round_match else 1
            fingerprint = (role, report_round, report[:2000])
            if fingerprint in seen_reports:
                continue
            seen_reports.add(fingerprint)
            previous = role_reports.get(role, "")
            if not previous:
                role_reports[role] = report
            elif report not in previous:
                role_reports[role] = previous + f"\n\n--- round {report_round} ---\n\n" + report
            round_reports.append({
                "role": role,
                "round": report_round,
                "chars": len(report),
                "estimated_tokens": (len(report) + 3) // 4,
                "node": node,
                "namespace": namespace,
            })
    workflow = changes.get("workflow")
    if isinstance(workflow, dict):
        review = workflow.get("review")
        if isinstance(review, dict) and "research_complete" in review:
            if not any(r.get("summary") == review.get("summary") for r in reviews):
                reviews.append(review)
        for task in workflow.get("completed_tasks") or []:
            if isinstance(task, dict):
                identity = str(task.get("task_id") or task.get("objective"))
                if identity not in {str(t.get("task_id") or t.get("objective")) for t in completed_tasks}:
                    completed_tasks.append(task)
    report_state = changes.get("report")
    if isinstance(report_state, dict):
        final = report_state.get("final")
        if isinstance(final, str) and final:
            final_report = final
        for section in report_state.get("sections") or []:
            if isinstance(section, dict):
                sections_seen.append(section)

print(f"role reports: {[(k, len(v)) for k, v in role_reports.items()]}")
print(f"round reports: {[(r['role'], r['round'], r['chars']) for r in round_reports]}")
print(f"reviews: {len(reviews)}")
if reviews:
    first = reviews[0]
    print("review keys:", sorted(first.keys()))
    print("  complete:", first.get("research_complete"))
    print("  gaps:", str(first.get("research_gaps") or first.get("gaps"))[:300])
    print("  conflicts:", str(first.get("conflicts"))[:200])
print(f"completed tasks: {len(completed_tasks)}")
print(f"final report chars: {len(final_report)}")
print(f"sections seen: {len(sections_seen)}")

calls = data["model_calls"]
reuse = base.repeated_context_analysis(calls, round_reports, data.get("stream_updates", []))
growth = base.report_sizes(round_reports)
print()
print("repeated context:", json.dumps(reuse, ensure_ascii=False)[:800])
print()
print("report growth:", json.dumps(growth, ensure_ascii=False)[:400])

out = {
    "role_reports": role_reports,
    "round_reports": round_reports,
    "research_reviews": reviews,
    "completed_research_tasks": completed_tasks,
    "final_report": final_report,
    "sections": sections_seen,
    "analysis": {
        "role_reports_reuse": reuse,
        "report_growth": growth,
    },
}
path.with_name(path.stem + "_reports.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(f"\nwritten {path.with_name(path.stem + '_reports.json').name}")
