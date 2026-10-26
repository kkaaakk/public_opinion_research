/** Validate every REST read at the boundary; components receive concrete data. */
import {
  array,
  boolean,
  integer,
  jsonObject,
  optionalString,
  parseRoleReports,
  parseUsage,
  record,
  string,
  type EvidencePage,
  type ExecutionPage,
  type HistoryItem,
  type HistoryPage,
  type HistoryRecord,
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
export function parseEvidence(v: unknown): EvidencePage {
  const r = record(v);
  return {
    run_id: string(r.run_id),
    available: boolean(r.available),
    reason: optionalString(r.reason),
    nodes: array(r.nodes).map((item) => {
      const n = record(item);
      return {
        node_id: string(n.node_id),
        node_type: string(n.node_type),
        role: optionalString(n.role),
        task_id: optionalString(n.task_id),
        research_round: n.research_round == null ? undefined : integer(n.research_round),
        properties: jsonObject(n.properties),
      };
    }),
    edges: array(r.edges).map((item) => {
      const e = record(item);
      return {
        edge_id: string(e.edge_id),
        relation_type: string(e.relation_type),
        source_id: string(e.source_id),
        target_id: string(e.target_id),
      };
    }),
    has_more: boolean(r.has_more),
    next_cursor: optionalString(r.next_cursor),
    edges_has_more: boolean(r.edges_has_more),
    next_edge_cursor: optionalString(r.next_edge_cursor),
  };
}
function historyItem(v: unknown): HistoryItem {
  const r = record(v);
  return {
    id: string(r.id),
    run_id: string(r.run_id),
    topic: string(r.topic),
    status: string(r.status),
    completed_at: string(r.completed_at),
  };
}
export function parseHistory(v: unknown): HistoryPage {
  const r = record(v);
  return { items: array(r.items).map(historyItem), total: integer(r.total) };
}
export function parseArchive(v: unknown): HistoryRecord {
  const r = record(v);
  return {
    ...historyItem(r),
    report: string(r.report),
    role_reports: parseRoleReports(r.role_reports ?? []),
    usage: parseUsage(r.usage ?? {}),
  };
}
