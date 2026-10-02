import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { emptyTrajectory, reduceTrajectory, trajectorySnapshot } from '../src/trajectory/adapter/reducer.ts'
import type { TrajectoryPacket } from '../src/trajectory/adapter/reducer.ts'
import { deriveTrajectoryLayout } from '../src/trajectory/upstream/src/client/layout.ts'
import { deriveTrajectoryTimeline } from '../src/trajectory/upstream/src/client/timeline.ts'
import { TrajectorySearchIndex } from '../src/trajectory/upstream/src/client/trajectory-search-index.ts'
import { zh } from '../src/trajectory/upstream/src/client/locales.ts'

// This is the projection of a fixed backend event log collected by a real
// LangGraph and real LangChain callback managers with an offline model fixture.
const fixture = JSON.parse(readFileSync(new URL('../../../../../tests/fixtures/trajectory.json', import.meta.url), 'utf8'))
const packet: TrajectoryPacket = { session_id: 'offline-fixture', seq: fixture.events.at(-1).seq,
  snapshot: fixture.snapshot }
const t = (key: string, values?: Record<string, string | number>) => {
  let result: string = (zh as Record<string, string>)[key] ?? key
  for (const [name, value] of Object.entries(values ?? {})) result = result.replaceAll(`{${name}}`, String(value))
  return result
}
function layout(value = packet) {
  const snapshot = trajectorySnapshot(reduceTrajectory(emptyTrajectory(), value))
  return deriveTrajectoryLayout({ nodes: snapshot.eventNodes as never,
    systemPrompts: snapshot.systemPrompts as never, eventLocations: snapshot.eventLocations as never,
    requests: snapshot.requests as never, partial: snapshot.partial as never,
    runningCalls: snapshot.runningCalls as never, callSchemas: snapshot.callSchemas as never }, t as never)
}

describe('Backend ledger projection -> original DSH UI', () => {
  it('hydrates maps without generating seq, turn, step, parent or timing', () => {
    const state = reduceTrajectory(emptyTrajectory(), packet)
    const snapshot = trajectorySnapshot(state)
    expect(snapshot.eventNodes).toEqual(fixture.snapshot.eventNodes)
    expect([...snapshot.eventLocations]).toEqual(fixture.snapshot.eventLocations)
    expect([...snapshot.callSchemas]).toEqual(fixture.snapshot.callSchemas)
    expect(state.seq).toBe(packet.seq)
    expect(snapshot.requests.map(r => r.step)).toEqual(fixture.snapshot.requests.map((r: {step: number}) => r.step))
  })

  it('renders one user turn and six real model steps across four agents', () => {
    const turns = layout()
    expect(turns.map(turn => turn.turn)).toEqual([1])
    const titles = turns.flatMap(turn => turn.groups.map(group => group.title))
    for (let step = 1; step <= 6; step++) expect(titles).toContain(t('group.step', { step }))
    expect(titles).not.toContain(t('group.step', { step: 7 }))
    const cells = turns.flatMap(turn => turn.groups.flatMap(group => group.cells))
    const messages = cells.filter(cell => cell.kind === 'message')
    expect(messages).toHaveLength(6)
    expect(messages[0].input).toBe(10)
    expect(messages[0].output).toBe(5)
    expect(messages[0].assistantMetrics?.firstTokenTime).toBe(fixture.snapshot.eventNodes.find((n: {kind: string}) => n.kind === 'assistant').timing.firstTokenTime)
    const index = new TrajectorySearchIndex()
    index.update([turns])
    expect(index.search('battery')?.size).toBeGreaterThan(0)
    expect(deriveTrajectoryTimeline(turns, 'sequence', t as never).spans.length).toBeGreaterThan(0)
  })

  it('keeps root/nested tools and real request schemas available to Inspector', () => {
    const turns = layout()
    const cells = turns.flatMap(turn => turn.groups.flatMap(group => group.cells))
    expect(cells.filter(cell => cell.kind === 'tool')).toHaveLength(2)
    expect(cells.filter(cell => cell.kind === 'subtool')).toHaveLength(1)
    expect(cells.filter(cell => cell.kind === 'tool').some(cell => cell.outputDetail?.includes('battery safety'))).toBe(true)
    const schemas = trajectorySnapshot(reduceTrajectory(emptyTrajectory(), packet)).callSchemas
    expect([...schemas.values()].some(schema => schema.name === 'web_search' && schema.parameters)).toBe(true)
  })

  it('repeated snapshots do not duplicate records and stale watermarks cannot roll back', () => {
    const first = reduceTrajectory(emptyTrajectory(), packet)
    const repeated = reduceTrajectory(first, packet)
    expect(repeated.snapshot.eventNodes).toHaveLength(first.snapshot.eventNodes.length)
    expect(reduceTrajectory(first, { ...packet, seq: packet.seq - 1 })).toBe(first)
  })

  it('rejects an older worker notification at the same durable watermark', () => {
    const current = reduceTrajectory(emptyTrajectory(), { ...packet, revision: 10 })
    expect(reduceTrajectory(current, { ...packet, revision: 9 })).toBe(current)
    expect(reduceTrajectory(current, { ...packet, revision: 11 }).revision).toBe(11)
  })

  it('does not fabricate a model step for a tool whose ownership is unresolved', () => {
    const result = fixture.snapshot.eventNodes.find((n: {kind: string}) => n.kind === 'tool-result')
    const orphan: TrajectoryPacket = { ...packet, snapshot: { ...packet.snapshot,
      systemPrompts: [], requests: [], eventNodes: [result], runningCalls: [],
      eventLocations: [[result.seq, {kind: 'turn', turn: {turn: 1, status: 'closed'}}]],
    } }
    const turns = layout(orphan)
    expect(turns.map(turn => turn.turn)).toEqual([1])
    expect(turns.flatMap(turn => turn.groups.map(group => group.title))).not.toContain(t('group.step', { step: 1 }))
    expect(turns.flatMap(turn => turn.groups.flatMap(group => group.cells)).some(cell => cell.kind === 'tool')).toBe(true)
  })
})
