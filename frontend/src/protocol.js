// UI-only projections. DBOS remains the authority; event order is authoritative.
export const TERMINAL = new Set(["completed", "failed", "cancelled", "interrupted"]);
export const PHASES = [
  { id: "planning", label: "研究规划", subtitle: "Intake / Plan", steps: ["enrich_query_images", "clarify_with_user", "write_research_brief", "plan_report_sections"] },
  { id: "public_signal", label: "公开信号研究", subtitle: "Producer · 1", steps: ["public_signal_agent"] },
  { id: "internal_knowledge", label: "内部知识研究", subtitle: "Producer · 2", steps: ["internal_knowledge_agent"] },
  { id: "review", label: "研究核验", subtitle: "Review / Follow-up", steps: ["research_review"] },
  { id: "risk_assessment", label: "风险评估", subtitle: "Consumer · 1", steps: ["risk_assessment_agent"] },
  { id: "response_strategy", label: "应对策略", subtitle: "Consumer · 2", steps: ["response_strategy_agent"] },
  { id: "reporting", label: "报告生成", subtitle: "Report", steps: ["section_writer", "write_final_sections", "compile_final_report"] }
];
export const EDGES = [
  ["planning", "public_signal"], ["planning", "internal_knowledge"],
  ["public_signal", "review"], ["internal_knowledge", "review"],
  ["review", "risk_assessment"], ["risk_assessment", "response_strategy"],
  ["response_strategy", "reporting"]
];
const byStep = new Map(PHASES.flatMap(phase => phase.steps.map(step => [step, phase.id])));
const byRole = new Map([
  ["public_signal", "public_signal"], ["internal_knowledge", "internal_knowledge"],
  ["risk_assessment", "risk_assessment"], ["response_strategy", "response_strategy"]
]);
export function phaseForEvent(event) {
  if (typeof event.step === "string") return byStep.get(event.step) || null;
  if (typeof event.role === "string") return byRole.get(event.role.replace(/_agent$/, "")) || null;
  if (event.type === "research_followup" || event.type === "research_review") return "review";
  if (event.type === "report") return "reporting";
  return null;
}
export function projectRun(events, snapshot, request) {
  const statuses = Object.fromEntries(PHASES.map(phase => [phase.id, "waiting"]));
  const counts = Object.fromEntries(PHASES.map(phase => [phase.id, 0]));
  const started = new Set();
  const finished = new Set();
  const list = Array.isArray(events) ? events : [];
  // Order is the DBOS cursor order; steps may repeat during follow-up or retry.
  for (const event of list) {
    const phase = phaseForEvent(event);
    if (!phase) continue;
    if (event.type === "step_started") {
      statuses[phase] = "running";
      started.add(event.step);
      counts[phase] += 1;
    } else if (event.type === "step_completed") {
      finished.add(event.step);
      if (PHASES.find(p => p.id === phase).steps.every(step => finished.has(step))) statuses[phase] = "completed";
    } else if (event.type === "agent_report" || event.type === "report") {
      statuses[phase] = "completed";
    } else if (event.type === "sdk_event" && event.event === "agent_started" && statuses[phase] === "waiting") {
      statuses[phase] = "running";
    } else if (event.type === "research_followup") {
      statuses[phase] = "running";
    }
  }
  // A late subscriber can still show persisted completed steps without fabricating an event.
  for (const step of snapshot?.completed_steps || []) {
    const phase = byStep.get(step);
    if (!phase) continue;
    finished.add(step);
  }
  for (const phase of PHASES) {
    if (phase.steps.length && phase.steps.every(step => finished.has(step))) statuses[phase.id] = "completed";
  }
  if (request && request.rag_enabled === false && !started.has("internal_knowledge_agent")) {
    statuses.internal_knowledge = "disabled";
  }
  const current = snapshot?.current_step && byStep.get(snapshot.current_step);
  if (current && statuses[current] !== "completed") statuses[current] = "running";
  if (snapshot?.status === "awaiting_approval") statuses.planning = "paused";
  if (snapshot?.status === "failed" || snapshot?.status === "interrupted") {
    if (current) statuses[current] = "error";
  }
  return { statuses, counts };
}

export function describeEvent(event) {
  const kind = event.type || "unknown";
  if (kind === "sdk_event") {
    const role = event.role ? event.role + " · " : "";
    return role + (event.event || "SDK event") + (event.name ? " · " + event.name : "");
  }
  if (kind === "step_started") return "开始步骤 · " + (event.step || "unknown");
  if (kind === "step_completed") return "步骤完成 · " + (event.step || "unknown");
  if (kind === "agent_report") return "角色报告 · " + (event.role || "unknown");
  if (kind === "research_review") return "研究核验完成";
  if (kind === "research_followup") return "补充研究 · 第 " + (event.round || "?") + " 轮";
  if (kind === "settlement") return "清算状态 · " + (event.status || "unknown");
  if (kind === "run_status") return "运行状态 · " + (event.status || "unknown");
  if (kind === "retry_started") return "已接收重试授权请求";
  if (kind === "error") return "执行错误 · " + (event.kind || "unknown");
  if (kind === "report") return "最终研究报告";
  if (kind === "usage") return "Token 统计";
  return kind.replace(/_/g, " ");
}

export function parseFrames(buffer) {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const frames = [];
  let rest = normalized;
  let end;
  while ((end = rest.indexOf("\n\n")) >= 0) {
    const frame = rest.slice(0, end);
    rest = rest.slice(end + 2);
    const data = frame.split("\n").filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
    if (data) frames.push(JSON.parse(data));
  }
  return { frames, rest };
}
