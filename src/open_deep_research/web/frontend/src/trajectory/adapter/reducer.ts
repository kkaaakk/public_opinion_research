/** Hydrate the backend's deterministic projection; never allocate execution identities. */
export interface TrajectorySnapshotWire {
  eventNodes: Record<string, unknown>[]
  requests: Record<string, unknown>[]
  systemPrompts: Record<string, unknown>[]
  eventLocations: [number, Record<string, unknown>][]
  callSchemas: [string, Record<string, unknown>][]
  partial: Record<string, unknown> | null
  runningCalls: Record<string, unknown>[]
  status: string
  budgetUsage?: Record<string, unknown> | null
}
export interface TrajectoryPacket {
  session_id: string
  seq: number
  revision?: number
  snapshot: TrajectorySnapshotWire
  degraded?: boolean
}
export interface TrajectoryState {
  sessionId: string | null
  seq: number
  revision: number
  snapshot: TrajectorySnapshotWire
  degraded: boolean
}
const emptySnapshot = (): TrajectorySnapshotWire => ({
  eventNodes: [], requests: [], systemPrompts: [], eventLocations: [],
  callSchemas: [], partial: null, runningCalls: [], status: 'ready',
})
export const emptyTrajectory = (): TrajectoryState => ({
  sessionId: null, seq: -1, revision: 0, snapshot: emptySnapshot(), degraded: false,
})
export function reduceTrajectory(state: TrajectoryState, packet: TrajectoryPacket): TrajectoryState {
  if (state.sessionId === packet.session_id && packet.seq < state.seq) return state
  if (state.sessionId === packet.session_id && packet.seq === state.seq
    && packet.revision !== undefined && packet.revision < state.revision) return state
  return { sessionId: packet.session_id, seq: packet.seq, snapshot: packet.snapshot,
    revision: packet.revision ?? state.revision, degraded: Boolean(packet.degraded) }
}
export function trajectorySnapshot(state: TrajectoryState) {
  return { ...state.snapshot, eventLocations: new Map(state.snapshot.eventLocations),
    callSchemas: new Map(state.snapshot.callSchemas) }
}
