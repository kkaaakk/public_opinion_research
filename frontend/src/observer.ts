import { ApiError, StudioApi } from './api';
import {
  appendEvents,
  isDelivered,
  parseEvent,
  ProtocolError,
  SSEDecoder,
  type RunEvent,
  type RunSnapshot,
} from './protocol';

export type Connection =
  'loading' | 'live' | 'reconnecting' | 'paused' | 'archiving' | 'complete' | 'error';
type Sink = {
  events: (events: RunEvent[]) => void;
  snapshot: (run: RunSnapshot) => void;
  connection: (state: Connection, error?: string) => void;
};
export function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason);
      return;
    }
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

/** A subscriber only. All POST commands are deliberately outside this controller. */
export class RunObserver {
  private events: RunEvent[] = [];
  constructor(
    private api: StudioApi,
    private sink: Sink,
    private interval = 1500,
  ) {}
  private cursor() {
    return this.events.at(-1)?.seq ?? 0;
  }
  private append(incoming: RunEvent[], id: string) {
    const next = appendEvents(this.events, incoming, id);
    if (next.length !== this.events.length) {
      this.events = next;
      this.sink.events(next);
    }
  }
  async observe(id: string, signal: AbortSignal): Promise<void> {
    let failures = 0;
    this.sink.connection('loading');
    while (!signal.aborted) {
      try {
        let snapshot = await this.api.read(id, signal);
        if (signal.aborted) return;
        this.sink.snapshot(snapshot);
        // Fresh reload always reads from zero. No cursor is persisted without its prefix.
        let more = this.cursor() < snapshot.event_cursor;
        while (more && !signal.aborted) {
          const page = await this.api.page(id, this.cursor(), signal);
          if (signal.aborted) return;
          this.append(page.items, id);
          more = page.has_more;
        }
        snapshot = await this.api.read(id, signal);
        if (signal.aborted) return;
        this.sink.snapshot(snapshot);
        if (isDelivered(snapshot, this.cursor())) {
          if (snapshot.archive_status === 'pending') {
            this.sink.connection('archiving');
            await delay(this.interval * 2, signal);
            continue;
          }
          this.sink.connection('complete');
          return;
        }
        if (
          ['awaiting_approval', 'awaiting_clarification'].includes(snapshot.status) &&
          snapshot.events_paused &&
          this.cursor() >= snapshot.event_cursor
        ) {
          this.sink.connection('paused');
          await delay(this.interval, signal);
          continue;
        }
        this.sink.connection('live');
        const connection = new AbortController();
        const abort = () => connection.abort(signal.reason);
        signal.addEventListener('abort', abort, { once: true });
        let pollError: unknown;
        const poll = (async () => {
          try {
            while (!connection.signal.aborted) {
              await delay(this.interval, connection.signal);
              const run = await this.api.read(id, connection.signal);
              if (connection.signal.aborted) return;
              this.sink.snapshot(run);
              if (
                isDelivered(run, this.cursor()) ||
                (['awaiting_approval', 'awaiting_clarification'].includes(run.status) &&
                  run.events_paused &&
                  this.cursor() >= run.event_cursor)
              )
                connection.abort();
            }
          } catch (error) {
            if (!connection.signal.aborted) {
              pollError = error;
              connection.abort();
            }
          }
        })();
        try {
          await this.consume(id, connection.signal);
        } catch (error) {
          if (!connection.signal.aborted) throw error;
        } finally {
          connection.abort();
          signal.removeEventListener('abort', abort);
          await poll;
        }
        if (pollError) throw pollError;
        if (!signal.aborted) {
          // EOF can be a network failure; avoid a busy reconnect loop.
          this.sink.connection('reconnecting');
          await delay(Math.min(this.interval, 1000), signal);
        }
        failures = 0;
      } catch (error) {
        if (signal.aborted) return;
        const message = error instanceof Error ? error.message : '订阅中断';
        if (
          error instanceof ProtocolError ||
          (error instanceof ApiError && error.status < 500 && error.status !== 429)
        ) {
          this.sink.connection('error', message);
          return;
        }
        this.sink.connection('reconnecting', message);
        try {
          await delay(Math.min(500 * 2 ** failures++, 10000), signal);
        } catch {
          return;
        }
      }
    }
  }
  private async consume(id: string, signal: AbortSignal) {
    const response = await this.api.stream(id, this.cursor(), signal);
    if (!response.headers.get('content-type')?.includes('text/event-stream') || !response.body)
      throw new ProtocolError('服务器没有返回 SSE 流');
    const reader = response.body.getReader();
    const text = new TextDecoder();
    const decoder = new SSEDecoder();
    try {
      while (!signal.aborted) {
        const packet = await reader.read();
        if (signal.aborted) return;
        const events = decoder
          .push(text.decode(packet.value, { stream: !packet.done }))
          .map((frame) => {
            let json: unknown;
            try {
              json = JSON.parse(frame.data);
            } catch {
              throw new ProtocolError('SSE JSON 无效；保留已确认游标');
            }
            const event = parseEvent(json);
            if (frame.id && frame.id !== String(event.seq))
              throw new ProtocolError('SSE id 与事件序号不一致');
            return event;
          });
        this.append(events, id);
        if (packet.done) return; // Partial last frame is discarded, then replayed from the prior cursor.
      }
    } finally {
      await reader.cancel().catch(() => undefined);
      reader.releaseLock();
    }
  }
}
