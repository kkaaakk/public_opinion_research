import { TERMINAL, type RunEvent, type RunSnapshot, type RunStatus, type Usage } from './protocol';

export const PHASES = [
  {
    id: 'intake',
    label: 'Intake',
    detail: '需求与材料',
    steps: ['enrich_query_images', 'clarify_with_user'],
  },
  {
    id: 'planning',
    label: 'Planning',
    detail: '研究简报与章节规划',
    steps: ['write_research_brief', 'plan_report_sections'],
  },
  { id: 'approval', label: 'Approval', detail: '不可变计划审批', steps: [] },
  {
    id: 'public_signal',
    label: 'Public Signal',
    detail: '公开信号 · Producer',
    steps: ['public_signal_agent'],
  },
  {
    id: 'internal_knowledge',
    label: 'Internal Knowledge',
    detail: '内部知识 · Producer',
    steps: ['internal_knowledge_agent'],
  },
  {
    id: 'review',
    label: 'Research Review',
    detail: '证据复核 / Gap / Follow-up',
    steps: ['research_review'],
  },
  {
    id: 'risk_assessment',
    label: 'Risk Assessment',
    detail: '风险评估 · Consumer',
    steps: ['risk_assessment_agent'],
  },
  {
    id: 'response_strategy',
    label: 'Response Strategy',
    detail: '应对策略 · Consumer',
    steps: ['response_strategy_agent'],
  },
  {
    id: 'reporting',
    label: 'Report Generation',
    detail: '章节 / 最终报告 / 记忆',
    steps: ['section_writer', 'write_final_sections', 'compile_final_report'],
  },
] as const;
export type PhaseId = (typeof PHASES)[number]['id'];
export type NodeStatus =
  | 'waiting'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'interrupted'
  | 'paused'
  | 'partial'
  | 'skipped';
export const EDGES: [PhaseId, PhaseId][] = [
  ['intake', 'planning'],
  ['planning', 'approval'],
  ['approval', 'public_signal'],
  ['approval', 'internal_knowledge'],
  ['public_signal', 'review'],
  ['internal_knowledge', 'review'],
  ['review', 'risk_assessment'],
  ['risk_assessment', 'response_strategy'],
  ['response_strategy', 'reporting'],
];
const stepPhase = new Map<string, PhaseId>(
  PHASES.flatMap((p) => p.steps.map((step) => [step, p.id] as [string, PhaseId])),
);
export const ROLE_LABELS: Record<string, string> = {
  public_signal: 'Public Signal',
  internal_knowledge: 'Internal Knowledge',
  risk_assessment: 'Risk Assessment',
  response_strategy: 'Response Strategy',
};
export const STATUS_LABELS: Record<string, string> = {
  waiting: '未执行',
  running: '执行中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '中断 / 待核对',
  paused: '等待审批',
  partial: '部分完成',
  skipped: '已跳过',
  queued: '等待调度',
  awaiting_approval: '等待审批',
  cancelling: '取消与清算中',
  settled: '已清算',
  pending: '清算中',
  unresolved: '待核对',
  started: '开始',
  returned: '已返回',
  uncertain: '效果未知',
  succeeded: '成功',
  unobserved: '结果未观测',
  not_executed: '未执行',
  exception: '异常结束',
};
export function phaseForEvent(e: RunEvent): PhaseId | null {
  if (e.step && stepPhase.has(e.step)) return stepPhase.get(e.step)!;
  const role = e.role?.replace(/_agent$/, '');
  if (role && ROLE_LABELS[role]) return role as PhaseId;
  if (e.type === 'research_followup' || e.type === 'research_review') return 'review';
  if (
    e.type.startsWith('approval') ||
    (e.type === 'run_status' && e.status === 'awaiting_approval')
  )
    return 'approval';
  if (e.type === 'report') return 'reporting';
  return null;
}
export function eventStatus(e: RunEvent): string {
  if (e.type === 'step_skipped') return 'skipped';
  if (e.type === 'step_started' || e.event === 'model_started' || e.event === 'agent_started')
    return 'started';
  if (e.type === 'step_completed' || e.event === 'model_completed' || e.event === 'agent_completed')
    return 'completed';
  if (
    e.type === 'step_failed' ||
    e.event === 'model_failed' ||
    e.event === 'agent_failed' ||
    e.type === 'error'
  )
    return e.status === 'cancelled' ? 'cancelled' : 'failed';
  return e.status ?? (typeof e.decision === 'string' ? e.decision : '');
}
export function describeEvent(e: RunEvent): string {
  if (e.type === 'sdk_event')
    return (
      ({
        session_started: 'SDK Session',
        agent_started: 'Agent 开始',
        agent_completed: 'Agent 结束',
        agent_failed: 'Agent 异常结束',
        model_started: '模型请求',
        model_completed: '模型响应',
        model_failed: '模型异常',
        tool: '工具效果回执',
        tool_result: '工具业务结果',
        harness: `Harness · ${e.stage ?? '阶段'}`,
      }[e.event ?? ''] ??
        e.event ??
        'SDK 事件') + (e.name ? ` · ${e.name}` : '')
    );
  return (
    {
      step_started: '步骤开始',
      step_completed: '步骤完成',
      step_skipped: '步骤已跳过',
      step_failed: '步骤异常',
      agent_report: '角色报告',
      report: '最终报告',
      research_review: '研究复核',
      research_followup: `补充研究 · Round ${e.round ?? '?'}`,
      retry_started: '重试授权请求',
      run_status: 'Run 状态',
      settlement: '清算状态',
      usage: '已结算用量',
      error: '执行错误',
      done: '代次事件交付完成',
      history_saved: '报告归档完成',
      archive_error: '归档等待补偿',
      approval_ended: '审批结束',
    }[e.type] ?? e.type
  );
}
export type StepInstance = {
  id: string;
  step: string;
  phase: PhaseId;
  status: NodeStatus;
  generation: number;
  start?: RunEvent;
  end?: RunEvent;
  unitId?: string;
  round?: number;
};
export type Projection = {
  statuses: Record<PhaseId, NodeStatus>;
  instances: StepInstance[];
  followups: RunEvent[];
  status?: string;
  generation: number;
  settlement?: string;
  usage?: Usage;
};
export function measuredDuration(start?: RunEvent, end?: RunEvent): number | undefined {
  if (end?.duration_ms !== undefined) return end.duration_ms;
  if (!start?.observed_at || !end?.observed_at) return undefined;
  const d = Date.parse(end.observed_at) - Date.parse(start.observed_at);
  return Number.isFinite(d) && d >= 0 ? d : undefined;
}

/** One deterministic fold for live data and cold replay. Never fill future nodes. */
export function projectRun(
  events: readonly RunEvent[],
  snapshot: RunSnapshot | null = null,
): Projection {
  const instances: StepInstance[] = [];
  const active = new Map<string, StepInstance>();
  let generation = 0;
  let status: string | undefined;
  let settlement: string | undefined;
  let usage: Usage | undefined;
  let approval: NodeStatus = 'waiting';
  const followups: RunEvent[] = [];
  for (const e of events) {
    generation =
      e.generation ??
      (e.type === 'retry_started' ? Number(e.generation ?? generation + 1) : generation);
    if (e.type === 'run_status') {
      status = e.status;
      if (status === 'awaiting_approval') approval = 'paused';
      else if (status === 'running' && approval === 'paused') approval = 'completed';
    }
    if (e.type === 'approval_ended')
      approval = e.decision === 'approve' ? 'completed' : 'cancelled';
    if (e.type === 'settlement') settlement = e.status;
    if (e.type === 'usage') usage = e.usage ?? (e as Usage);
    if (e.type === 'retry_started') {
      status = 'queued';
      settlement = undefined;
      for (const i of active.values()) if (i.status === 'running') i.status = 'interrupted';
      active.clear();
    }
    if (e.type === 'research_followup') followups.push(e);
    const phase = phaseForEvent(e);
    if (
      !e.step ||
      !phase ||
      !['step_started', 'step_completed', 'step_failed', 'step_skipped'].includes(e.type)
    )
      continue;
    const key = e.execution_id ?? `${generation}:${e.step}`;
    if (e.type === 'step_started') {
      const known = instances.find((i) => !!e.execution_id && i.id === e.execution_id);
      if (known) {
        known.start ??= e;
        known.round ??= e.research_round;
        known.unitId ??= e.unit_id;
        if (!known.end) active.set(key, known);
        continue;
      }
      const i: StepInstance = {
        id: e.execution_id ?? `${generation}:${e.step}:${e.seq}`,
        step: e.step,
        phase,
        status: 'running',
        generation,
        start: e,
        unitId: e.unit_id,
        round: e.research_round,
      };
      active.set(key, i);
      instances.push(i);
    } else {
      let i =
        active.get(key) ?? instances.findLast((i) => !!e.execution_id && i.id === e.execution_id);
      if (!i) {
        i = {
          id: e.execution_id ?? `end:${e.seq}`,
          step: e.step,
          phase,
          generation,
          status: 'waiting',
          unitId: e.unit_id,
        };
        instances.push(i);
      }
      i.end = e;
      i.status =
        e.type === 'step_skipped'
          ? 'skipped'
          : e.type === 'step_completed'
            ? 'completed'
            : e.error_kind === 'cancelled'
              ? 'cancelled'
              : 'failed';
      active.delete(key);
    }
  }
  if (snapshot) {
    status = snapshot.status;
    settlement = snapshot.settlement;
    generation = snapshot.generation;
    usage = snapshot.usage;
    if (status === 'awaiting_approval') approval = 'paused';
    if (snapshot.approval_required === false && snapshot.plan && approval === 'waiting')
      approval = 'skipped';
  }
  if (status && TERMINAL.has(status as RunStatus) && settlement !== 'pending') {
    if (approval === 'paused') approval = status === 'cancelled' ? 'cancelled' : 'interrupted';
    for (const i of instances)
      if (i.status === 'running') i.status = status === 'cancelled' ? 'cancelled' : 'interrupted';
  }
  const statuses = Object.fromEntries(PHASES.map((p) => [p.id, 'waiting'])) as Record<
    PhaseId,
    NodeStatus
  >;
  statuses.approval = approval;
  for (const p of PHASES) {
    if (p.id === 'approval') continue;
    const latest = p.steps.map((step) => instances.findLast((i) => i.step === step));
    const values = latest.map((i) => i?.status ?? 'waiting');
    statuses[p.id] = values.includes('running')
      ? 'running'
      : values.includes('failed')
        ? 'failed'
        : values.includes('interrupted')
          ? 'interrupted'
          : values.includes('cancelled')
            ? 'cancelled'
            : values.every((v) => v === 'skipped')
              ? 'skipped'
              : values.every((v) => v === 'completed' || v === 'skipped')
                ? 'completed'
                : values.includes('completed')
                  ? 'partial'
                  : 'waiting';
  }
  return { statuses, instances, followups, generation, status, settlement, usage };
}

export type SDKSpan = {
  id: string;
  kind: 'model' | 'tool';
  label: string;
  role?: string;
  invocation?: string;
  status: string;
  start?: RunEvent;
  end?: RunEvent;
  events: RunEvent[];
  usage?: Usage;
};
export function sdkSpans(events: readonly RunEvent[]): SDKSpan[] {
  const spans = new Map<string, SDKSpan>();
  const currentModel = new Map<string, string>();
  for (const e of events) {
    if (e.type !== 'sdk_event') continue;
    const scope = e.invocation_id ?? `legacy:${e.role ?? 'unknown'}`;
    if (e.event?.startsWith('model_')) {
      let key = `${scope}:model:${e.model_turn ?? ''}`;
      if (e.event === 'model_started') {
        if (e.model_turn === undefined) key += e.seq;
        currentModel.set(scope, key);
        const known = spans.get(key);
        if (known) {
          known.start ??= e;
          known.label = e.model_turn ? `模型调用 #${e.model_turn}` : '模型调用 · 轮次未提供';
          known.events.push(e);
          continue;
        }
        spans.set(key, {
          id: key,
          kind: 'model',
          label: e.model_turn ? `模型调用 #${e.model_turn}` : '模型调用 · 轮次未提供',
          role: e.role,
          invocation: e.invocation_id,
          status: 'running',
          start: e,
          events: [e],
        });
      } else {
        if (e.model_turn === undefined) key = currentModel.get(scope) ?? `unlinked:${e.seq}`;
        const span = spans.get(key) ?? {
          id: key,
          kind: 'model' as const,
          label: '模型响应 · 开始事件未提供',
          role: e.role,
          invocation: e.invocation_id,
          status: 'unknown',
          events: [],
        };
        span.end = e;
        span.status =
          e.event === 'model_completed'
            ? 'completed'
            : e.status === 'cancelled'
              ? 'cancelled'
              : 'failed';
        span.usage = e.usage;
        span.events.push(e);
        spans.set(key, span);
      }
    }
    if (e.event === 'tool' || e.event === 'tool_result') {
      const key = `${scope}:tool:${e.tool_call_id ?? e.operation_id ?? e.seq}`;
      const span = spans.get(key) ?? {
        id: key,
        kind: 'tool' as const,
        label: e.name ?? '工具名未提供',
        role: e.role,
        invocation: e.invocation_id,
        status: 'unknown',
        events: [],
      };
      if (e.status === 'started') {
        span.start = e;
        if (!span.end) span.status = 'running';
      } else if (span.end?.event !== 'tool_result' || e.event === 'tool_result') {
        span.end = e;
        span.status = e.status ?? 'unknown';
      }
      span.events.push(e);
      spans.set(key, span);
    }
    if (e.event === 'agent_failed')
      for (const span of spans.values()) {
        if (
          (span.invocation ? span.invocation === e.invocation_id : span.role === e.role) &&
          span.status === 'running'
        )
          span.status = e.status === 'cancelled' ? 'cancelled' : 'interrupted';
      }
  }
  return [...spans.values()].sort(
    (a, b) => (a.start?.seq ?? a.end?.seq ?? 0) - (b.start?.seq ?? b.end?.seq ?? 0),
  );
}
