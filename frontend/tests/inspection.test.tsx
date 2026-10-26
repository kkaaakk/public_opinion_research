import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { eventsForSDKFocus, invocationsForInstance, preferredPhase } from '../src/inspection';
import { projectRun } from '../src/projection';
import { Inspector } from '../src/components/Inspector';
import { SDKTrace } from '../src/components/SDKTrace';
import { event, snapshot } from './fixtures';

function invocationEvents(execution: string, invocation: string, generation = 0, offset = 0) {
  const meta = {
    type: 'sdk_event',
    execution_id: execution,
    invocation_id: invocation,
    role: 'public_signal',
    generation,
  };
  return [
    event(offset + 1, { ...meta, event: 'model_started', model_turn: 1 }),
    event(offset + 2, {
      ...meta,
      event: 'model_completed',
      model_turn: 1,
      usage: { total_tokens: 14 },
    }),
    event(offset + 3, {
      ...meta,
      event: 'tool',
      status: 'started',
      tool_call_id: 'same-call',
      name: 'web_search',
    }),
    event(offset + 4, {
      ...meta,
      event: 'tool_result',
      status: 'succeeded',
      tool_call_id: 'same-call',
      name: 'web_search',
    }),
  ];
}
it('prefers the active real instance, then the latest ended business node', () => {
  const events = [
    event(1, { type: 'step_started', step: 'public_signal_agent', execution_id: 'a' }),
    event(2, { type: 'step_started', step: 'internal_knowledge_agent', execution_id: 'b' }),
  ];
  expect(preferredPhase(projectRun(events), events)).toBe('internal_knowledge');
  events.push(
    event(3, { type: 'step_completed', step: 'internal_knowledge_agent', execution_id: 'b' }),
  );
  expect(preferredPhase(projectRun(events), events)).toBe('public_signal');
  events.push(
    event(4, { type: 'step_completed', step: 'public_signal_agent', execution_id: 'a' }),
    event(5, { type: 'step_completed', step: 'compile_final_report', execution_id: 'report' }),
  );
  expect(preferredPhase(projectRun(events), events)).toBe('reporting');
});
it('selects an actual approval wait and keeps no-data selection empty', () => {
  expect(preferredPhase(projectRun([], snapshot({ status: 'awaiting_approval' })), [])).toBe(
    'approval',
  );
  expect(preferredPhase(projectRun([]), [])).toBeNull();
});
it('links followup and retry invocations by exact execution and generation, never by role alone', () => {
  const records = [
    event(1, {
      type: 'step_started',
      step: 'public_signal_agent',
      execution_id: 'first',
      generation: 0,
    }),
    ...invocationEvents('first', 'sdk', 0, 1),
    event(6, { type: 'step_completed', step: 'public_signal_agent', execution_id: 'first' }),
    event(7, { type: 'retry_started', generation: 1 }),
    event(8, {
      type: 'step_started',
      step: 'public_signal_agent',
      execution_id: 'second',
      generation: 1,
      research_round: 2,
    }),
    ...invocationEvents('second', 'sdk', 1, 8),
  ];
  const p = projectRun(records);
  const first = invocationsForInstance(records, p.instances[0]);
  const second = invocationsForInstance(records, p.instances[1]);
  expect(first).toHaveLength(1);
  expect(second).toHaveLength(1);
  expect(first[0].focus.generation).toBe(0);
  expect(second[0].focus.generation).toBe(1);
  expect(first[0].models).toBe(1);
  expect(first[0].tools).toBe(1);
  expect(
    eventsForSDKFocus(records, second[0].focus).every(
      (e) => e.execution_id === 'second' && e.generation === 1,
    ),
  ).toBe(true);
  expect(
    invocationsForInstance(records, {
      ...p.instances[0],
      id: 'legacy',
      start: undefined,
      end: undefined,
    }),
  ).toEqual([]);
});
it('opens the observed model or tool from a business instance and filters actual SDK rows', async () => {
  const events = [
    event(1, { type: 'step_started', step: 'public_signal_agent', execution_id: 'one' }),
    ...invocationEvents('one', 'sdk-one', 0, 1),
  ];
  const p = projectRun(events);
  const onSDK = vi.fn();
  const user = userEvent.setup();
  const { unmount } = render(
    <Inspector
      selected="public_signal"
      event={null}
      events={events}
      projection={p}
      onEvent={() => undefined}
      onSDK={onSDK}
    />,
  );
  await user.click(screen.getByRole('button', { name: '工具 1' }));
  const focus = onSDK.mock.calls[0][0];
  expect(focus).toMatchObject({
    invocationId: 'sdk-one',
    executionId: 'one',
    generation: 0,
    kind: 'tool',
  });
  unmount();
  render(
    <SDKTrace
      events={[...events, ...invocationEvents('other', 'other-sdk', 1, 8)]}
      focus={focus}
      onEvent={() => undefined}
      onClearFocus={() => undefined}
    />,
  );
  expect(screen.getByLabelText('追踪 Invocation')).toHaveValue('sdk-one');
  expect(screen.getByLabelText('追踪 Invocation')).toBeDisabled();
  expect(screen.getByRole('button', { name: /web_search/ })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /模型调用/ })).toBeNull();
});
it('never uses future invocation records in a replay prefix', () => {
  const records = invocationEvents('one', 'sdk-one');
  const focus = {
    invocationId: 'sdk-one',
    executionId: 'one',
    generation: 0,
    kind: 'all' as const,
  };
  expect(eventsForSDKFocus([], focus)).toEqual([]);
  expect(eventsForSDKFocus(records.slice(0, 1), focus)).toHaveLength(1);
});
it('keeps an uncertain external effect visible alongside a failed business result', () => {
  const m = {
    type: 'sdk_event',
    execution_id: 'one',
    invocation_id: 'sdk',
    role: 'public_signal',
    tool_call_id: 'call',
    name: 'write',
  };
  render(
    <SDKTrace
      events={[
        event(1, { ...m, event: 'tool', status: 'started' }),
        event(2, { ...m, event: 'tool', status: 'uncertain' }),
        event(3, { ...m, event: 'tool_result', status: 'failed' }),
      ]}
      onEvent={() => undefined}
    />,
  );
  expect(screen.getByRole('button', { name: /write/ })).toHaveTextContent('失败');
  expect(screen.getByRole('button', { name: /write/ })).toHaveTextContent('效果回执 · 效果未知');
});
