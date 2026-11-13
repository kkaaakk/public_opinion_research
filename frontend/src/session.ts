import { integer, record, string, type BriefRevisionRequest, type RunRequest } from './protocol';
export type PendingCreate = { key: string; request: RunRequest };
export type PendingRetry = {
  key: string;
  runId: string;
  state_version: number;
  generation: number;
};
export type PendingBriefRevision = { key: string; request: BriefRevisionRequest };
type Session = {
  runId?: string;
  create?: PendingCreate;
  retry?: PendingRetry;
  briefRevisions?: Record<string, PendingBriefRevision>;
};
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
    if (v.briefRevisions) {
      s.briefRevisions = {};
      for (const [id, value] of Object.entries(record(v.briefRevisions))) {
        if (!/^[a-f0-9]{32}$/.test(id)) continue;
        const pending = record(value);
        const request = record(pending.request);
        const key = string(pending.key);
        const version = string(request.version);
        const feedback = string(request.feedback);
        if (
          /^[a-f0-9]{64}$/.test(version) &&
          /^[A-Za-z0-9_.:-]{1,128}$/.test(key) &&
          request.decision === 'revise' &&
          feedback.trim() &&
          feedback.length <= 4000
        ) {
          s.briefRevisions[id] = { key, request: { version, decision: 'revise', feedback } };
        }
      }
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
