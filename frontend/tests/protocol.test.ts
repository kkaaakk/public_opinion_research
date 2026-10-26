import { describe, expect, it } from 'vitest';
import { appendEvents, isDelivered, parseEvent, parseSnapshot, SSEDecoder } from '../src/protocol';
import { event, ID, snapshot } from './fixtures';
describe('SSE and the durable prefix', () => {
  it('handles every CRLF packet boundary, comments, multiline data and UTF-8 text', () => {
    const raw = ': keep-alive\r\n\r\nid: 1\r\ndata: {"seq":1,\r\ndata: "text":"证据"}\r\n\r\n';
    for (let i = 1; i < raw.length; i++) {
      const decoder = new SSEDecoder();
      expect([...decoder.push(raw.slice(0, i)), ...decoder.push(raw.slice(i))]).toEqual([
        { id: '1', data: '{"seq":1,\n"text":"证据"}' },
      ]);
    }
  });
  it('does not consume a torn final frame', () => {
    expect(new SSEDecoder().push('id: 1\ndata: {"seq":1}')).toEqual([]);
  });
  it('bounds a malformed unclosed event', () => {
    expect(() => new SSEDecoder().push('x'.repeat(2_000_001))).toThrow(/上限/);
  });
  it('deduplicates without skipping gaps or another run', () => {
    expect(appendEvents([event(1)], [event(1), event(2)], ID).map((e) => e.seq)).toEqual([1, 2]);
    expect(() => appendEvents([event(1)], [event(3)], ID)).toThrow(/不连续/);
    expect(() => appendEvents([], [event(1, { run_id: 'other' })], ID)).toThrow(/其他 Run/);
  });
  it('rejects unknown protocol versions and invalid native fields', () => {
    expect(() => parseEvent({ ...event(1), protocol_version: 2 })).toThrow(/版本/);
    expect(() => parseEvent({ ...event(1), seq: '1' })).toThrow();
    expect(() => parseEvent({ ...event(1), duration_ms: -1 })).toThrow();
    expect(() => parseSnapshot({ ...snapshot(), status: 'made-up' })).toThrow();
  });
  it('requires settlement, current generation delivery and the full cursor', () => {
    const done = snapshot({ status: 'failed', events_complete: true, event_cursor: 4 });
    expect(isDelivered(done, 3)).toBe(false);
    expect(isDelivered({ ...done, settlement: 'pending' }, 4)).toBe(false);
    expect(isDelivered({ ...done, retry_requested: true }, 4)).toBe(false);
    expect(isDelivered({ ...done, events_complete: false }, 4)).toBe(false);
    expect(isDelivered(done, 4)).toBe(true);
  });
});
