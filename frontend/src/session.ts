import { boolean, integer, record, string, type RunRequest } from './protocol';
export type PendingCreate = {
  key: string;
  request: RunRequest;
};
export type PendingRetry = {
  key: string;
  runId: string;
  state_version: number;
  generation: number;
};
type Session = { runId?: string; create?: PendingCreate; retry?: PendingRetry };
const KEY = 'por-studio-session-v2';
/** Read one create body from storage or the dialog as the endpoint's three fields. */
export function createRequest(value: unknown): RunRequest {
  const r = record(value);
  return {
    topic: string(r.topic),
    org_context: r.org_context === undefined ? '' : string(r.org_context),
    rag_enabled: r.rag_enabled === undefined ? false : boolean(r.rag_enabled),
  };
}
export function restore(): Session {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return {};
    const v = record(JSON.parse(raw));
    const s: Session = {};
    if (v.runId !== undefined) s.runId = string(v.runId);
    if (v.create) {
      const c = record(v.create);
      s.create = { key: string(c.key), request: createRequest(c.request) };
    }
    if (v.retry) {
      const r = record(v.retry);
      s.retry = {
        key: string(r.key),
        runId: string(r.runId),
        state_version: integer(r.state_version),
        generation: integer(r.generation),
      };
    }
    return s;
  } catch {
    return {};
  }
}
export function persist(session: Session): void {
  // A failed write must prevent the first mutation, preserving ambiguous-request safety.
  sessionStorage.setItem(KEY, JSON.stringify(session));
}
