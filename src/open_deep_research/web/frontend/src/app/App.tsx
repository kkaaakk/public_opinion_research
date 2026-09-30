import { useMemo, useRef, useState } from 'react'
import { TrajectoryView } from '@trajectory-upstream'
import { MarkdownText } from '@deepseek-ai/dsh-client-ui-primitives'
import { zh } from '../trajectory/upstream/src/client/locales.ts'
import { emptyTrajectory, reduceTrajectory, trajectorySnapshot } from '../trajectory/adapter/reducer.ts'
import type { TrajectoryEvent, TrajectoryState } from '../trajectory/adapter/reducer.ts'

type ResearchEvent = { type: string; event?: TrajectoryEvent; message?: string;
  content?: string; input_tokens?: number; output_tokens?: number;
  total_tokens?: number; model_calls?: number }
const markdownLabels = { code: { copyLabel: '复制', copiedLabel: '已复制' }, footnotes: '脚注' }
const models = [
  ['deepseek:deepseek-flash', 'DeepSeek V4.1 Flash'],
  ['deepseek:deepseek-v4-flash', 'DeepSeek V4 Flash'],
  ['deepseek:deepseek-chat', 'DeepSeek Chat'],
  ['openai:gpt-4.1', 'GPT-4.1'], ['openai:gpt-4o', 'GPT-4o'],
  ['anthropic:claude-sonnet-4-20250514', 'Claude Sonnet 4'],
  ['anthropic:claude-opus-4-20250514', 'Claude Opus 4'],
  ['google:gemini-2.5-pro', 'Gemini 2.5 Pro'],
] as const

function translate(key: string, values?: Record<string, string | number>): string {
  let text = (zh as Record<string, string>)[key] ?? key
  for (const [name, value] of Object.entries(values ?? {})) {
    text = text.replaceAll(`{${name}}`, String(value))
  }
  return text
}

export function App() {
  const [topic, setTopic] = useState('')
  const [model, setModel] = useState<string>(models[0][0])
  const [search, setSearch] = useState('tavily')
  const [mode, setMode] = useState('normal')
  const [orgContext, setOrgContext] = useState('')
  const [ragEnabled, setRagEnabled] = useState(true)
  const [running, setRunning] = useState(false)
  const [status, setStatus] = useState('Ready')
  const [report, setReport] = useState('')
  const [usage, setUsage] = useState<ResearchEvent | null>(null)
  const [trajectory, setTrajectory] = useState<TrajectoryState>(emptyTrajectory)
  const [duration, setDuration] = useState(false)
  const abort = useRef<AbortController | null>(null)
  const lastEvent = useRef<TrajectoryEvent | null>(null)
  const snapshot = useMemo(() => trajectorySnapshot(trajectory), [trajectory])

  function accept(data: ResearchEvent) {
    if (data.type === 'trajectory' && data.event) {
      lastEvent.current = data.event
      setTrajectory(previous => reduceTrajectory(previous, data.event!))
    } else if (data.type === 'status') setStatus(data.message ?? '')
    else if (data.type === 'report') setReport(data.content ?? '')
    else if (data.type === 'usage') setUsage(data)
    else if (data.type === 'error') setStatus(data.message ?? 'Error')
    else if (data.type === 'done') setStatus('Complete')
  }

  async function start() {
    if (!topic.trim() || running) return
    const controller = new AbortController()
    abort.current = controller
    lastEvent.current = null
    setTrajectory(emptyTrajectory())
    setReport('')
    setUsage(null)
    setRunning(true)
    setStatus('Starting research…')
    try {
      const response = await fetch('/api/research', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ topic: topic.trim(), model, search_api: search,
          mode, org_context: orgContext, rag_enabled: ragEnabled, trajectory_enabled: true }),
        signal: controller.signal,
      })
      if (!response.ok || !response.body) throw new Error(`Research request failed (${response.status})`)
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let pending = ''
      while (true) {
        const { value, done } = await reader.read()
        pending += decoder.decode(value, { stream: !done }).replaceAll('\r\n', '\n')
        let boundary = pending.indexOf('\n\n')
        while (boundary >= 0) {
          const block = pending.slice(0, boundary)
          pending = pending.slice(boundary + 2)
          const text = block.split('\n').filter(line => line.startsWith('data: '))
            .map(line => line.slice(6)).join('\n')
          if (text) accept(JSON.parse(text) as ResearchEvent)
          boundary = pending.indexOf('\n\n')
        }
        if (done) break
      }
    } catch (error) {
      if (!controller.signal.aborted) setStatus(error instanceof Error ? error.message : 'Research failed')
    } finally {
      if (controller.signal.aborted) {
        const previous = lastEvent.current as TrajectoryEvent | null
        if (previous) setTrajectory(state => reduceTrajectory(state, {
          seq: previous.seq + 1, timestamp: Date.now(), kind: 'run_end',
          run_id: previous.thread_id, parent_ids: [], thread_id: previous.thread_id,
          name: 'research', status: 'cancelled',
        }))
        setStatus('Cancelled')
      }
      setRunning(false)
      abort.current = null
    }
  }

  const saveReport = () => {
    const url = URL.createObjectURL(new Blob([report], { type: 'text/markdown' }))
    const link = document.createElement('a'); link.href = url
    link.download = 'research-report.md'; link.click(); URL.revokeObjectURL(url)
  }

  return <div className="app-shell">
    <header className="app-header">
      <div><h1>Public Opinion Research</h1><span>企业舆情风险研究与处置辅助系统</span></div>
      <span className="developer-label">Developer Trajectory</span>
    </header>
    <section className="research-form" aria-label="Research request">
      <label htmlFor="topic">研究话题</label>
      <textarea id="topic" value={topic} onChange={event => setTopic(event.target.value)}
        onKeyDown={event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void start() } }}
        placeholder="例如：最近网上关于某品牌电池安全问题的舆论情况怎么样？" rows={2} />
      <div className="form-controls">
        <label>Model <select value={model} onChange={event => setModel(event.target.value)}>
          {models.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
        </select></label>
        <label>Search <select value={search} onChange={event => setSearch(event.target.value)}>
          <option value="tavily">Tavily</option><option value="openai">OpenAI</option>
          <option value="anthropic">Anthropic</option>
        </select></label>
        <label>Mode <select value={mode} onChange={event => setMode(event.target.value)}>
          <option value="fast">Fast</option><option value="normal">Normal</option>
          <option value="deep">Deep</option>
        </select></label>
        <label className="rag-control"><input type="checkbox" checked={ragEnabled}
          onChange={event => setRagEnabled(event.target.checked)} /> Enable RAG</label>
        <button onClick={() => void start()} disabled={running || !topic.trim()}>开始舆情分析</button>
        {running && <button className="stop" onClick={() => abort.current?.abort()}>Stop</button>}
      </div>
      <label className="org-label">Organization context (optional)
        <input value={orgContext} onChange={event => setOrgContext(event.target.value)}
          placeholder="e.g. Acme Corp — consumer electronics" />
      </label>
    </section>
    <div className="status" role="status">{status}</div>
    <section className="trajectory-panel" aria-label="Execution trajectory">
      <TrajectoryView
        useSession={(select: (value: unknown) => unknown) => select({ openState: 'ready', loadingOlder: false, hasMore: false })}
        useTrajectory={(select: (value: unknown) => unknown) => select(snapshot)}
        useDuration={(select: (value: boolean) => unknown) => select(duration)}
        setActualDuration={setDuration}
        loadOlder={async () => false}
        loadImage={async () => null}
        renderSlot={() => null}
        viewRequest={null}
        completeViewRequest={() => undefined}
        t={translate}
      />
    </section>
    {report && <section className="report-panel" aria-label="Research report">
      <div className="report-heading"><h2>Research Report</h2><div>
        <button onClick={saveReport}>Download</button>
        <button onClick={() => void navigator.clipboard.writeText(report)}>Copy</button>
      </div></div>
      <MarkdownText text={report} labels={markdownLabels} />
      {usage && <div className="usage">{usage.total_tokens ?? 'N/A'} tokens ·
        {usage.input_tokens ?? 'N/A'} in · {usage.output_tokens ?? 'N/A'} out ·
        {usage.model_calls ?? 'N/A'} calls</div>}
    </section>}
  </div>
}
