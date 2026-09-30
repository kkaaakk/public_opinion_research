import { describe, expect, it } from 'vitest'
import { emptyTrajectory, reduceTrajectory, trajectorySnapshot } from '../src/trajectory/adapter/reducer.ts'
import type { TrajectoryEvent } from '../src/trajectory/adapter/reducer.ts'
import { deriveTrajectoryLayout } from '../src/trajectory/upstream/src/client/layout.ts'
import { deriveTrajectoryTimeline } from '../src/trajectory/upstream/src/client/timeline.ts'
import { TrajectorySearchIndex } from '../src/trajectory/upstream/src/client/trajectory-search-index.ts'
import { zh } from '../src/trajectory/upstream/src/client/locales.ts'

const t = (key: string, values?: Record<string, string | number>) => {
  let result: string = (zh as Record<string, string>)[key] ?? key
  for (const [name, value] of Object.entries(values ?? {})) result = result.replaceAll(`{${name}}`, String(value))
  return result
}
let seq = 0
function event(kind: string, run_id: string, extra: Partial<TrajectoryEvent> = {}): TrajectoryEvent {
  seq += 1
  return { seq, timestamp: 1_000 + seq * 100, kind, run_id,
    thread_id: 'thread', parent_ids: [], name: run_id, status: 'completed', ...extra }
}
function fold(events: TrajectoryEvent[]) {
  return events.reduce(reduceTrajectory, emptyTrajectory())
}

describe('LangGraph -> DSH snapshot', () => {
  it('retains one real user turn, model usage, TTFT, and DSH search/timeline', () => {
    seq = 0
    const state = fold([
      event('user', 'thread', { input: 'battery safety' }),
      event('model_start', 'llm', { input: { messages: [[{ role: 'system', content: 'Research carefully' }]] },
        provider: 'deepseek', model: 'deepseek-chat' }),
      event('model_first_token', 'llm'),
      event('model_end', 'llm', { output: { content: 'Found evidence' },
        usage: { input_tokens: 10, output_tokens: 4 } }),
    ])
    const snapshot = trajectorySnapshot(state)
    const turns = deriveTrajectoryLayout({ nodes: snapshot.eventNodes as never,
      systemPrompts: snapshot.systemPrompts as never,
      eventLocations: snapshot.eventLocations, requests: snapshot.requests as never,
      partial: null, runningCalls: [], callSchemas: new Map() }, t as never)
    expect(turns.map(turn => turn.turn)).toEqual([1])
    const cells = turns.flatMap(turn => turn.groups.flatMap(group => group.cells))
    expect(cells.some(cell => cell.kind === 'system')).toBe(true)
    const assistant = cells.find(cell => cell.kind === 'message')!
    expect(assistant.input).toBe(10)
    expect(assistant.output).toBe(4)
    expect(assistant.assistantMetrics?.firstTokenTime).toBe(1300)
    const index = new TrajectorySearchIndex()
    index.update([turns])
    expect(index.search('battery')?.size).toBeGreaterThan(0)
    expect(deriveTrajectoryTimeline(turns, 'sequence', t as never).spans.length).toBeGreaterThan(0)
  })

  it('shows running, completed, error, and nested tool calls', () => {
    seq = 0
    let state = fold([
      event('user', 'thread', { input: 'brand' }),
      event('tool_start', 'outer', { name: 'web_search', input: { q: 'brand' } }),
    ])
    expect(trajectorySnapshot(state).runningCalls).toHaveLength(1)
    state = reduceTrajectory(state, event('tool_start', 'child', {
      name: 'rag_search', parent_ids: ['outer'], input: { query: 'brand' },
    }))
    state = reduceTrajectory(state, event('tool_error', 'child', { error: 'failed' }))
    state = reduceTrajectory(state, event('tool_end', 'outer', { output: { result: 'news' } }))
    expect(trajectorySnapshot(state).runningCalls).toHaveLength(0)
    const snapshot = trajectorySnapshot(state)
    const turns = deriveTrajectoryLayout({ nodes: snapshot.eventNodes as never,
      eventLocations: snapshot.eventLocations, requests: [], partial: null,
      runningCalls: [], callSchemas: new Map() }, t as never)
    const cells = turns.flatMap(turn => turn.groups.flatMap(group => group.cells))
    expect(cells.find(cell => cell.kind === 'tool')?.outputDetail).toContain('news')
    expect(cells.find(cell => cell.kind === 'subtool')?.isError).toBe(true)
  })

  it('ends in-flight records as cancelled without a success result', () => {
    seq = 0
    const state = fold([event('tool_start', 'tool', { name: 'web_search' }),
      event('run_end', 'thread', { status: 'cancelled' })])
    const result = state.nodes.find(node => node.kind === 'tool-result')
    expect(result?.isError).toBe(true)
    expect(trajectorySnapshot(state).runningCalls).toHaveLength(0)
  })

  it('shows real runtime context and retry markers without inventing another turn', () => {
    seq = 0
    const state = fold([
      event('user', 'thread', { input: 'brand' }),
      event('context', 'thread', { name: 'research_config', input: { rag_enabled: true } }),
      event('retry', 'llm', { name: 'chat', input: { attempt: 2 } }),
    ])
    const contexts = trajectorySnapshot(state).eventNodes.filter(node => node.kind === 'context')
    expect(contexts).toHaveLength(2)
    expect(JSON.stringify(contexts)).toContain('rag_enabled')
    expect(JSON.stringify(contexts)).toContain('Retry: chat')
  })
})
