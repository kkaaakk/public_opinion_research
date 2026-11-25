import { ApiError, delivered, type Api, type Run, type Schema } from './client';

export type FollowupTask = {
  task_id: string;
  objective: string;
  target_role: 'public_signal' | 'internal_knowledge';
  evidence_needed: string;
  reason: string;
  priority: 'high' | 'medium' | 'low';
};

export type Review = {
  research_complete: boolean;
  confirmed_findings: string[];
  unresolved_claims: string[];
  conflicts: string[];
  research_gaps: string[];
  next_tasks: FollowupTask[];
};

export type RunEvent = Schema['EventEnvelope'] & {
  observed_at?: string;
  step?: string;
  unit_id?: string;
  execution_id?: string;
  event?: string;
  role?: string;
  roles?: string[];
  round?: number;
  research_round?: number;
  status?: string;
  reason?: string;
  decision?: string;
  error_kind?: string;
  content?: string;
  review?: Review;
  clarification_version?: string;
  plan_version?: string;
  name?: string;
  tool_call_id?: string;
  invocation_id?: string;
  operation_id?: string;
  model?: string;
  model_turn?: number;
  duration_ms?: number;
  search_calls?: number;
  usage?: Schema['Usage'];
};

export class ProtocolError extends Error {}

/** Incremental SSE parsing, including CR/LF split across transport packets. */
export class SSEDecoder {
  private line = '';
  private data: string[] = [];
  private id = '';
  private sawCR = false;
  private size = 0;

  push(chunk: string): { id: string; data: string }[] {
    const frames: { id: string; data: string }[] = [];
    for (const ch of chunk) {
      if (this.sawCR && ch === '\n') {
        this.sawCR = false;
        continue;
      }
      this.sawCR = ch === '\r';
      if (ch === '\n' || ch === '\r') {
        if (!this.line) {
          if (this.data.length) frames.push({ id: this.id, data: this.data.join('\n') });
          this.data = [];
          this.id = '';
          this.size = 0;
        } else if (this.line.startsWith('data:')) {
          this.data.push(this.line.slice(5).replace(/^ /, ''));
        } else if (this.line.startsWith('id:')) {
          this.id = this.line.slice(3).replace(/^ /, '');
        }
        this.line = '';
      } else {
        this.line += ch;
        if (++this.size > 2_000_000) throw new ProtocolError('SSE 消息过大，已保留接收位置。');
      }
    }
    return frames;
  }
}

function append(existing: RunEvent[], incoming: RunEvent[], id: string) {
  const added: RunEvent[] = [];
  let cursor = existing.at(-1)?.seq ?? 0;
  for (const event of incoming) {
    if (
      event?.run_id !== id ||
      event.protocol_version !== 1 ||
      !Number.isSafeInteger(event.seq) ||
      event.seq < 1 ||
      typeof event.type !== 'string' ||
      !Number.isFinite(Date.parse(event.timestamp))
    ) {
      throw new ProtocolError('无法读取此研究的事件。');
    }
    if (event.seq <= cursor) continue;
    if (event.seq !== cursor + 1) throw new ProtocolError('事件顺序异常，已保留接收位置。');
    added.push(event);
    cursor = event.seq;
  }
  return added.length ? [...existing, ...added] : existing;
}

export type Connection = 'loading' | 'live' | 'waiting' | 'closed' | 'reconnecting' | 'error';
type Observer = {
  snapshot: (run: Run) => void;
  events: (items: RunEvent[]) => void;
  connection: (state: Connection, error?: unknown) => void;
};

function delay(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    signal.throwIfAborted();
    const onAbort = () => {
      clearTimeout(timer);
      reject(signal.reason);
    };
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

/** Observe only. Switch to snapshot polling at a delivered human pause. */
export async function observeRun(
  api: Api,
  id: string,
  history: boolean,
  events: RunEvent[],
  sink: Observer,
  signal: AbortSignal,
) {
  const cursor = () => events.at(-1)?.seq ?? 0;
  const receive = (incoming: RunEvent[]) => {
    signal.throwIfAborted();
    const next = append(events, incoming, id);
    if (next !== events) {
      events = next;
      sink.events(events);
    }
  };
  const read = async (abort: AbortSignal) => {
    const run = await api.run(id, abort, history);
    abort.throwIfAborted();
    if (run.run_id !== id) throw new ProtocolError('研究快照属于其他 Run。');
    sink.snapshot(run);
    return run;
  };
  const closed = (run: Run) => delivered(run) && cursor() >= run.event_cursor;
  const waiting = (run: Run) => run.events_paused && cursor() >= run.event_cursor;

  let failures = 0;
  sink.connection('loading');
  while (!signal.aborted) {
    try {
      const run = await read(signal);
      if (cursor() < run.event_cursor) {
        let more = true;
        while (more) {
          const after = cursor();
          const page = await api.eventPage(id, after, signal);
          receive(page.items);
          if (page.next_cursor !== cursor() || (page.has_more && cursor() <= after)) {
            throw new ProtocolError('事件分页未能继续。');
          }
          more = page.has_more;
        }
      }
      if (history && cursor() < run.event_cursor) {
        throw new ProtocolError('部分历史事件已不可读取。报告和已保留的阶段产物仍可查看。');
      }
      if (closed(run) || history) {
        sink.connection('closed');
        return;
      }
      if (waiting(run)) {
        sink.connection('waiting');
        failures = 0;
        await delay(3500, signal);
        continue;
      }

      sink.connection('live');
      const subscription = new AbortController();
      const connection = AbortSignal.any([signal, subscription.signal]);
      let pollError: unknown;
      const polling = (async () => {
        try {
          while (!connection.aborted) {
            await delay(4000, connection);
            const latest = await read(connection);
            if (closed(latest) || waiting(latest)) subscription.abort();
          }
        } catch (error) {
          if (!connection.aborted) {
            pollError = error;
            subscription.abort();
          }
        }
      })();

      const before = cursor();
      try {
        await consume(api, id, before, receive, connection);
      } catch (error) {
        if (!connection.aborted) throw error;
      } finally {
        subscription.abort();
        await polling;
      }
      if (pollError) throw pollError;
      if (cursor() > before) failures = 0;
      // EOF is only a transport observation. The next GET decides whether the
      // current generation ended, paused, or still needs a connection.
      await delay(Math.min(400 * 2 ** failures++, 5000), signal);
    } catch (error) {
      if (signal.aborted) return;
      if (
        error instanceof ProtocolError ||
        (error instanceof ApiError && error.status < 500 && error.status !== 429)
      ) {
        sink.connection('error', error);
        return;
      }
      sink.connection('reconnecting', error);
      await delay(Math.min(600 * 2 ** failures++, 10_000), signal).catch(() => undefined);
    }
  }
}

async function consume(
  api: Api,
  id: string,
  cursor: number,
  receive: (events: RunEvent[]) => void,
  signal: AbortSignal,
) {
  const response = await api.stream(id, cursor, signal);
  if (!response.headers.get('content-type')?.includes('text/event-stream') || !response.body) {
    throw new ProtocolError('服务器没有返回 SSE 流。');
  }
  const reader = response.body.getReader();
  const text = new TextDecoder();
  const decoder = new SSEDecoder();
  try {
    while (!signal.aborted) {
      const packet = await reader.read();
      signal.throwIfAborted();
      const frames = decoder.push(text.decode(packet.value, { stream: !packet.done }));
      if (frames.length) {
        receive(
          frames.map((frame) => {
            let event: RunEvent;
            try {
              event = JSON.parse(frame.data);
            } catch {
              throw new ProtocolError('SSE 消息格式无效。');
            }
            if (frame.id && frame.id !== String(event?.seq)) {
              throw new ProtocolError('SSE ID 与事件序号不一致。');
            }
            return event;
          }),
        );
      }
      if (packet.done) return;
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
