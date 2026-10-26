/** Additive public v1 protocol. No transcripts or execution authority live here. */
export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type Usage = {
  model_calls?: number;
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
};
export type RunStatus =
  | 'queued'
  | 'running'
  | 'awaiting_approval'
  | 'cancelling'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'interrupted';
export const TERMINAL = new Set<RunStatus>(['completed', 'failed', 'cancelled', 'interrupted']);
export type RunRequest = {
  topic: string;
  model: string;
  mode: 'fast' | 'normal' | 'deep';
  search_api: 'tavily';
  org_context: string;
  rag_enabled: boolean;
  require_approval: boolean;
};
export type RoleReport = {
  role: string;
  content: string;
  stage_id?: string;
  completed_at?: string;
};
export type Plan = {
  version: string;
  brief: string;
  sections: { name: string }[];
  expires_at: string;
};
export type RunSnapshot = {
  run_id: string;
  status: RunStatus;
  native_status: string;
  event_cursor: number;
  state_version: number;
  generation: number;
  settlement: string;
  events_complete: boolean;
  events_paused: boolean;
  retry_safe: boolean;
  retry_requested: boolean;
  current_step?: string;
  completed_steps: string[];
  created_at?: string;
  updated_at?: string;
  terminated_at?: string | null;
  usage: Usage;
  error: Record<string, Json>;
  report: string;
  role_reports: RoleReport[];
  plan: Plan | null;
  request?: Partial<RunRequest>;
  approval_required?: boolean;
  archive_status?: string;
  history_id?: string | null;
  workflow_version?: string;
  sdk_version?: string;
  runtime_version?: string;
  research_session_id?: string;
};
export type RunEvent = {
  protocol_version: 1;
  run_id: string;
  seq: number;
  timestamp: string;
  type: string;
  generation?: number;
  observed_at?: string;
  step?: string;
  role?: string;
  event?: string;
  unit_id?: string;
  execution_id?: string;
  invocation_id?: string;
  task_id?: string;
  tool_call_id?: string;
  graph_call_id?: string;
  operation_id?: string;
  model_turn?: number;
  model_calls?: number;
  research_round?: number;
  duration_ms?: number;
  name?: string;
  status?: string;
  stage?: string;
  reason?: string;
  content?: string;
  usage?: Usage;
  round?: number;
  roles?: string[];
  error_type?: string;
  error_kind?: string;
  [key: string]: unknown;
};
export type EventPage = { items: RunEvent[]; next_cursor: number; has_more: boolean };
export type NativeWorkflow = {
  workflow_id: string;
  name: string;
  status: string;
  queue_name: string | null;
  app_version: string | null;
  parent_workflow_id: string | null;
  created_at: number | null;
  updated_at: number | null;
  completed_at: number | null;
  dequeued_at: number | null;
};
export type NativeStep = {
  function_id: number;
  function_name: string;
  child_workflow_id: string | null;
  started_at_epoch_ms: number | null;
  completed_at_epoch_ms: number | null;
};
export type ExecutionPage = {
  run_id: string;
  workflow_id: string;
  workflows: NativeWorkflow[];
  steps: NativeStep[];
  workflows_has_more: boolean;
  next_workflow_offset: number;
  steps_has_more: boolean;
  next_offset: number;
};
export type EvidenceNode = {
  node_id: string;
  node_type: string;
  role?: string;
  research_round?: number;
  task_id?: string;
  properties: Record<string, Json>;
};
export type EvidenceEdge = {
  edge_id: string;
  relation_type: string;
  source_id: string;
  target_id: string;
};
export type EvidencePage = {
  run_id: string;
  available: boolean;
  reason?: string;
  nodes: EvidenceNode[];
  edges: EvidenceEdge[];
  has_more: boolean;
  next_cursor?: string;
  edges_has_more: boolean;
  next_edge_cursor?: string;
};
export type HistoryItem = {
  id: string;
  run_id: string;
  topic: string;
  status: string;
  completed_at: string;
  [key: string]: unknown;
};
export type HistoryRecord = HistoryItem & {
  report: string;
  role_reports: RoleReport[];
  usage: Usage;
};
export type HistoryPage = { items: HistoryItem[]; total: number };

export class ProtocolError extends Error {}
export function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw new ProtocolError('接口返回了无效对象');
  return value as Record<string, unknown>;
}
export function integer(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0)
    throw new ProtocolError('无效事件游标或计数');
  return value;
}
export function string(value: unknown): string {
  if (typeof value !== 'string') throw new ProtocolError('接口缺少字符串字段');
  return value;
}
export function array(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw new ProtocolError('接口缺少数组字段');
  return value;
}
export function boolean(value: unknown): boolean {
  if (typeof value !== 'boolean') throw new ProtocolError('接口缺少布尔字段');
  return value;
}
export function parseUsage(value: unknown): Usage {
  const v = record(value);
  const usage: Usage = {};
  for (const key of ['model_calls', 'input_tokens', 'output_tokens', 'total_tokens'] as const) {
    if (v[key] !== undefined) usage[key] = integer(v[key]);
  }
  return usage;
}
export function optionalString(v: unknown): string | undefined {
  return v == null ? undefined : string(v);
}
export function jsonValue(v: unknown): Json {
  if (v === null || typeof v === 'string' || typeof v === 'boolean') return v;
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (Array.isArray(v)) return v.map(jsonValue);
  return jsonObject(v);
}
export function jsonObject(v: unknown): Record<string, Json> {
  return Object.fromEntries(
    Object.entries(record(v)).map(([key, value]) => [key, jsonValue(value)]),
  );
}
export function parseRoleReports(v: unknown): RoleReport[] {
  return array(v).map((item) => {
    const r = record(item);
    return {
      role: string(r.role),
      content: string(r.content),
      stage_id: optionalString(r.stage_id),
      completed_at: optionalString(r.completed_at),
    };
  });
}
function parseRequest(v: unknown): Partial<RunRequest> | undefined {
  if (v == null) return undefined;
  const r = record(v);
  const request: Partial<RunRequest> = {};
  if (r.topic !== undefined) request.topic = string(r.topic);
  if (r.model !== undefined) request.model = string(r.model);
  if (r.rag_enabled !== undefined) request.rag_enabled = boolean(r.rag_enabled);
  if (r.require_approval !== undefined) request.require_approval = boolean(r.require_approval);
  if (r.mode !== undefined) {
    if (r.mode !== 'fast' && r.mode !== 'normal' && r.mode !== 'deep')
      throw new ProtocolError('未知研究模式');
    request.mode = r.mode;
  }
  return request;
}
export function parseEvent(value: unknown): RunEvent {
  const v = record(value);
  if (v.protocol_version !== 1) throw new ProtocolError('不支持的事件协议版本');
  const event: RunEvent = {
    ...v,
    protocol_version: 1,
    seq: integer(v.seq),
    run_id: string(v.run_id),
    timestamp: string(v.timestamp),
    type: string(v.type),
  };
  if (event.seq < 1 || !Number.isFinite(Date.parse(event.timestamp)))
    throw new ProtocolError('事件缺少有效序号或时间');
  for (const key of [
    'step',
    'role',
    'event',
    'observed_at',
    'unit_id',
    'execution_id',
    'invocation_id',
    'task_id',
    'tool_call_id',
    'graph_call_id',
    'operation_id',
    'name',
    'status',
    'stage',
    'reason',
    'content',
    'error_type',
    'error_kind',
  ] as const) {
    if (v[key] !== undefined) event[key] = string(v[key]);
  }
  for (const key of [
    'generation',
    'model_turn',
    'model_calls',
    'research_round',
    'round',
  ] as const) {
    if (v[key] !== undefined) event[key] = integer(v[key]);
  }
  if (v.duration_ms !== undefined) {
    if (typeof v.duration_ms !== 'number' || !Number.isFinite(v.duration_ms) || v.duration_ms < 0)
      throw new ProtocolError('无效耗时');
    event.duration_ms = v.duration_ms;
  }
  if (v.usage !== undefined) event.usage = parseUsage(v.usage);
  if (v.roles !== undefined) event.roles = array(v.roles).map(string);
  return event;
}
export function parseSnapshot(value: unknown): RunSnapshot {
  const v = record(value);
  const status = string(v.status) as RunStatus;
  if (!['queued', 'running', 'awaiting_approval', 'cancelling', ...TERMINAL].includes(status))
    throw new ProtocolError('未知 Run 状态');
  const reports = parseRoleReports(v.role_reports);
  let plan: Plan | null = null;
  if (v.plan) {
    const p = record(v.plan);
    plan = {
      version: string(p.version),
      brief: string(p.brief),
      expires_at: string(p.expires_at),
      sections: array(p.sections).map((s) => ({ name: string(record(s).name) })),
    };
  }
  return {
    run_id: string(v.run_id),
    status,
    native_status: string(v.native_status),
    event_cursor: integer(v.event_cursor),
    state_version: integer(v.state_version),
    generation: integer(v.generation),
    settlement: string(v.settlement),
    events_complete: boolean(v.events_complete),
    events_paused: boolean(v.events_paused),
    retry_safe: boolean(v.retry_safe),
    retry_requested: boolean(v.retry_requested),
    completed_steps: array(v.completed_steps).map(string),
    usage: parseUsage(v.usage),
    error: jsonObject(v.error),
    report: string(v.report),
    role_reports: reports,
    plan,
    request: parseRequest(v.request),
    approval_required: v.approval_required === undefined ? undefined : boolean(v.approval_required),
    current_step: optionalString(v.current_step),
    created_at: optionalString(v.created_at),
    updated_at: optionalString(v.updated_at),
    terminated_at: v.terminated_at === null ? null : optionalString(v.terminated_at),
    archive_status: optionalString(v.archive_status),
    history_id: v.history_id === null ? null : optionalString(v.history_id),
    workflow_version: optionalString(v.workflow_version),
    sdk_version: optionalString(v.sdk_version),
    runtime_version: optionalString(v.runtime_version),
    research_session_id: optionalString(v.research_session_id),
  };
}
export function isDelivered(snapshot: RunSnapshot, cursor: number): boolean {
  return (
    TERMINAL.has(snapshot.status) &&
    snapshot.settlement !== 'pending' &&
    snapshot.events_complete &&
    !snapshot.retry_requested &&
    cursor >= snapshot.event_cursor
  );
}

/** Incremental SSE parsing, including CR/LF split across transport packets. */
export class SSEDecoder {
  private line = '';
  private data: string[] = [];
  private id = '';
  private sawCR = false;
  private size = 0;
  push(chunk: string): { id: string; data: string }[] {
    const frames: { id: string; data: string }[] = [];
    for (const ch of chunk) {
      if (this.sawCR && ch === '\n') {
        this.sawCR = false;
        continue;
      }
      this.sawCR = ch === '\r';
      if (ch === '\n' || ch === '\r') {
        if (!this.line) {
          if (this.data.length) frames.push({ id: this.id, data: this.data.join('\n') });
          this.data = [];
          this.id = '';
          this.size = 0;
        } else if (this.line.startsWith('data:'))
          this.data.push(this.line.slice(5).replace(/^ /, ''));
        else if (this.line.startsWith('id:')) this.id = this.line.slice(3).replace(/^ /, '');
        this.line = '';
      } else {
        this.line += ch;
        if (++this.size > 2_000_000) throw new ProtocolError('SSE 帧超过展示上限；游标未跳过');
      }
    }
    return frames;
  }
}
export function appendEvents(
  existing: readonly RunEvent[],
  incoming: readonly RunEvent[],
  runId: string,
): RunEvent[] {
  const added: RunEvent[] = [];
  let cursor = existing.at(-1)?.seq ?? 0;
  for (const event of incoming) {
    if (event.run_id !== runId) throw new ProtocolError('拒绝其他 Run 的事件');
    if (event.seq <= cursor) continue;
    if (event.seq !== cursor + 1)
      throw new ProtocolError(`事件游标不连续：期望 #${cursor + 1}，收到 #${event.seq}`);
    added.push(event);
    cursor = event.seq;
  }
  return [...existing, ...added];
}
