/** UI selection and navigation over observed facts; never execution commands. */
import {
  phaseForEvent,
  sdkSpans,
  type PhaseId,
  type Projection,
  type StepInstance,
} from './projection';
import type { RunEvent } from './protocol';

export type SDKFocus = {
  invocationId: string;
  executionId: string;
  generation?: number;
  role?: string;
  kind: 'all' | 'model' | 'tool';
};
export type InvocationSummary = { focus: SDKFocus; models: number; tools: number };

export function preferredPhase(
  projection: Projection,
  events: readonly RunEvent[],
): PhaseId | null {
  if (projection.statuses.approval === 'paused') return 'approval';
  const active = projection.instances.filter((i) => i.status === 'running');
  const latestActive = active.reduce<StepInstance | undefined>(
    (latest, i) => (!latest || (i.start?.seq ?? 0) > (latest.start?.seq ?? 0) ? i : latest),
    undefined,
  );
  if (latestActive) return latestActive.phase;
  const ended = projection.instances.filter((i) => i.end && i.status !== 'skipped');
  const latestEnd = ended.reduce<StepInstance | undefined>(
    (latest, i) => (!latest || i.end!.seq > latest.end!.seq ? i : latest),
    undefined,
  );
  if (latestEnd) return latestEnd.phase;
  const latest = events.findLast((e) => phaseForEvent(e) !== null);
  return latest ? phaseForEvent(latest) : null;
}

export function eventsForSDKFocus(events: readonly RunEvent[], focus: SDKFocus): RunEvent[] {
  return events.filter(
    (e) =>
      e.type === 'sdk_event' &&
      e.invocation_id === focus.invocationId &&
      e.execution_id === focus.executionId &&
      (focus.generation === undefined || e.generation === focus.generation),
  );
}

export function invocationsForInstance(
  events: readonly RunEvent[],
  instance: StepInstance,
): InvocationSummary[] {
  const executionId = instance.start?.execution_id ?? instance.end?.execution_id;
  if (!executionId) return []; // Legacy role-only records cannot prove an invocation/attempt link.
  const groups = new Map<string, SDKFocus>();
  for (const e of events) {
    if (
      e.type !== 'sdk_event' ||
      !e.invocation_id ||
      e.execution_id !== executionId ||
      (e.generation !== undefined && e.generation !== instance.generation)
    )
      continue;
    const key = `${e.invocation_id}:${e.generation ?? 'unknown'}`;
    groups.set(key, {
      invocationId: e.invocation_id,
      executionId,
      generation: e.generation,
      role: e.role,
      kind: 'all',
    });
  }
  return [...groups.values()].map((focus) => {
    const spans = sdkSpans(eventsForSDKFocus(events, focus));
    return {
      focus,
      models: spans.filter((s) => s.kind === 'model').length,
      tools: spans.filter((s) => s.kind === 'tool').length,
    };
  });
}
