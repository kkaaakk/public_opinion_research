import {
  array,
  boolean,
  integer,
  parseEvent,
  parseSnapshot,
  ProtocolError,
  record,
  string,
  type EventPage,
  type EvidencePage,
  type ExecutionPage,
  type HistoryPage,
  type HistoryRecord,
  type RunRequest,
  type RunSnapshot,
} from './protocol';
import { parseArchive, parseEvidence, parseExecution, parseHistory } from './readModels';

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
export class StudioApi {
  constructor(
    private token: () => string,
    private transport: typeof fetch = (...args) => fetch(...args),
  ) {}
  async request(path: string, init: RequestInit = {}): Promise<Response> {
    const headers = new Headers(init.headers);
    const token = this.token().trim();
    if (token) headers.set('Authorization', `Bearer ${token}`);
    if (init.body) headers.set('Content-Type', 'application/json');
    const response = await this.transport(path, {
      ...init,
      headers,
      cache: 'no-store',
      credentials: 'same-origin',
    });
    if (!response.ok) {
      const data: unknown = await response.json().catch(() => null);
      const detail =
        data && typeof data === 'object' && 'detail' in data && typeof data.detail === 'string'
          ? data.detail
          : '请求失败';
      throw new ApiError(`${detail} (HTTP ${response.status})`, response.status);
    }
    return response;
  }
  async json(path: string, init: RequestInit = {}): Promise<unknown> {
    return (await this.request(path, init)).json();
  }
  async read(id: string, signal?: AbortSignal): Promise<RunSnapshot> {
    const result = parseSnapshot(
      await this.json(`/api/runs/${encodeURIComponent(id)}`, { signal }),
    );
    if (result.run_id !== id) throw new ProtocolError('快照属于其他 Run');
    return result;
  }
  async create(request: RunRequest, key: string): Promise<RunSnapshot> {
    return parseSnapshot(
      await this.json('/api/runs', {
        method: 'POST',
        headers: { 'Idempotency-Key': key },
        body: JSON.stringify(request),
      }),
    );
  }
  async command(
    id: string,
    action: 'cancel' | 'approval' | 'retry' | 'clarification',
    body?: object,
    key?: string,
  ): Promise<RunSnapshot> {
    return parseSnapshot(
      await this.json(`/api/runs/${encodeURIComponent(id)}/${action}`, {
        method: 'POST',
        headers: key ? { 'Idempotency-Key': key } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      }),
    );
  }
  async page(id: string, after: number, signal?: AbortSignal): Promise<EventPage> {
    const v = record(
      await this.json(`/api/runs/${encodeURIComponent(id)}/event-page?after=${after}&limit=200`, {
        signal,
      }),
    );
    const page = {
      items: array(v.items).map(parseEvent),
      next_cursor: integer(v.next_cursor),
      has_more: boolean(v.has_more),
    };
    if (
      page.next_cursor !== (page.items.at(-1)?.seq ?? after) ||
      (page.has_more && !page.items.length) ||
      (page.items.length > 0 && page.next_cursor <= after)
    )
      throw new ProtocolError('事件分页没有向前推进');
    return page;
  }
  stream(id: string, cursor: number, signal: AbortSignal): Promise<Response> {
    return this.request(`/api/runs/${encodeURIComponent(id)}/events`, {
      headers: { 'Last-Event-ID': String(cursor), Accept: 'text/event-stream' },
      signal,
    });
  }
  async execution(
    id: string,
    params: URLSearchParams,
    signal?: AbortSignal,
  ): Promise<ExecutionPage> {
    const v = record(
      await this.json(`/api/runs/${encodeURIComponent(id)}/execution?${params}`, { signal }),
    );
    if (string(v.run_id) !== id) throw new ProtocolError('日志属于其他 Run');
    return parseExecution(v);
  }
  async evidence(id: string, params: URLSearchParams, signal?: AbortSignal): Promise<EvidencePage> {
    const v = record(
      await this.json(`/api/runs/${encodeURIComponent(id)}/evidence?${params}`, { signal }),
    );
    if (string(v.run_id) !== id) throw new ProtocolError('证据属于其他 Run');
    return parseEvidence(v);
  }
  async history(query: string, offset: number, signal?: AbortSignal): Promise<HistoryPage> {
    const v = record(
      await this.json(`/api/history?q=${encodeURIComponent(query)}&offset=${offset}&limit=10`, {
        signal,
      }),
    );
    return parseHistory(v);
  }
  async archive(id: string, signal?: AbortSignal): Promise<HistoryRecord> {
    const v = record(await this.json(`/api/history/${encodeURIComponent(id)}`, { signal }));
    return parseArchive(v);
  }
}
