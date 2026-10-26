import type { RunEvent, RunSnapshot } from '../src/protocol';
export const ID = 'a'.repeat(32);
export function event(seq: number, data: Partial<RunEvent> = {}): RunEvent {
  return {
    protocol_version: 1,
    run_id: ID,
    seq,
    timestamp: `2026-10-25T01:00:${String(seq % 60).padStart(2, '0')}Z`,
    type: 'status',
    generation: 0,
    ...data,
  };
}
export function snapshot(data: Partial<RunSnapshot> = {}): RunSnapshot {
  return {
    run_id: ID,
    status: 'running',
    native_status: 'PENDING',
    event_cursor: 0,
    state_version: 1,
    generation: 0,
    settlement: 'settled',
    events_complete: false,
    events_paused: false,
    retry_safe: false,
    retry_requested: false,
    completed_steps: [],
    usage: {},
    error: {},
    report: '',
    role_reports: [],
    plan: null,
    ...data,
  };
}
export function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}
export function stream(value: string) {
  return new Response(value, { headers: { 'Content-Type': 'text/event-stream' } });
}
export function frame(e: RunEvent) {
  return `id: ${e.seq}\ndata: ${JSON.stringify(e)}\n\n`;
}
