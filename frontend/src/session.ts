import { integer, record, string, type RunRequest } from './protocol';
export type PendingCreate = { key: string; request: RunRequest };
export type PendingRetry = {
  key: string;
  runId: string;
  state_version: number;
  generation: number;
};
type Session = { runId?: string; create?: PendingCreate; retry?: PendingRetry };
const KEY = 'por-studio-session-v2';
export function restore(): Session {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return {};
    const v = record(JSON.parse(raw));
    const s: Session = {};
    if (v.runId !== undefined) s.runId = string(v.runId);
    if (v.create) {
      const c = record(v.create);
      const r = record(c.request);
      string(r.topic);
      string(r.model);
      s.create = { key: string(c.key), request: r as RunRequest };
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
