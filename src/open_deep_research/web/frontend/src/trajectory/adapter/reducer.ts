/** Browser-only projection of the LangGraph SSE contract into DSH's snapshot. */
export interface TrajectoryEvent {
  seq: number; timestamp: number; kind: string; run_id: string
  parent_ids: string[]; thread_id: string; name: string; status: string
  agent_name?: string | null; metadata?: Record<string, unknown>
  input?: unknown; output?: unknown; error?: string
  provider?: string | null; model?: string | null
  usage?: Record<string, unknown> | null; ttft_ms?: number | null
}
interface ToolRun {
  id: string; name: string; input: string; output?: string; error?: string
  start: number; end?: number; step: number; parent?: string
}
interface ModelRun {
  id: string; step: number; start: number; first?: number
  input: unknown; provider?: string; model?: string
}
interface AgentRun { step: number; start: number; name: string }
export interface TrajectoryState {
  nodes: Record<string, unknown>[]; tools: Record<string, ToolRun>
  models: Record<string, ModelRun>; agents: Record<string, AgentRun>
  requests: Record<string, unknown>[]
  systemPrompts: Record<string, unknown>[]; nextStep: number; terminal?: string
}
export const emptyTrajectory = (): TrajectoryState => ({
  nodes: [], tools: {}, models: {}, agents: {}, requests: [], systemPrompts: [], nextStep: 1,
})
const format = (value: unknown): string => typeof value === 'string' ? value
  : value == null ? '' : JSON.stringify(value, null, 2) ?? String(value)
function content(value: unknown): string {
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map(content).filter(Boolean).join('\n')
  if (value && typeof value === 'object') {
    const object = value as Record<string, unknown>
    if (object.content !== undefined) return content(object.content)
    for (const key of ['message', 'generations', 'output']) {
      if (object[key] !== undefined) return content(object[key])
    }
  }
  return format(value)
}
function systemText(value: unknown): string | undefined {
  if (Array.isArray(value)) return value.map(systemText).find(Boolean)
  if (value && typeof value === 'object') {
    const item = value as Record<string, unknown>
    if (item.role === 'system' || item.type === 'system') return content(item.content)
    for (const key of ['messages', 'input']) {
      if (item[key] !== undefined) {
        const found = systemText(item[key]); if (found) return found
      }
    }
  }
  return undefined
}
function usageOf(value?: Record<string, unknown> | null) {
  if (!value) return undefined
  const details = value.input_token_details as Record<string, unknown> | undefined
  const outputDetails = value.output_token_details as Record<string, unknown> | undefined
  return {
    ...(typeof value.input_tokens === 'number' ? { inputTokens: value.input_tokens } : {}),
    ...(typeof value.output_tokens === 'number' ? { outputTokens: value.output_tokens } : {}),
    ...(typeof details?.cache_read === 'number' ? { cacheReadTokens: details.cache_read } : {}),
    ...(typeof details?.cache_creation === 'number' ? { cacheWriteTokens: details.cache_creation } : {}),
    ...(typeof outputDetails?.reasoning === 'number' ? { reasoningTokens: outputDetails.reasoning } : {}),
  }
}
function toolShape(state: TrajectoryState, tool: ToolRun): Record<string, unknown> {
  const subCalls = Object.values(state.tools).filter(run => run.parent === tool.id)
    .map(child => toolShape(state, child))
  if (tool.end === undefined) return { callId: tool.id, name: tool.name,
    argsRaw: tool.input, phase: 'running', time: tool.start, turn: 1,
    step: tool.step, subCalls }
  return { kind: 'tool-result', callId: tool.id, seq: 0, time: tool.end,
    callTime: tool.start, call: { name: tool.name, argsRaw: tool.input },
    content: [{ type: 'text', text: tool.output ?? tool.error ?? '' }],
    isError: tool.error !== undefined,
    ...(tool.error ? { error: { name: 'ToolError', code: tool.error } } : {}), subCalls }
}
/** Pure reducer: all updates replace state so streaming remains reactive. */
export function reduceTrajectory(state: TrajectoryState, event: TrajectoryEvent): TrajectoryState {
  const { kind, timestamp: time, seq } = event
  if (kind === 'user') return { ...state, nodes: [...state.nodes,
    { kind: 'user', seq, time, content: [{ type: 'text', text: content(event.input) }],
      source: { kind: 'user' } }] }
  if (kind === 'context') return { ...state, nodes: [...state.nodes,
    { kind: 'context', seq, time,
      content: [{ type: 'text', text: format(event.input) }],
      source: { kind: 'runtime', name: event.name } }] }
  if (kind === 'run_end') {
    const error = event.status === 'completed' ? 'Incomplete' : event.status
    const tools = Object.fromEntries(Object.entries(state.tools).map(([id, tool]) =>
      [id, tool.end === undefined ? { ...tool, end: time, error } : tool]))
    const closed = { ...state, tools }
    const unfinishedRoots = Object.values(state.tools)
      .filter(tool => !tool.parent && tool.end === undefined)
      .map((tool, index) => ({ ...toolShape(closed, tools[tool.id]), seq: seq + index / 100 }))
    return { ...state, tools, nodes: [...state.nodes, ...unfinishedRoots],
      models: {}, agents: {}, terminal: event.status,
      requests: state.requests.map(request => request.status === 'running'
        ? { ...request, status: 'error', completedAt: time, error } : request) }
  }
  if (kind === 'agent_start') {
    const name = event.agent_name || event.name; const step = state.nextStep
    return { ...state, nextStep: step + 1,
      agents: { ...state.agents, [event.run_id]: { step, start: time, name } },
      nodes: [...state.nodes,
      { kind: 'context', seq, time, content: [{ type: 'text', text: `Agent: ${name}` }],
        source: { kind: 'agent', name } },
      { kind: 'assistant', seq: seq + 0.01, time, turn: 1, step,
        blocks: [{ kind: 'text', text: name }], timing: { stepStartTime: time } },
    ] }
  }
  if (kind === 'retry') return { ...state, nodes: [...state.nodes,
    { kind: 'context', seq, time, content: [{ type: 'text',
      text: `Retry: ${event.name}${event.input ? ` ${format(event.input)}` : ''}` }],
      source: { kind: 'runtime', name: event.name } }] }
  if (kind === 'agent_end' || kind === 'agent_error') {
    const agent = state.agents[event.run_id]; if (!agent) return state
    const agents = { ...state.agents }; delete agents[event.run_id]
    return { ...state, agents, nodes: state.nodes.map(node =>
      node.kind === 'assistant' && node.step === agent.step
        ? { ...node, time, ...(kind === 'agent_error'
          ? { blocks: [{ kind: 'text', text: event.error ?? 'Agent error' }] } : {}) }
        : node) }
  }
  if (kind === 'model_start') {
    const step = state.nextStep
    const model: ModelRun = { id: event.run_id, step, start: time, input: event.input,
      provider: event.provider ?? undefined, model: event.model ?? event.name }
    const system = systemText(event.input)
    const systemPrompts = system && !state.systemPrompts.some(row => row.text === system)
      ? [...state.systemPrompts, { seq, time, text: system, update: state.systemPrompts.length > 0 }]
      : state.systemPrompts
    return { ...state, nextStep: step + 1, systemPrompts,
      models: { ...state.models, [event.run_id]: model },
      requests: [...state.requests, { purpose: 'assistant', turn: 1, step,
        startSeq: seq, startedAt: time, completedAt: null, status: 'running',
        providerMetadata: { provider: model.provider, model: model.model },
        requestConfig: { provider: model.provider, model: model.model,
          input: model.input } }] }
  }
  if (kind === 'model_first_token') {
    const model = state.models[event.run_id]
    return model ? { ...state, models: { ...state.models,
      [event.run_id]: { ...model, first: time } } } : state
  }
  if (kind === 'model_end' || kind === 'model_error') {
    const model = state.models[event.run_id]; if (!model) return state
    const models = { ...state.models }; delete models[event.run_id]
    const error = kind === 'model_error'; const usage = usageOf(event.usage)
    const requests = state.requests.map(request => request.step === model.step
      ? { ...request, status: error ? 'error' : 'complete', completedAt: time,
        resultSeq: seq, ...(error ? { error: event.error } : {}),
        ...(usage ? { usage } : {}) } : request)
    return { ...state, models, requests, nodes: [...state.nodes,
      { kind: 'assistant', seq, time, turn: 1, step: model.step,
        blocks: [{ kind: 'text', text: error ? event.error ?? 'Model error' : content(event.output) }],
        timing: { stepStartTime: model.start, firstTokenTime: model.first ?? null },
        usage, providerMetadata: { provider: model.provider, model: model.model },
        requestConfig: { provider: model.provider, model: model.model,
          input: model.input } }] }
  }
  if (kind === 'tool_start') {
    const parent = [...event.parent_ids].reverse().find(id => state.tools[id] !== undefined)
    const step = state.nextStep
    const tool: ToolRun = { id: event.run_id, name: event.name,
      input: format(event.input), start: time, step, parent }
    const nodes = parent ? state.nodes : [...state.nodes,
      { kind: 'assistant', seq, time, turn: 1, step,
        blocks: [{ kind: 'tool-call', callId: tool.id, name: tool.name, argsRaw: tool.input }],
        timing: { stepStartTime: time } }]
    return { ...state, nextStep: step + 1, nodes, tools: { ...state.tools, [tool.id]: tool } }
  }
  if (kind === 'tool_end' || kind === 'tool_error') {
    const tool = state.tools[event.run_id]; if (!tool) return state
    const updated: ToolRun = { ...tool, end: time,
      ...(kind === 'tool_end' ? { output: format(event.output) }
        : { error: event.error ?? 'Tool error' }) }
    const next = { ...state, tools: { ...state.tools, [event.run_id]: updated } }
    return tool.parent ? next : { ...next, nodes: [...state.nodes,
      { ...toolShape(next, updated), seq }] }
  }
  return state
}
export function trajectorySnapshot(state: TrajectoryState) {
  const runningCalls = Object.values(state.tools)
    .filter(tool => !tool.parent && tool.end === undefined).map(tool => toolShape(state, tool))
  const active = Object.values(state.models).at(-1)
  return { systemPrompts: state.systemPrompts, eventNodes: state.nodes,
    eventLocations: new Map(), requests: state.requests, callSchemas: new Map(),
    partial: active ? { turn: 1, step: active.step, blocks: [{ kind: 'text', text: '' }] } : null,
    runningCalls }
}
