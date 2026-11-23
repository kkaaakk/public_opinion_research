import { boolean, integer, record, string, type RunRequest } from './protocol';
export type PendingCreate = {
  key: string;
  request: RunRequest;
  /** Set when a persisted body still carried settings the API no longer takes. */
  upgraded?: boolean;
};
export type PendingRetry = {
  key: string;
  runId: string;
  state_version: number;
  generation: number;
};
type Session = { runId?: string; create?: PendingCreate; retry?: PendingRetry };
const KEY = 'por-studio-session-v2';
const ACCEPTED_FIELDS = new Set(['topic', 'org_context', 'rag_enabled']);
/** Keep only the fields the create endpoint still accepts and validates. */
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
      const stored = record(c.request);
      // Rebuilds the body from accepted fields only, so a request persisted by
      // an older build cannot be retried with removed technical settings. Such
      // a body is no longer the request that key may already own, so it stays
      // flagged across reloads instead of passing for the original one.
      const upgraded =
        c.upgraded === true ||
        Object.keys(stored).some((name) => !ACCEPTED_FIELDS.has(name));
      s.create = {
        key: string(c.key),
        request: createRequest(stored),
        ...(upgraded ? { upgraded: true } : {}),
      };
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
