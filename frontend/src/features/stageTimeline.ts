import type { Run } from '@/api/client';
import type { RunEvent } from '@/api/events';

export type Stage = {
  id: string;
  unit?: string;
  step: string;
  order: number;
  generation: number;
  round?: number;
  state: 'running' | 'completed' | 'failed' | 'recorded';
  result?: string;
  error?: string;
  review?: RunEvent['review'];
  tools: Map<string, RunEvent>;
};

const known = new Set([
  'plan_research',
  'enrich_query_images',
  'revise_brief',
  'public_signal_agent',
  'internal_knowledge_agent',
  'research_review',
  'risk_assessment_agent',
  'response_strategy_agent',
  'generate_final_report',
]);

function visible(run: Run, step: string) {
  if (!known.has(step)) return false;
  if (step === 'enrich_query_images' && !run.images.length) return false;
  if (step === 'internal_knowledge_agent' && run.request.rag_enabled === false) return false;
  const role = step.replace(/_agent$/, '');
  return (
    !step.endsWith('_agent') ||
    run.enabled_roles.includes(role) ||
    run.role_reports.some((artifact) => artifact.role === role)
  );
}

function roundFor(event: RunEvent) {
  if (event.research_round) return event.research_round;
  return (
    Number(
      event.unit_id?.match(/:(?:review:|plan:|public_signal:|internal_knowledge:)(\d+)$/)?.[1],
    ) || undefined
  );
}

export function stageTimeline(run: Run, events: RunEvent[]) {
  const stages = new Map<string, Stage>();
  for (const event of events) {
    const step = event.step;
    const generation = event.generation ?? 0;
    if (!step || !visible(run, step) || generation > run.generation) continue;
    if (
      ![
        'step_started',
        'step_completed',
        'step_failed',
        'step_skipped',
        'agent_report',
        'research_review',
        'sdk_event',
      ].includes(event.type)
    )
      continue;
    const round = roundFor(event);
    const id = generation + ':' + (event.unit_id ?? step + ':' + (round ?? ''));
    if (event.type === 'step_skipped') {
      stages.delete(id);
      continue;
    }
    const stage: Stage = stages.get(id) ?? {
      id,
      unit: event.unit_id,
      order: event.seq,
      generation,
      step,
      round,
      state: 'recorded',
      tools: new Map(),
    };
    if (event.type === 'step_started' && stage.state === 'recorded') stage.state = 'running';
    if (event.type === 'step_completed') stage.state = 'completed';
    if (event.type === 'step_failed') {
      stage.state = 'failed';
      stage.error = event.error_kind;
    }
    if (event.type === 'agent_report') stage.result = event.content;
    if (event.type === 'research_review') stage.review = event.review;
    if (
      event.type === 'sdk_event' &&
      event.name &&
      ['tool', 'tool_result'].includes(event.event ?? '')
    ) {
      const identity = event.tool_call_id
        ? (event.invocation_id ?? '') + ':' + event.tool_call_id
        : String(event.seq);
      stage.tools.set(identity, event);
    }
    stages.set(id, stage);
  }

  for (const artifact of run.role_reports) {
    const step = artifact.role + '_agent';
    if (!visible(run, step)) continue;
    const existing = Array.from(stages.values()).findLast((stage) =>
      artifact.stage_id ? stage.unit === artifact.stage_id : stage.step === step,
    );
    if (existing) {
      if (existing.state !== 'failed') existing.state = 'completed';
      existing.result ??= artifact.content;
      continue;
    }
    const id = 'artifact:' + (artifact.stage_id ?? step);
    stages.set(id, {
      id,
      step,
      order: 1e12,
      generation: run.generation,
      state: 'completed',
      round: Number(artifact.stage_id?.match(/:(\d+)$/)?.[1]) || undefined,
      tools: new Map(),
      result: artifact.content,
    });
  }

  for (const step of new Set(run.completed_steps)) {
    if (!visible(run, step) || Array.from(stages.values()).some((item) => item.step === step))
      continue;
    stages.set(step, {
      id: step,
      step,
      order: 0,
      generation: run.generation,
      state: 'completed',
      tools: new Map(),
    });
  }
  const current =
    run.current_step === 'plan' ||
    (!run.current_step && run.status === 'running' && run.phase === 'planning')
      ? 'plan_research'
      : run.current_step;
  if (
    run.status === 'running' &&
    visible(run, current) &&
    !Array.from(stages.values()).some(
      (item) => item.step === current && item.generation === run.generation,
    )
  ) {
    stages.set('current:' + current, {
      id: 'current:' + current,
      step: current,
      order: 1e12,
      generation: run.generation,
      state: 'running',
      tools: new Map(),
    });
  }
  return Array.from(stages.values()).sort((a, b) => a.order - b.order);
}
