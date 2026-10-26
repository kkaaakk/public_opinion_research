import { expect, it, vi } from 'vitest';
import { StudioApi } from '../src/api';
import { RunObserver, type Connection } from '../src/observer';
import type { RunEvent } from '../src/protocol';
import { event, frame, ID, json, snapshot, stream } from './fixtures';

it('cold replays all pages beyond 2000 events and starts every reload at zero', async () => {
  const events = Array.from({ length: 2401 }, (_, i) => event(i + 1));
  const cursors: number[] = [];
  const transport = vi.fn<typeof fetch>(async (input) => {
    const url = String(input);
    if (!url.includes('event-page'))
      return json(
        snapshot({ event_cursor: events.length, events_complete: true, status: 'completed' }),
      );
    const after = Number(new URL(url, 'http://local').searchParams.get('after'));
    cursors.push(after);
    const page = events.slice(after, after + 200);
    return json({
      items: page,
      next_cursor: page.at(-1)?.seq ?? after,
      has_more: after + page.length < events.length,
    });
  });
  const states: Connection[] = [];
  let delivered: RunEvent[] = [];
  const api = new StudioApi(() => 'test-token', transport);
  for (let i = 0; i < 2; i++)
    await new RunObserver(api, {
      events: (e) => {
        delivered = e;
      },
      snapshot: () => undefined,
      connection: (c) => states.push(c),
    }).observe(ID, new AbortController().signal);
  expect(delivered).toHaveLength(2401);
  expect(cursors.filter((c) => c === 0)).toHaveLength(2);
  expect(states.at(-1)).toBe('complete');
  expect(transport.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(
    true,
  );
  expect(new Headers(transport.mock.calls[0][1]?.headers).get('Authorization')).toBe(
    'Bearer test-token',
  );
});
it('reconnects at the last full cursor, deduplicates, and waits through native terminal cleanup', async () => {
  let streams = 0;
  let cleanupReads = 0;
  const cursors: string[] = [];
  const statuses: string[] = [];
  let received: RunEvent[] = [];
  const transport = vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input);
    if (url.endsWith('/events')) {
      cursors.push(new Headers(init?.headers).get('Last-Event-ID')!);
      streams++;
      return stream(
        streams === 1
          ? frame(event(1)) + 'id: 2\ndata: {"seq":'
          : streams === 2
            ? frame(event(1)) + frame(event(2, { type: 'done' }))
            : ': keep-alive\n\n',
      );
    }
    if (streams >= 2 && ++cleanupReads < 3)
      return json(snapshot({ event_cursor: 2, status: 'interrupted', settlement: 'pending' }));
    return json(
      snapshot(
        streams >= 2
          ? { event_cursor: 2, status: 'failed', events_complete: true }
          : { event_cursor: streams },
      ),
    );
  });
  await new RunObserver(
    new StudioApi(() => '', transport),
    {
      events: (e) => {
        received = e;
      },
      snapshot: (r) => statuses.push(`${r.status}/${r.settlement}`),
      connection: () => undefined,
    },
    2,
  ).observe(ID, new AbortController().signal);
  expect(cursors.slice(0, 2)).toEqual(['0', '1']);
  expect(received.map((e) => e.seq)).toEqual([1, 2]);
  expect(statuses).toContain('interrupted/pending');
  expect(statuses.at(-1)).toBe('failed/settled');
  expect(transport.mock.calls.every(([, init]) => !init?.method)).toBe(true);
});
it('stops on a gap, wrong run, malformed JSON, or mismatched frame id without advancing', async () => {
  for (const raw of [
    frame(event(2)),
    frame(event(1, { run_id: 'other' })),
    'id: 1\ndata: invalid\n\n',
    'id: 2\ndata: ' + JSON.stringify(event(1)) + '\n\n',
  ]) {
    const states: Connection[] = [];
    let received: RunEvent[] = [];
    const transport = vi.fn<typeof fetch>(async (input) =>
      String(input).endsWith('/events') ? stream(raw) : json(snapshot()),
    );
    await new RunObserver(
      new StudioApi(() => '', transport),
      {
        events: (e) => {
          received = e;
        },
        snapshot: () => undefined,
        connection: (s) => states.push(s),
      },
      2,
    ).observe(ID, new AbortController().signal);
    expect(received).toEqual([]);
    expect(states.at(-1)).toBe('error');
  }
});
it('does not retry authorization failures or treat them as completion', async () => {
  const transport = vi.fn<typeof fetch>(async () => json({ detail: 'Unauthorized' }, 401));
  const states: Connection[] = [];
  await new RunObserver(new StudioApi(() => '', transport), {
    events: () => undefined,
    snapshot: () => undefined,
    connection: (s) => states.push(s),
  }).observe(ID, new AbortController().signal);
  expect(transport).toHaveBeenCalledTimes(1);
  expect(states).toEqual(['loading', 'error']);
});
it('backs off transient read errors and never creates a run', async () => {
  let reads = 0;
  const transport = vi.fn<typeof fetch>(async () =>
    ++reads === 1
      ? json({ detail: 'offline' }, 503)
      : json(snapshot({ status: 'cancelled', events_complete: true })),
  );
  const states: Connection[] = [];
  await new RunObserver(
    new StudioApi(() => '', transport),
    { events: () => undefined, snapshot: () => undefined, connection: (s) => states.push(s) },
    1,
  ).observe(ID, new AbortController().signal);
  expect(states).toContain('reconnecting');
  expect(states.at(-1)).toBe('complete');
});
it('aborting a stale subscription prevents late data from reaching the new run', async () => {
  const controller = new AbortController();
  const sink = { events: vi.fn(), snapshot: vi.fn(), connection: vi.fn() };
  const transport = vi.fn<typeof fetch>(async () => {
    controller.abort();
    return json(snapshot());
  });
  await new RunObserver(new StudioApi(() => '', transport), sink).observe(ID, controller.signal);
  expect(sink.snapshot).not.toHaveBeenCalled();
  expect(sink.events).not.toHaveBeenCalled();
});
it('observes archive compensation after terminal settlement without replaying research', async () => {
  let reads = 0;
  const states: Connection[] = [];
  let delivered: RunEvent[] = [];
  const transport = vi.fn<typeof fetch>(async (input) => {
    if (String(input).endsWith('/events'))
      return stream(frame(event(1, { type: 'history_saved' })));
    if (String(input).includes('event-page'))
      return json({
        items: [event(1, { type: 'history_saved' })],
        next_cursor: 1,
        has_more: false,
      });
    const saved = ++reads >= 4;
    return json(
      snapshot({
        status: 'completed',
        events_complete: true,
        archive_status: saved ? 'saved' : 'pending',
        event_cursor: saved ? 1 : 0,
      }),
    );
  });
  await new RunObserver(
    new StudioApi(() => '', transport),
    {
      events: (e) => {
        delivered = e;
      },
      snapshot: () => undefined,
      connection: (s) => states.push(s),
    },
    1,
  ).observe(ID, new AbortController().signal);
  expect(states).toContain('archiving');
  expect(states.at(-1)).toBe('complete');
  expect(delivered).toHaveLength(1);
  expect(transport.mock.calls.every(([, init]) => !init?.method)).toBe(true);
});
