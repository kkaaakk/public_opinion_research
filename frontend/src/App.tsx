import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactFlow, { Background, Controls, MiniMap, Position, type Edge, type Node } from "@xyflow/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Activity, ArrowRight, Check, CircleAlert, CirclePause, Clock3, Copy, Download, FileText, History, Layers3, Play, Radio, RefreshCw, RotateCcw, Search, Settings2, ShieldCheck, Square, TerminalSquare, Workflow, X } from "lucide-react";
import { EDGES, PHASES, TERMINAL, describeEvent, parseFrames, phaseForEvent, projectRun } from "./protocol.js";

type AnyObj = Record<string, any>;
type Session = { key: string; request: AnyObj; runId: string | null; cursor: number };
const STORAGE_KEY = "public-opinion-studio-run-v1";
const MODELS = ["deepseek:deepseek-flash", "deepseek:deepseek-chat", "deepseek:deepseek-v4-flash", "openai:gpt-4.1", "openai:gpt-4o"];
const STATUSES: Record<string, string> = { queued: "等待调度", running: "运行中", awaiting_approval: "等待审批", cancelling: "取消与清算中", completed: "已完成", failed: "运行失败", cancelled: "已取消", interrupted: "执行中断" };
const PHASE_NAMES = Object.fromEntries(PHASES.map(p => [p.id, p.label]));
const MAX_EVENTS = 2000;

function restoreSession(): Session | null {
  try {
    const entry = sessionStorage.getItem(STORAGE_KEY);
    if (!entry) return null;
    const value = JSON.parse(entry);
    if (typeof value.key === "string" && typeof value.request?.topic === "string" &&
      (value.runId === null || typeof value.runId === "string") && Number.isSafeInteger(value.cursor) && value.cursor >= 0) return value;
  } catch { /* Invalid tab-local data is ignored. */ }
  return null;
}
function storeSession(value: Session | null) {
  if (value) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(value));
  else sessionStorage.removeItem(STORAGE_KEY);
}
function newKey() { return crypto.randomUUID(); }
function statusTone(value: string) {
  if (value === "completed" || value === "settled") return "success";
  if (value === "failed" || value === "interrupted" || value === "unresolved" || value === "error") return "danger";
  if (value === "running" || value === "cancelling" || value === "paused") return "active";
  return "muted";
}
function stamp(value: unknown): string {
  if (typeof value !== "string" || !value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString("zh-CN");
}
async function errorFrom(response: Response) {
  const payload = await response.json().catch(() => ({}));
  return new Error(typeof payload.detail === "string" ? payload.detail : "请求失败（HTTP " + response.status + "）");
}
async function api(path: string, token: string, init: RequestInit = {}) {
  const headers: Record<string, string> = { "Content-Type": "application/json", ...(init.headers as Record<string, string> || {}) };
  if (token.trim()) headers.Authorization = "Bearer " + token.trim();
  const response = await fetch(path, { ...init, headers, cache: "no-store" });
  if (!response.ok) throw await errorFrom(response);
  return response;
}

const POSITIONS: Record<string, { x: number; y: number }> = {
  planning: { x: 310, y: 10 },
  public_signal: { x: 65, y: 155 },
  internal_knowledge: { x: 545, y: 155 },
  review: { x: 310, y: 315 },
  risk_assessment: { x: 310, y: 470 },
  response_strategy: { x: 310, y: 625 },
  reporting: { x: 310, y: 780 }
};
const STATUS_ICONS: Record<string, string> = { completed: "✓", running: "●", error: "!", paused: "Ⅱ", disabled: "–", waiting: "○" };

export default function App() {
  const [topic, setTopic] = useState("");
  const [org, setOrg] = useState("");
  const [model, setModel] = useState(MODELS[0]);
  const [mode, setMode] = useState("normal");
  const [rag, setRag] = useState(true);
  const [approval, setApproval] = useState(false);
  const [token, setToken] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [session, setSession] = useState<Session | null>(() => restoreSession());
  const [run, setRun] = useState<AnyObj | null>(null);
  const [events, setEvents] = useState<AnyObj[]>([]);
  const [report, setReport] = useState("");
  const [selectedPhase, setSelectedPhase] = useState("planning");
  const [selectedEvent, setSelectedEvent] = useState<AnyObj | null>(null);
  const [eventFilter, setEventFilter] = useState("all");
  const [reportTab, setReportTab] = useState(false);
  const [historyItems, setHistoryItems] = useState<AnyObj[]>([]);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [historyQuery, setHistoryQuery] = useState("");
  const [historyError, setHistoryError] = useState("");
  const [historyRecord, setHistoryRecord] = useState<AnyObj | null>(null);
  const [openId, setOpenId] = useState("");
  const [busy, setBusy] = useState(false);
  const [following, setFollowing] = useState(false);
  const [message, setMessage] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const activeSessionRef = useRef<Session | null>(session);
  const followSerial = useRef(0);
  const tokenRef = useRef(token);
  tokenRef.current = token;
  const commitSession = useCallback((s: Session | null) => {
    activeSessionRef.current = s;
    setSession(s);
    storeSession(s);
  }, []);

  const loadHistory = useCallback(async (offset = 0, query = historyQuery) => {
    try {
      const res = await api("/api/history?q=" + encodeURIComponent(query) + "&limit=10&offset=" + offset, tokenRef.current);
      const data = await res.json();
      setHistoryItems(Array.isArray(data.items) ? data.items : []);
      setHistoryTotal(data.total || 0);
      setHistoryOffset(offset);
      setHistoryError("");
    } catch (err) { setHistoryError((err as Error).message); }
  }, [historyQuery]);

  useEffect(() => { loadHistory(0, ""); }, []); // One attempt; entering a token allows manual reload.
  useEffect(() => () => { followSerial.current++; abortRef.current?.abort(); }, []);

  const applySnapshot = (data: AnyObj) => {
    setRun(data);
    if (typeof data.report === "string" && data.report) setReport(data.report);
  };

  // Observe only existing DBOS identity/cursor. No GET/SSE action creates or retries work.
  const follow = async (entry: Session) => {
    const serial = ++followSerial.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setFollowing(true);
    setMessage("");
    let current = entry;
    try {
      if (!current.runId) {
        const response = await api("/api/runs", tokenRef.current, {
          method: "POST", headers: { "Idempotency-Key": current.key },
          body: JSON.stringify(current.request), signal: controller.signal
        });
        const created = await response.json();
        current = { ...current, runId: created.run_id };
        commitSession(current);
        applySnapshot(created);
      }
      const runId = current.runId;
      if (!runId) return;
      while (serial === followSerial.current && !controller.signal.aborted) {
        const snap = await (await api("/api/runs/" + encodeURIComponent(runId), tokenRef.current, { signal: controller.signal })).json();
        if (serial !== followSerial.current) break;
        applySnapshot(snap);
        const delivered = current.cursor >= (snap.event_cursor ?? 0);
        if (TERMINAL.has(snap.status) && snap.settlement !== "pending" && snap.events_complete && delivered) break;
        if (snap.status === "awaiting_approval" && snap.events_paused && delivered) break;
        const res = await api("/api/runs/" + encodeURIComponent(runId) + "/events", tokenRef.current, {
          headers: { "Last-Event-ID": String(current.cursor) }, signal: controller.signal
        });
        if (!res.body) throw new Error("服务器未返回 SSE 流");
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (serial === followSerial.current) {
          const packet = await reader.read();
          buffer += decoder.decode(packet.value || new Uint8Array(), { stream: !packet.done });
          const parsed = parseFrames(buffer);
          buffer = parsed.rest;
          for (const event of parsed.frames) {
            if (!Number.isSafeInteger(event.seq) || event.seq <= current.cursor) continue;
            if (event.seq !== current.cursor + 1) throw new Error("事件游标不连续，请重新订阅");
            current = { ...current, cursor: event.seq };
            commitSession(current);
            setEvents(old => [...old, event].slice(-MAX_EVENTS));
            if (event.type === "report" && typeof event.content === "string") setReport(event.content);
          }
          if (packet.done) break;
        }
        // EOF is not task completion: refresh the authoritative DBOS snapshot.
      }
    } catch (err) {
      if (!controller.signal.aborted && serial === followSerial.current) {
        setMessage((err as Error).message + " · 可使用同一 Run 恢复订阅");
      }
    } finally {
      if (serial === followSerial.current) setFollowing(false);
    }
  };

  const start = async () => {
    if (!topic.trim() || busy || following) return;
    setBusy(true);
    try {
      const entry: Session = { key: newKey(), runId: null, cursor: 0,
        request: { topic: topic.trim(), model, search_api: "tavily", mode, org_context: org, rag_enabled: rag, require_approval: approval } };
      setRun(null); setEvents([]); setReport(""); setReportTab(false); setHistoryRecord(null); setSelectedEvent(null);
      commitSession(entry); // Persist before POST so an ambiguous response reuses the same key/body.
      await follow(entry);
    } finally { setBusy(false); }
  };
  const attach = async (entry = activeSessionRef.current) => {
    if (!entry || following) return;
    setHistoryRecord(null);
    await follow(entry);
  };
  const openRun = async () => {
    if (!/^[a-f0-9]{32}$/i.test(openId.trim())) { setMessage("请输入服务端返回的有效 Run ID"); return; }
    followSerial.current++; abortRef.current?.abort();
    const entry: Session = { key: newKey(), runId: openId.trim(), cursor: 0, request: { topic: "已存在的研究任务" } };
    setEvents([]); setRun(null); setReport(""); setSelectedEvent(null); commitSession(entry);
    await follow(entry);
  };
  const cancel = async () => {
    const id = activeSessionRef.current?.runId;
    if (!id) return;
    try {
      await api("/api/runs/" + id + "/cancel", tokenRef.current, { method: "POST" });
      setMessage("已提交取消请求；仍需等待 DBOS / SDK 完成清算");
    } catch (err) { setMessage((err as Error).message); }
  };
  const decide = async (decision: "approve" | "reject") => {
    if (!session?.runId || !run?.plan?.version || busy) return;
    setBusy(true);
    try {
      await api("/api/runs/" + session.runId + "/approval", tokenRef.current, {
        method: "POST", body: JSON.stringify({ plan_version: run.plan.version, decision })
      });
      await follow(session);
    } catch (err) { setMessage((err as Error).message); }
    finally { setBusy(false); }
  };
  const retry = async () => {
    if (!session?.runId || !run?.retry_safe || !run.state_version || busy) return;
    setBusy(true);
    try {
      await api("/api/runs/" + session.runId + "/retry", tokenRef.current, {
        method: "POST", headers: { "Idempotency-Key": newKey() },
        body: JSON.stringify({ state_version: run.state_version })
      });
      await follow(session);
    } catch (err) { setMessage((err as Error).message); }
    finally { setBusy(false); }
  };
  const openHistory = async (id: string) => {
    try {
      const data = await (await api("/api/history/" + encodeURIComponent(id), tokenRef.current)).json();
      setHistoryRecord(data); setReportTab(true);
    } catch (err) { setMessage((err as Error).message); }
  };
  const reset = () => {
    followSerial.current++; abortRef.current?.abort();
    commitSession(null); setRun(null); setEvents([]); setReport(""); setHistoryRecord(null); setSelectedEvent(null); setReportTab(false); setMessage("");
  };

  const projection = useMemo(() => projectRun(events, run, session?.request), [events, run, session]);
  const nodes: Node[] = useMemo(() => PHASES.map(phase => {
    const state = projection.statuses[phase.id];
    return {
      id: phase.id, type: "default", position: POSITIONS[phase.id],
      sourcePosition: Position.Bottom, targetPosition: Position.Top, draggable: false, selectable: true,
      className: "graph-node graph-" + state + (selectedPhase === phase.id ? " graph-selected" : ""),
      data: { label: (
        <div className="node-content">
          <span className="node-status">{STATUS_ICONS[state] || "○"}</span>
          <div><strong>{phase.label}</strong><small>{phase.subtitle}</small></div>
        </div>
      ) }
    };
  }), [projection, selectedPhase]);
  const edges: Edge[] = useMemo(() => EDGES.map(([source, target]) => ({
    id: source + "-" + target, source, target, type: "smoothstep", animated: projection.statuses[target] === "running",
    style: { stroke: projection.statuses[source] === "completed" ? "#499f81" : "#34445b", strokeWidth: 1.9 }
  })), [projection]);

  const selectedEvents = events.filter(e => phaseForEvent(e) === selectedPhase);
  const visibleEvents = events.filter(e => eventFilter === "all" || phaseForEvent(e) === eventFilter).slice().reverse();
  const currentReport = historyRecord?.report || report;
  const apiStatus = run?.status || (session ? "尚未连接" : "未启动");
  const canCancel = !!run && ["queued", "running", "awaiting_approval", "cancelling"].includes(run.status);
  const statusDone = !!run && TERMINAL.has(run.status) && run.settlement !== "pending";
  const tokenRequired = message.includes("401") || message.includes("令牌") || message.includes("Authentication");

  return <div className="shell">
    <aside className="rail">
      <div className="brand"><div className="brand-mark"><Workflow size={22}/></div><div><strong>Execution Studio</strong><small>PUBLIC OPINION RESEARCH</small></div></div>
      <div className="rail-label">WORKSPACE</div>
      <button className="rail-main active" onClick={() => { setHistoryRecord(null); setReportTab(false); }}><Activity size={17}/> 运行观测台</button>
      <button className="rail-main" onClick={() => { setReportTab(true); }}><FileText size={17}/> 研究报告</button>
      <div className="rail-label history-label"><History size={14}/> 历史归档 <span>{historyTotal}</span></div>
      <div className="search"><Search size={14}/><input value={historyQuery} placeholder="搜索历史研究" onChange={e => setHistoryQuery(e.target.value)} onKeyDown={e => e.key === "Enter" && loadHistory(0, historyQuery)}/><button title="搜索" onClick={() => loadHistory(0, historyQuery)}><ArrowRight size={14}/></button></div>
      <div className="history-list">
        {historyError && <p className="error-note">{historyError}</p>}
        {historyItems.map(item => <button key={item.id} className={"history-item" + (historyRecord?.id === item.id ? " chosen" : "")} onClick={() => openHistory(item.id)}>
          <span className="history-topic">{item.topic || "未命名研究"}</span>
          <span className="history-meta"><span>{STATUSES[item.status] || item.status || "归档"}</span><span>{stamp(item.completed_at)}</span></span>
        </button>)}
        {!historyItems.length && !historyError && <p className="empty-history">暂无归档记录</p>}
      </div>
      <div className="pagination"><button disabled={!historyOffset} onClick={() => loadHistory(Math.max(0, historyOffset-10))}>上一页</button><span>{Math.floor(historyOffset/10)+1} / {Math.max(1,Math.ceil(historyTotal/10))}</span><button disabled={historyOffset+10>=historyTotal} onClick={() => loadHistory(historyOffset+10)}>下一页</button></div>
      <div className="rail-bottom"><div className="stack-info"><ShieldCheck size={15}/><span>DBOS 3.3 · SDK 0.24</span></div><span>单操作员 · 只读事件回放</span><a href="/classic">经典页面</a></div>
    </aside>
    <main className="main">
      <header className="topbar"><div className="bread"><span>工作空间</span><span>/</span><strong>运行观测台</strong></div><div className="top-actions"><span className={"status-pill "+statusTone(run?.status || "")}><span className="pulse"/> {STATUSES[apiStatus] || apiStatus}</span><button title="访问设置" className="icon-button" onClick={() => setSettingsOpen(!settingsOpen)}><Settings2 size={18}/></button></div></header>
      {settingsOpen && <section className="settings"><label>API 访问令牌（仅保留在当前页面内存）<input type="password" value={token} placeholder="Bearer token" onChange={e=>setToken(e.target.value)} autoComplete="off"/></label><div className="settings-row"><button onClick={() => loadHistory(0)}>刷新归档</button><button onClick={() => attach()} disabled={!session || following}>重新订阅 Run</button><button onClick={() => setSettingsOpen(false)}>关闭</button></div></section>}
      <div className="content">
        <div className="heading"><div><div className="eyebrow">AGENT ORCHESTRATION / LIVE DEBUGGER</div><h1>研究执行观测台</h1><p>观察 DBOS 步骤、并行 Agent、SDK 调用与清算过程。事件来自真实运行日志。</p></div><div className="heading-buttons"><button className="secondary" onClick={() => { setReportTab(!reportTab); setHistoryRecord(null); }}><Layers3 size={15}/>{reportTab ? "返回拓扑" : "查看报告"}</button><button className="secondary" disabled={!session || following} onClick={() => attach()}><RefreshCw size={15}/> 恢复订阅</button></div></div>

        <section className="launch panel"><div className="launch-title"><div><Radio size={17}/> 新建舆情研究</div><span>DBOS Native Run</span></div><div className="launch-input"><textarea value={topic} onChange={e=>setTopic(e.target.value)} maxLength={12000} placeholder="输入需要分析的品牌、企业、产品或舆情事件..." onKeyDown={e=>{if((e.ctrlKey||e.metaKey)&&e.key==="Enter"){e.preventDefault();start();}}}/><button className="primary" onClick={start} disabled={busy || following || !topic.trim()}><Play size={16}/> 开始研究</button></div><div className="launch-controls"><label>模型<select value={model} onChange={e=>setModel(e.target.value)}>{MODELS.map(x=><option key={x}>{x}</option>)}</select></label><label>模式<select value={mode} onChange={e=>setMode(e.target.value)}><option value="fast">Fast</option><option value="normal">Normal</option><option value="deep">Deep</option></select></label><label>组织背景<input value={org} onChange={e=>setOrg(e.target.value)} maxLength={8000} placeholder="可选"/></label><label className="check"><input type="checkbox" checked={rag} onChange={e=>setRag(e.target.checked)}/> RAG</label><label className="check"><input type="checkbox" checked={approval} onChange={e=>setApproval(e.target.checked)}/> 计划审批</label></div></section>

        <section className="run-top">
          <div><span className="section-label">CURRENT RUN</span><div className="run-id">{run?.run_id || session?.runId || "—"} {run?.run_id && <button title="复制 Run ID" onClick={() => navigator.clipboard?.writeText(run.run_id)}><Copy size={13}/></button>}</div></div>
          <div className="run-metrics"><div><span>已接收事件</span><strong>{session?.cursor || 0}</strong></div><div><span>模型调用</span><strong>{run?.usage?.model_calls ?? "—"}</strong></div><div><span>总 Token</span><strong>{run?.usage?.total_tokens ?? "—"}</strong></div><div><span>清算状态</span><strong className={statusTone(run?.settlement || "")}>{run?.settlement || "—"}</strong></div></div>
          <div className="run-buttons">{canCancel && <button className="danger-button" onClick={cancel} disabled={run?.status==="cancelling"}><Square size={13}/>取消</button>}{run?.retry_safe && <button className="secondary" onClick={retry} disabled={busy || following}><RotateCcw size={14}/>安全重试</button>}</div>
        </section>
        {!run && <div className="connect-bar"><input aria-label="已有 Run ID" value={openId} onChange={e=>setOpenId(e.target.value)} placeholder="输入已有 Run ID，查看持久执行轨迹"/><button className="secondary" onClick={openRun}>打开 Run</button>{session && <button className="secondary" onClick={() => attach()} disabled={following}>恢复当前 Run</button>}</div>}
        {!!run && !!session && <div className="connect-bar subtle"><span>当前任务：{session.request.topic}</span><span>Generation {run.generation ?? 0}</span><span>State v{run.state_version || "—"}</span><button onClick={reset} title="仅清理浏览器当前会话，不取消服务端任务">清除本地选择 <X size={13}/></button></div>}
        {message && <div className="message" role="alert"><CircleAlert size={16}/>{message}{tokenRequired && <button onClick={()=>setSettingsOpen(true)}>设置令牌</button>}<button title="关闭提示" onClick={()=>setMessage("")}><X size={14}/></button></div>}

        {run?.status==="awaiting_approval" && run.plan && <section className="approval panel"><div className="approval-heading"><CirclePause size={18}/><strong>研究计划待审批</strong><span>版本 {String(run.plan.version).slice(0,12)}</span></div><p>{run.plan.brief}</p><div className="approval-section">{(run.plan.sections || []).map((s:AnyObj,i:number)=><span key={i}>{s.name || "章节 "+(i+1)}</span>)}</div><div className="approval-actions"><button className="primary" onClick={()=>decide("approve")} disabled={busy}><Check size={15}/> 批准执行</button><button className="secondary" onClick={()=>decide("reject")} disabled={busy}>拒绝计划</button></div></section>}
        {run?.settlement==="pending" && <div className="message neutral"><Clock3 size={16}/>DBOS 已报告终态，但 SDK 工具与预算仍在清算，正在继续订阅直至最终状态确认。</div>}
        {run?.status==="interrupted" && <div className="message"><CircleAlert size={16}/>存在中断或未知外部效果。此处不提供强制重放，请按后端核对流程处理。</div>}

        {reportTab ? <section className="panel report-panel"><div className="panel-head"><div><FileText size={17}/><strong>{historyRecord ? "归档报告" : "本次研究报告"}</strong></div><div className="head-right"><span>{historyRecord ? "Neo4j History · 非完整 Trace" : statusDone ? "最终状态已确认" : "实时 Run 状态"}</span><button className="secondary small" disabled={!currentReport} onClick={()=>navigator.clipboard?.writeText(currentReport)}><Copy size={14}/>复制</button><button className="secondary small" disabled={!currentReport} onClick={()=>{
          const url=URL.createObjectURL(new Blob([currentReport],{type:"text/markdown;charset=utf-8"}));
          const a=document.createElement("a");a.href=url;a.download="public-opinion-report.md";a.click();URL.revokeObjectURL(url);
        }}><Download size={14}/>导出</button></div></div><div className="markdown">{currentReport ? <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>{currentReport}</ReactMarkdown> : <div className="empty-large">尚无最终报告。运行中的阶段报告可在拓扑节点详情查看。</div>}</div></section> :
        <div className="studio-grid">
          <section className="panel flow-panel"><div className="panel-head"><div><Workflow size={17}/><strong>工作流执行拓扑</strong></div><span>节点状态来自 DBOS / SDK 事件</span></div><div className="flow-frame"><ReactFlow nodes={nodes} edges={edges} fitView fitViewOptions={{padding:0.18}} nodesDraggable={false} nodesConnectable={false} elementsSelectable onNodeClick={(_,node)=>{setSelectedPhase(node.id);setSelectedEvent(null);}} proOptions={{hideAttribution:true}}><Background color="#27354b" gap={22}/><Controls showInteractive={false}/><MiniMap pannable zoomable maskColor="rgba(12,21,35,.72)" nodeColor={n=>projection.statuses[n.id]==="completed"?"#499f81":"#4e668b"}/></ReactFlow></div><div className="legend"><span><i className="legend-dot completed"/>完成</span><span><i className="legend-dot active"/>执行中</span><span><i className="legend-dot muted"/>未执行</span><span><i className="legend-dot danger"/>异常</span><span>补查会重新进入 Producer / Review</span></div></section>
          <aside className="panel details"><div className="panel-head"><div><TerminalSquare size={17}/><strong>节点检查器</strong></div></div><div className="inspect-title"><span>{PHASE_NAMES[selectedPhase]}</span><span className={"tiny-badge "+statusTone(projection.statuses[selectedPhase])}>{projection.statuses[selectedPhase]}</span></div><div className="inspect-sub">本次接收到 {selectedEvents.length} 条关联事件 · {projection.counts[selectedPhase] || 0} 次步骤开始</div><div className="detail-intro">点击左侧任意节点查看 SDK、步骤、证据阶段事件。这里展示已发布的元数据，不包含原始 Prompt 或工具参数。</div><div className="inspect-events">{selectedEvents.slice(-22).reverse().map(e=><button key={e.seq} className={"inspect-event"+(selectedEvent?.seq===e.seq?" chosen":"")} onClick={()=>setSelectedEvent(e)}><span className="seq">#{e.seq}</span><span>{describeEvent(e)}</span><ArrowRight size={13}/></button>)}{!selectedEvents.length && <div className="empty-inspect">这个节点暂时没有可公开的运行事件。</div>}</div><div className="raw-detail"><div className="raw-title">事件内容 {selectedEvent ? "#"+selectedEvent.seq : ""}</div>{selectedEvent ? <pre>{JSON.stringify(selectedEvent,null,2)}</pre> : <p>选择一条事件查看经过服务端过滤的原始结构化数据。</p>}</div></aside>
        </div>}
        <section className="panel timeline"><div className="panel-head"><div><History size={17}/><strong>运行事件时间线</strong><span className="event-count">{events.length} 条{events.length>=MAX_EVENTS?" · 仅显示最近 2000 条":""}</span></div><div className="head-right"><select aria-label="事件筛选" value={eventFilter} onChange={e=>setEventFilter(e.target.value)}><option value="all">全部节点</option>{PHASES.map(p=><option value={p.id} key={p.id}>{p.label}</option>)}</select><button className="secondary small" onClick={()=>{setSelectedEvent(null);setEventFilter("all");}}>重置筛选</button></div></div><div className="event-list">{visibleEvents.slice(0,120).map(e=><button key={e.seq} className={"event-row"+(selectedEvent?.seq===e.seq?" chosen":"")} onClick={()=>{setSelectedEvent(e);setSelectedPhase(phaseForEvent(e)||"planning");}}><span className="event-seq">#{e.seq}</span><span className={"event-type "+(e.type==="error"?"danger":"")}>{e.type}</span><span className="event-desc">{describeEvent(e)}</span><span className="event-phase">{PHASE_NAMES[phaseForEvent(e)||""] || "DBOS"}</span></button>)}{!visibleEvents.length && <div className="empty-large">尚无事件。开始研究或打开一个已有 Run，页面会按 DBOS 持久游标重放。</div>}</div></section>
        <div className="footer-line"><span>DBOS 为状态权威 · SSE 是只读回放 · 断线不表示结束</span><span>Research Graph Context Harness / OpenAI Agents SDK</span></div>
      </div>
    </main>
  </div>;
}
