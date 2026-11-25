/** Validate every REST read at the boundary; components receive concrete data. */
import {
  array,
  boolean,
  integer,
  record,
  string,
  type ExecutionPage,
  type HistoryPage,
} from './protocol';
const nullableString = (v: unknown): string | null => (v == null ? null : string(v));
const nullableNumber = (v: unknown): number | null => (v == null ? null : integer(v));
export function parseExecution(v: unknown): ExecutionPage {
  const r = record(v);
  return {
    run_id: string(r.run_id),
    workflow_id: string(r.workflow_id),
    workflows: array(r.workflows).map((item) => {
      const w = record(item);
      return {
        workflow_id: string(w.workflow_id),
        name: string(w.name),
        status: string(w.status),
        queue_name: nullableString(w.queue_name),
        app_version: nullableString(w.app_version),
        parent_workflow_id: nullableString(w.parent_workflow_id),
        created_at: nullableNumber(w.created_at),
        updated_at: nullableNumber(w.updated_at),
        completed_at: nullableNumber(w.completed_at),
        dequeued_at: nullableNumber(w.dequeued_at),
      };
    }),
    steps: array(r.steps).map((item) => {
      const s = record(item);
      return {
        function_id: integer(s.function_id),
        function_name: string(s.function_name),
        child_workflow_id: nullableString(s.child_workflow_id),
        started_at_epoch_ms: nullableNumber(s.started_at_epoch_ms),
        completed_at_epoch_ms: nullableNumber(s.completed_at_epoch_ms),
      };
    }),
    workflows_has_more: boolean(r.workflows_has_more),
    next_workflow_offset: integer(r.next_workflow_offset),
    steps_has_more: boolean(r.steps_has_more),
    next_offset: integer(r.next_offset),
  };
}
export function parseHistory(v: unknown): HistoryPage {
  const r = record(v);
  return {
    items: array(r.items).map((item) => {
      const run = record(item);
      return {
        run_id: string(run.run_id),
        topic: string(run.topic),
        status: string(run.status),
        started_at: string(run.started_at),
        completed_at: string(run.completed_at),
      };
    }),
    next_offset: integer(r.next_offset),
    has_more: boolean(r.has_more),
  };
}
