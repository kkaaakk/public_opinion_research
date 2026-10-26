import { expect, it } from 'vitest';
import { measuredDuration, projectRun, sdkSpans } from '../src/projection';
import { event, snapshot } from './fixtures';
it('projects concurrent producers without completing future nodes', () => {
  const p = projectRun([
    event(1, { type: 'step_started', step: 'public_signal_agent', execution_id: 'a' }),
    event(2, { type: 'step_started', step: 'internal_knowledge_agent', execution_id: 'b' }),
    event(3, { type: 'step_completed', step: 'public_signal_agent', execution_id: 'a' }),
  ]);
  expect(p.statuses.public_signal).toBe('completed');
  expect(p.statuses.internal_knowledge).toBe('running');
  expect(p.statuses.review).toBe('waiting');
  expect(p.statuses.risk_assessment).toBe('waiting');
});
it('does not infer disabled or completed Agents from the RAG switch', () => {
  expect(
    projectRun([], snapshot({ request: { rag_enabled: false } })).statuses.internal_knowledge,
  ).toBe('waiting');
});
it('only skips an Agent when an actual server fact confirms the disabled role', () => {
  const p = projectRun([
    event(1, { type: 'step_started', step: 'internal_knowledge_agent', execution_id: 'unit' }),
    event(2, {
      type: 'step_skipped',
      step: 'internal_knowledge_agent',
      execution_id: 'unit',
      reason: 'role_disabled',
    }),
  ]);
  expect(p.statuses.internal_knowledge).toBe('skipped');
  expect(p.statuses.public_signal).toBe('waiting');
});
it('creates separate followup and retry instances, preserving completed producers', () => {
  const events = [
    event(1, { type: 'step_started', step: 'public_signal_agent', execution_id: 'p1' }),
    event(2, { type: 'step_completed', step: 'public_signal_agent', execution_id: 'p1' }),
    event(3, { type: 'research_followup', round: 2, roles: ['public_signal'] }),
    event(4, {
      type: 'step_started',
      step: 'public_signal_agent',
      execution_id: 'p2',
      research_round: 2,
    }),
    event(5, { type: 'step_completed', step: 'public_signal_agent', execution_id: 'p2' }),
    event(6, { type: 'step_started', step: 'research_review', execution_id: 'r1' }),
    event(7, { type: 'step_failed', step: 'research_review', execution_id: 'r1' }),
    event(8, { type: 'retry_started', generation: 1 }),
    event(9, { type: 'step_started', step: 'research_review', execution_id: 'r2', generation: 1 }),
  ];
  const p = projectRun(events);
  expect(p.instances).toHaveLength(4);
  expect(p.statuses.public_signal).toBe('completed');
  expect(p.statuses.review).toBe('running');
  expect(p.instances.at(-2)?.status).toBe('failed');
  expect(p.generation).toBe(1);
  expect(projectRun(events.slice(0, 4)).statuses.public_signal).toBe('running');
  expect(projectRun(JSON.parse(JSON.stringify(events)))).toEqual(p);
});
it('cancellation waits for cleanup and never completes unexecuted nodes', () => {
  const es = [event(1, { type: 'step_started', step: 'public_signal_agent' })];
  expect(
    projectRun(es, snapshot({ status: 'interrupted', settlement: 'pending' })).statuses
      .public_signal,
  ).toBe('running');
  const p = projectRun(es, snapshot({ status: 'cancelled', settlement: 'settled' }));
  expect(p.statuses.public_signal).toBe('cancelled');
  expect(p.statuses.reporting).toBe('waiting');
});
it('keeps partial planning and immutable approval distinct', () => {
  const es = [event(1, { type: 'step_completed', step: 'write_research_brief' })];
  expect(projectRun(es).statuses.planning).toBe('partial');
  expect(projectRun(es, snapshot({ status: 'awaiting_approval' })).statuses.approval).toBe(
    'paused',
  );
});
it('ends an approval wait on authoritative cancellation and deduplicates terminal instance facts', () => {
  const es = [
    event(1, { type: 'run_status', status: 'awaiting_approval' }),
    event(2, { type: 'step_started', step: 'research_review', execution_id: 'same' }),
    event(3, { type: 'step_failed', step: 'research_review', execution_id: 'same' }),
    event(4, { type: 'step_failed', step: 'research_review', execution_id: 'same' }),
  ];
  const p = projectRun(es, snapshot({ status: 'cancelled', settlement: 'settled' }));
  expect(p.statuses.approval).toBe('cancelled');
  expect(p.instances).toHaveLength(1);
});
it('pairs native tool IDs and model turns while preserving result semantics', () => {
  const meta = { type: 'sdk_event', role: 'public_signal', invocation_id: 'sdk1' };
  const spans = sdkSpans([
    event(1, { ...meta, event: 'model_started', model_turn: 1 }),
    event(2, {
      ...meta,
      event: 'model_completed',
      model_turn: 1,
      usage: { total_tokens: 12 },
      duration_ms: 5,
    }),
    event(3, { ...meta, event: 'tool', status: 'started', tool_call_id: 't1', name: 'web_search' }),
    event(4, { ...meta, event: 'tool', status: 'returned', tool_call_id: 't1' }),
    event(5, { ...meta, event: 'tool_result', status: 'failed', tool_call_id: 't1' }),
    event(6, { ...meta, event: 'model_started', model_turn: 2 }),
    event(7, { ...meta, event: 'agent_failed', status: 'exception' }),
  ]);
  expect(spans).toHaveLength(3);
  expect(spans[0].usage?.total_tokens).toBe(12);
  expect(spans[1].status).toBe('failed');
  expect(spans[2].status).toBe('interrupted');
  expect(spans[2].usage).toBeUndefined();
});
it('does not turn relay latency into tool or step execution duration', () => {
  expect(measuredDuration(event(1), event(2))).toBeUndefined();
  expect(
    measuredDuration(
      event(1, { observed_at: '2026-10-25T00:00:00Z' }),
      event(2, { observed_at: '2026-10-25T00:00:02Z' }),
    ),
  ).toBe(2000);
});
