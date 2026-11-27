import type { components } from './schema';
import type { RunEvent } from './events';

export type Schema = components['schemas'];
export type Run = Schema['RunSnapshot'];
export type RunStatus = Run['status'];
export type ResearchRequest = Schema['ResearchRequest'];
export type HistoryStatus = Schema['HistoryItem']['status'];
export type RunCommand =
  | { action: 'clarification'; body: Schema['ClarificationAnswer'] }
  | { action: 'approval'; body: Schema['Approval'] }
  | { action: 'retry'; body: Schema['Retry'] }
  | { action: 'cancel' };

export const TERMINAL = new Set<RunStatus>(['completed', 'failed', 'cancelled', 'interrupted']);

export function delivered(run: Run) {
  return (
    TERMINAL.has(run.status) &&
    run.settlement !== 'pending' &&
    run.events_complete &&
    !run.retry_requested
  );
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    detail: string,
  ) {
    super(detail);
  }
}

export const runPath = (id: string) => '/api/runs/' + encodeURIComponent(id);

type Params = Record<string, string | number | undefined>;

function query(params: Params) {
  const fields = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') fields.set(key, String(value));
  }
  return fields.size ? '?' + fields : '';
}

export class Api {
  constructor(private readonly token: string) {}

  async request(path: string, init: RequestInit = {}) {
    const headers = new Headers(init.headers);
    if (this.token) headers.set('Authorization', 'Bearer ' + this.token);
    if (init.body) headers.set('Content-Type', 'application/json');
    const response = await fetch(path, { cache: 'no-store', ...init, headers });
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      throw new ApiError(
        response.status,
        typeof payload?.detail === 'string' ? payload.detail : 'HTTP ' + response.status,
      );
    }
    return response;
  }

  async json<T>(path: string, init?: RequestInit): Promise<T> {
    return (await this.request(path, init)).json();
  }

  post<T>(path: string, body?: object, key?: string, timeout = 30_000) {
    return this.json<T>(path, {
      method: 'POST',
      headers: key ? { 'Idempotency-Key': key } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(timeout),
    });
  }

  settings(signal?: AbortSignal) {
    return this.json<Schema['Settings']>('/api/settings', { signal });
  }

  status(signal?: AbortSignal) {
    return this.json<Schema['SystemStatus']>('/api/system/status', { signal });
  }

  health(signal?: AbortSignal) {
    return this.json<{ status: string }>('/api/health', { signal });
  }

  run(id: string, signal?: AbortSignal, history = false) {
    return this.json<Run>(history ? '/api/history/' + encodeURIComponent(id) : runPath(id), {
      signal,
    });
  }

  create(body: ResearchRequest, key: string) {
    return this.post<Run>('/api/runs', body, key, 120_000);
  }

  submission(key: string) {
    return this.json<Run>('/api/runs/submission', { headers: { 'Idempotency-Key': key } });
  }

  command(id: string, command: RunCommand, key?: string) {
    return this.post<Run>(
      runPath(id) + '/' + command.action,
      'body' in command ? command.body : undefined,
      key,
    );
  }

  eventPage(id: string, after: number, signal: AbortSignal) {
    return this.json<Omit<Schema['EventPage'], 'items'> & { items: RunEvent[] }>(
      runPath(id) + '/event-page' + query({ after, limit: 200 }),
      { signal },
    );
  }

  stream(id: string, cursor: number, signal: AbortSignal) {
    return this.request(runPath(id) + '/events', {
      headers: { 'Last-Event-ID': String(cursor), Accept: 'text/event-stream' },
      signal,
    });
  }

  execution(id: string, params: Params, signal?: AbortSignal) {
    return this.json<Schema['ExecutionPage']>(runPath(id) + '/execution' + query(params), {
      signal,
    });
  }

  sessions(offset: number, limit: number, signal?: AbortSignal) {
    return this.json<Schema['OffsetPage_SessionItem_']>(
      '/api/sessions' + query({ offset, limit }),
      { signal },
    );
  }

  session(id: string, offset: number, signal?: AbortSignal) {
    return this.json<Schema['SessionDetail']>(
      '/api/sessions/' + encodeURIComponent(id) + query({ offset }),
      { signal },
    );
  }

  history(offset: number, q: string, status: HistoryStatus | '', signal?: AbortSignal) {
    return this.json<Schema['OffsetPage_HistoryItem_']>(
      '/api/history' + query({ offset, q, status, limit: 20 }),
      { signal },
    );
  }

  knowledge(signal?: AbortSignal) {
    return this.json<Schema['KnowledgeStatus']>('/api/knowledge', { signal });
  }

  sources(offset: number, q: string, signal?: AbortSignal) {
    return this.json<Schema['SourcePage']>('/api/knowledge/sources' + query({ offset, q }), {
      signal,
    });
  }
}
