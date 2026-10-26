import { expect, it } from 'vitest';
import { parseArchive, parseEvidence, parseExecution } from '../src/readModels';
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
it('validates evidence properties and archive report data', () => {
  expect(() =>
    parseEvidence({
      run_id: 'run',
      available: true,
      nodes: [{ node_id: 'n', node_type: 'Evidence', properties: 'invalid' }],
      edges: [],
      has_more: false,
      edges_has_more: false,
    }),
  ).toThrow();
  expect(() =>
    parseArchive({
      id: '1',
      run_id: 'old-legacy-id',
      topic: 'old',
      status: 'completed',
      completed_at: '2026-10-25',
      report: [],
    }),
  ).toThrow();
  expect(
    parseArchive({
      id: '1',
      run_id: 'old-legacy-id',
      topic: 'old',
      status: 'completed',
      completed_at: '2026-10-25',
      report: 'old report',
    }).role_reports,
  ).toEqual([]);
});
