import { expect, it } from 'vitest';
import { parseExecution, parseHistory } from '../src/readModels';
import { parseSnapshot } from '../src/protocol';
import { snapshot } from './fixtures';
it('rejects incorrectly typed optional snapshot fields before render', () => {
  expect(() => parseSnapshot({ ...snapshot(), request: { topic: { secret: true } } })).toThrow();
  expect(() => parseSnapshot({ ...snapshot(), approval_required: 'false' })).toThrow();
  expect(() => parseSnapshot({ ...snapshot(), error: { kind: NaN } })).toThrow();
});
it('validates native step identities and nullable actual times', () => {
  const data = {
    run_id: 'run',
    workflow_id: 'run',
    workflows: [],
    steps: [
      {
        function_id: 1,
        function_name: 'por.role',
        child_workflow_id: null,
        started_at_epoch_ms: 10,
        completed_at_epoch_ms: null,
      },
    ],
    workflows_has_more: false,
    next_workflow_offset: 0,
    steps_has_more: false,
    next_offset: 1,
  };
  expect(parseExecution(data).steps[0].completed_at_epoch_ms).toBeNull();
  expect(() => parseExecution({ ...data, steps: [{ function_id: '1' }] })).toThrow();
});
it('validates Run IDs and native history pagination', () => {
  const item = {
    run_id: 'research-run',
    topic: 'Brand recall',
    status: 'completed',
    started_at: '2026-10-25T01:00:00Z',
    completed_at: '2026-10-25T01:01:00Z',
  };
  const page = { items: [item], has_more: true, next_offset: 10 };
  expect(parseHistory(page)).toEqual(page);
  expect(() => parseHistory({ ...page, items: [{ ...item, run_id: null }] })).toThrow();
  expect(() => parseHistory({ ...page, next_offset: '10' })).toThrow();
});
