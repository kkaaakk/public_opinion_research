import { lazy, Suspense, useEffect, useMemo, useState, type FormEvent } from 'react';
import {
  Activity,
  AlertCircle,
  ArrowUpRight,
  ChevronRight,
  Copy,
  Database,
  FileText,
  History as HistoryIcon,
  KeyRound,
  Menu,
  Plus,
  RefreshCw,
  RotateCcw,
  Square,
  Terminal,
  X,
} from 'lucide-react';
import type { Connection } from './observer';
import { useRunSession } from './useRunSession';
import { describeEvent, PHASES, projectRun, type PhaseId } from './projection';
import { TERMINAL, type RunEvent, type RunRequest } from './protocol';
import { Badge, count, duration, Empty, ErrorNote, Modal, time } from './components/common';
import { CreateDialog } from './components/RunDialogs';
import { WorkflowCanvas } from './components/WorkflowCanvas';
import { Inspector } from './components/Inspector';
import type { ReportOption } from './components/Reports';
import { Timeline } from './components/Timeline';
import { eventsForSDKFocus, preferredPhase, type SDKFocus } from './inspection';
import { sdkSpans } from './projection';
const SDKTrace = lazy(() => import('./components/SDKTrace').then((m) => ({ default: m.SDKTrace })));
const NativeTrace = lazy(() =>
  import('./components/NativeTrace').then((m) => ({ default: m.NativeTrace })),
);
const Evidence = lazy(() => import('./components/Evidence').then((m) => ({ default: m.Evidence })));
const Reports = lazy(() => import('./components/Reports').then((m) => ({ default: m.Reports })));
const History = lazy(() => import('./components/History').then((m) => ({ default: m.History })));
type Tab = 'workflow' | 'sdk' | 'dbos' | 'evidence' | 'reports';
const tabs: [Tab, string][] = [
  ['workflow', '工作流'],
  ['sdk', 'SDK 追踪'],
  ['dbos', 'DBOS'],
  ['evidence', '证据'],
  ['reports', '报告'],
];
const connectionLabels: Record<Connection | 'idle', string> = {
  idle: '未连接',
  loading: '加载持久事件',
  live: '实时订阅',
  reconnecting: '自动重连',
  paused: '审批等待 · 观察中',
  archiving: '已清算 · 归档补偿中',
  complete: '事件已交付',
  error: '订阅需处理',
};

export default function App() {
  const studio = useRunSession();
  const {
    session,
    runId,
    run,
    events,
    api,
    token,
    updateToken,
    applyToken,
    authRevision,
    connection,
    connectionError,
    setConnectionError,
    message,
    setMessage,
    busy,
    observationKey,
    refresh,
  } = studio;
  const [tab, setTab] = useState<Tab>('workflow');
  const [history, setHistory] = useState(false);
  const [settings, setSettings] = useState(false);
  const [create, setCreate] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);
  const [openId, setOpenId] = useState('');
  const [manualSelection, setManualSelection] = useState<{
    runId: string | undefined;
    phase: PhaseId;
  } | null>(null);
  const [sdkFocus, setSDKFocus] = useState<SDKFocus | null>(null);
  const [selectedEvent, setSelectedEvent] = useState<RunEvent | null>(null);
  const [replayCursor, setReplayCursor] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [now, setNow] = useState(Date.now);
  const maxCursor = events.at(-1)?.seq ?? 0;
  useEffect(() => {
    setSelectedEvent(null);
    setReplayCursor(null);
    setPlaying(false);
  }, [observationKey]);
  useEffect(() => {
    setSDKFocus(null);
  }, [runId]);
  useEffect(() => {
    if (!playing) return;
    const timer = setInterval(
      () =>
        setReplayCursor((old) => {
          const next = Math.min((old ?? 0) + 1, maxCursor);
          return next;
        }),
      400,
    );
    return () => clearInterval(timer);
  }, [playing, maxCursor]);
  useEffect(() => {
    if (playing && replayCursor !== null && replayCursor >= maxCursor) setPlaying(false);
  }, [playing, replayCursor, maxCursor]);
  useEffect(() => {
    if (!run || (TERMINAL.has(run.status) && run.settlement !== 'pending')) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [run]);
  function selectEvent(e: RunEvent) {
    setSelectedEvent((old) => (old?.seq === e.seq ? null : e));
  }
  function openRun(id: string) {
    if (studio.openRun(id)) {
      setHistory(false);
      setMobileNav(false);
    }
  }
  function submitOpen(e: FormEvent) {
    e.preventDefault();
    openRun(openId);
  }
  async function createRun(request?: RunRequest) {
    if (await studio.createRun(request)) {
      setCreate(false);
      setHistory(false);
      setTab('workflow');
    }
  }
  function command(action: 'cancel' | 'approval' | 'retry', decision?: 'approve' | 'reject') {
    if (replayCursor === null) return studio.command(action, decision);
  }
  const visibleEvents = useMemo(
    () => (replayCursor === null ? events : events.filter((e) => e.seq <= replayCursor)),
    [events, replayCursor],
  );
  const projection = useMemo(
    () => projectRun(visibleEvents, replayCursor === null ? run : null),
    [visibleEvents, run, replayCursor],
  );
  const selected =
    manualSelection && manualSelection.runId === runId
      ? manualSelection.phase
      : preferredPhase(projection, visibleEvents);
  function selectPhase(phase: PhaseId) {
    setManualSelection({ runId, phase });
    setSelectedEvent(null);
  }
  function openSDK(focus: SDKFocus) {
    const instance = projection.instances.find((i) => i.id === focus.executionId);
    if (instance) setManualSelection({ runId, phase: instance.phase });
    setSDKFocus(focus);
    setTab('sdk');
    const span = sdkSpans(eventsForSDKFocus(visibleEvents, focus)).find(
      (s) => focus.kind === 'all' || s.kind === focus.kind,
    );
    setSelectedEvent(span?.end ?? span?.start ?? null);
  }
  const reports = useMemo<ReportOption[]>(() => {
    const items: ReportOption[] = visibleEvents
      .filter((e) => e.type === 'agent_report' && e.content)
      .map((e) => ({
        id: `event-${e.seq}`,
        label: `${e.role ?? '角色'} · #${e.seq}`,
        content: e.content!,
      }));
    if (replayCursor === null && run) {
      for (const r of run.role_reports)
        if (!items.some((i) => i.content === r.content))
          items.push({
            id: r.stage_id ?? `stage-${items.length}`,
            label: r.role,
            content: r.content,
          });
    }
    const final =
      visibleEvents.findLast((e) => e.type === 'report')?.content ??
      (replayCursor === null ? run?.report : undefined);
    if (final) items.push({ id: 'final', label: '最终研究报告', content: final });
    return items;
  }, [visibleEvents, run, replayCursor]);
  const usage = projection.usage;
  const createdAt =
    replayCursor === null
      ? run?.created_at
      : (visibleEvents[0]?.observed_at ?? visibleEvents[0]?.timestamp);
  const endAt =
    replayCursor === null
      ? run && TERMINAL.has(run.status) && run.settlement !== 'pending'
        ? run.terminated_at
        : now
      : (visibleEvents.at(-1)?.observed_at ?? visibleEvents.at(-1)?.timestamp);
  const elapsed =
    createdAt && endAt
      ? Math.max(0, new Date(endAt).valueOf() - new Date(createdAt).valueOf())
      : undefined;
  const canCancel =
    run && ['queued', 'running', 'awaiting_approval', 'cancelling'].includes(run.status);
  const pendingRetry = session.retry?.runId === runId ? session.retry : undefined;
  const canRetry =
    run &&
    run.retry_safe &&
    run.events_complete &&
    run.settlement === 'settled' &&
    !run.retry_requested;
  const activePhase = PHASES.filter(
    (p) => projection.statuses[p.id] === 'running' || projection.statuses[p.id] === 'paused',
  )
    .map((p) => p.label)
    .join(' + ');
  return (
    <div className="app-shell">
      <aside className={`sidebar${mobileNav ? ' mobile-open' : ''}`}>
        <a className="brand" href="/studio">
          <span className="brand-icon">
            <Terminal size={23} />
          </span>
          <span>
            <strong>Execution Studio</strong>
            <small>Public Opinion Research</small>
          </span>
        </a>
        <button
          className="mobile-close icon-button"
          aria-label="关闭导航"
          onClick={() => setMobileNav(false)}
        >
          <X size={17} />
        </button>
        <nav aria-label="主导航">
          <button
            className={!history && tab !== 'reports' ? 'active' : ''}
            onClick={() => {
              setHistory(false);
              setTab('workflow');
              setMobileNav(false);
            }}
          >
            <Activity size={17} />
            运行观测
          </button>
          <button
            className={!history && tab === 'reports' ? 'active' : ''}
            onClick={() => {
              setHistory(false);
              setTab('reports');
              setMobileNav(false);
            }}
          >
            <FileText size={17} />
            研究报告
          </button>
          <button
            className={history ? 'active' : ''}
            onClick={() => {
              setHistory(true);
              setMobileNav(false);
            }}
          >
            <HistoryIcon size={17} />
            历史归档
          </button>
        </nav>
        <div className="sidebar-controls">
          <form onSubmit={submitOpen}>
            <label htmlFor="open-run">打开持久 Run</label>
            <input
              id="open-run"
              value={openId}
              onChange={(e) => setOpenId(e.target.value)}
              maxLength={32}
              placeholder="Run ID"
              spellCheck={false}
            />
            <button className="button" disabled={busy || !openId.trim()}>
              打开 Run
              <ChevronRight size={14} />
            </button>
          </form>
          <button
            className="button primary"
            disabled={busy || !!session.create}
            onClick={() => setCreate(true)}
          >
            <Plus size={16} />
            新建研究
          </button>
        </div>
        {runId && (
          <div className="sidebar-run">
            <span className="type-label">当前 Run</span>
            <button
              className="text-button mono"
              onClick={() => {
                setHistory(false);
                setTab('workflow');
              }}
            >
              {runId.slice(0, 12)}…
            </button>
            <small>{run?.request?.topic ?? '读取运行事实'}</small>
          </div>
        )}
        <footer>
          <div>
            <Database size={15} />
            <span>
              DBOS 3.3.0
              <br />
              Agents SDK 0.24.0
            </span>
          </div>
          <a href="/classic">
            <ArrowUpRight size={14} />
            经典页面 (Classic)
          </a>
        </footer>
      </aside>
      <main className="workspace">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="icon-button mobile-menu"
              aria-label="打开导航"
              onClick={() => setMobileNav(true)}
            >
              <Menu size={18} />
            </button>
            <span>工作空间</span>
            <ChevronRight size={13} />
            <strong>{history ? '历史归档' : '运行观测'}</strong>
          </div>
          <button className="text-button" onClick={() => setSettings(true)}>
            <KeyRound size={15} />
            访问设置
            <span className={`auth-dot${token ? ' configured' : ''}`} />
          </button>
        </header>
        <div className="workspace-content">
          <div className="workspace-title">
            <div>
              <h1>{history ? '研究历史' : '研究执行观测台'}</h1>
              <p>
                DBOS <span>→</span> OpenAI Agents SDK <span>→</span> Context Harness
              </p>
            </div>
            <span className={`connection connection-${connection}`}>
              <i />
              {connectionLabels[connection]}
            </span>
          </div>
          {(message || connectionError) && (
            <div className="notice" role="status">
              <AlertCircle size={16} />
              <span>{message || connectionError}</span>
              {connection === 'error' && (
                <button className="text-button" onClick={() => setSettings(true)}>
                  访问设置
                </button>
              )}
              <button
                className="icon-button"
                aria-label="关闭提示"
                onClick={() => {
                  setMessage('');
                  setConnectionError('');
                }}
              >
                <X size={14} />
              </button>
            </div>
          )}
          {session.create && (
            <div className="notice">
              <span>存在未确认的创建请求；重发使用原幂等键与原参数。</span>
              <button className="button small" disabled={busy} onClick={() => void createRun()}>
                确认原创建请求
              </button>
            </div>
          )}
          <Suspense fallback={<Empty>加载运行视图…</Empty>}>
            {history ? (
              <History key={authRevision} api={api} onOpen={openRun} />
            ) : (
              <>
                <section className="run-overview" aria-label="运行概览">
                  <div className="run-identity">
                    <span>Run ID</span>
                    <strong className="mono">{runId ?? '未选择'}</strong>
                    {runId && (
                      <button
                        className="icon-button"
                        aria-label="复制 Run ID"
                        onClick={() =>
                          void navigator.clipboard
                            .writeText(runId)
                            .then(() => setMessage('Run ID 已复制'))
                            .catch(() => setMessage('浏览器不允许复制'))
                        }
                      >
                        <Copy size={13} />
                      </button>
                    )}
                  </div>
                  <div>
                    <span>状态{replayCursor !== null ? ' · 回放' : ''}</span>
                    <Badge status={projection.status} />
                  </div>
                  <div>
                    <span>模型调用</span>
                    <strong>{count(usage?.model_calls)}</strong>
                  </div>
                  <div>
                    <span>Input / Output Tokens</span>
                    <strong>
                      {count(usage?.input_tokens)} <small>/</small> {count(usage?.output_tokens)}
                    </strong>
                  </div>
                  <div>
                    <span>Total Tokens</span>
                    <strong>{count(usage?.total_tokens)}</strong>
                  </div>
                  <div>
                    <span>运行耗时</span>
                    <strong>{duration(elapsed)}</strong>
                  </div>
                  <div>
                    <span>清算</span>
                    <Badge status={projection.settlement} />
                  </div>
                </section>
                <div className="run-context">
                  <span>
                    {activePhase ||
                      (runId ? (run?.request?.topic ?? '加载运行事实') : '选择已有 Run 或创建研究')}
                  </span>
                  <div>
                    <span className="mono">
                      Gen {projection.generation} · v
                      {replayCursor === null ? (run?.state_version ?? '—') : '—'}
                    </span>
                    <button className="text-button" disabled={!runId || busy} onClick={refresh}>
                      <RefreshCw size={13} />
                      重新读取
                    </button>
                    {canCancel && (
                      <button
                        className="button small danger"
                        disabled={busy || replayCursor !== null || run?.status === 'cancelling'}
                        onClick={() => void command('cancel')}
                      >
                        <Square size={12} />
                        取消
                      </button>
                    )}
                    {(canRetry || pendingRetry) && (
                      <button
                        className="button small"
                        disabled={busy || replayCursor !== null}
                        onClick={() => void command('retry')}
                      >
                        <RotateCcw size={13} />
                        {pendingRetry ? '确认原重试请求' : '安全重试'}
                      </button>
                    )}
                  </div>
                </div>
                {replayCursor === null && run?.usage && run.error.usage_complete === false && (
                  <p className="warning-note">用量尚不完整：存在未确认的外部效果或清算。</p>
                )}
                {replayCursor === null && run?.settlement === 'pending' && (
                  <div className="notice">
                    SDK / 工具仍在清算。即使 DBOS 已进入终态，订阅仍继续。
                  </div>
                )}
                {replayCursor === null && run?.status === 'interrupted' && (
                  <div className="notice danger">
                    执行中断或外部效果未知。请按服务端权威核对流程处理。
                  </div>
                )}
                {replayCursor === null && run && Object.keys(run.error).length > 0 && (
                  <p className="warning-note">
                    错误位置：{String(run.error.step ?? run.current_step ?? '未提供')} ·{' '}
                    {String(run.error.kind ?? '未提供')} · Archive {run.archive_status ?? '未提供'}
                  </p>
                )}
                {replayCursor !== null && (
                  <div className="replay-banner">
                    <span>
                      只读历史回放 · #{replayCursor} / #{maxCursor} ·{' '}
                      {time(visibleEvents.at(-1)?.observed_at ?? visibleEvents.at(-1)?.timestamp)}
                    </span>
                    <button
                      className="text-button"
                      onClick={() => {
                        setReplayCursor(null);
                        setPlaying(false);
                      }}
                    >
                      返回实时状态
                    </button>
                  </div>
                )}
                {run?.status === 'awaiting_approval' && run.plan && replayCursor === null && (
                  <section className="panel approval">
                    <div className="panel-head">
                      <strong>研究计划待审批</strong>
                      <span className="mono">{run.plan.version.slice(0, 12)}</span>
                    </div>
                    <div className="approval-body">
                      <p>{run.plan.brief}</p>
                      <ol>
                        {run.plan.sections.map((s, i) => (
                          <li key={i}>{s.name}</li>
                        ))}
                      </ol>
                      <small>到期：{time(run.plan.expires_at)}</small>
                      <div className="form-actions">
                        <button
                          className="button"
                          disabled={busy}
                          onClick={() => void command('approval', 'reject')}
                        >
                          拒绝计划
                        </button>
                        <button
                          className="button primary"
                          disabled={busy}
                          onClick={() => void command('approval', 'approve')}
                        >
                          批准执行
                        </button>
                      </div>
                    </div>
                  </section>
                )}
                <div className="tabs" role="tablist" aria-label="运行视图">
                  {tabs.map(([id, label]) => (
                    <button
                      key={id}
                      role="tab"
                      aria-selected={tab === id}
                      className={tab === id ? 'active' : ''}
                      onClick={() => {
                        setTab(id);
                        setSelectedEvent(null);
                        if (id === 'sdk') setSDKFocus(null);
                      }}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                {tab === 'workflow' && (
                  <div className="studio-grid">
                    <WorkflowCanvas
                      projection={projection}
                      selected={selected}
                      onSelect={selectPhase}
                    />
                    <Inspector
                      selected={selected}
                      event={selectedEvent}
                      events={visibleEvents}
                      projection={projection}
                      onEvent={selectEvent}
                      onSDK={openSDK}
                    />
                  </div>
                )}
                {tab === 'sdk' && (
                  <>
                    <SDKTrace
                      key={`${runId}:${sdkFocus ? JSON.stringify(sdkFocus) : 'all'}`}
                      events={visibleEvents}
                      onEvent={selectEvent}
                      focus={sdkFocus}
                      onClearFocus={() => {
                        setSDKFocus(null);
                        setSelectedEvent(null);
                      }}
                    />
                    {selectedEvent && (
                      <details className="panel selected-detail" open>
                        <summary>
                          {describeEvent(selectedEvent)} · #{selectedEvent.seq}
                        </summary>
                        <Inspector
                          selected={null}
                          event={selectedEvent}
                          events={visibleEvents}
                          projection={projection}
                          onEvent={selectEvent}
                        />
                      </details>
                    )}
                  </>
                )}
                {tab === 'dbos' &&
                  (runId ? (
                    <>
                      <p className="note">
                        DBOS 当前快照：{run?.native_status ?? '未提供'} · 清算{' '}
                        {run?.settlement ?? '未提供'}。
                        {replayCursor !== null &&
                          '本页展示当前原生日志，不代表回放游标处的历史快照。'}
                      </p>
                      <NativeTrace key={`${runId}:${authRevision}`} api={api} runId={runId} />
                    </>
                  ) : (
                    <Empty>打开 Run 后读取 DBOS Journal</Empty>
                  ))}
                {tab === 'evidence' &&
                  (runId ? (
                    <Evidence
                      key={`${runId}:${authRevision}`}
                      api={api}
                      runId={runId}
                      replay={replayCursor !== null}
                    />
                  ) : (
                    <Empty>打开 Run 后查看持久研究证据</Empty>
                  ))}
                {tab === 'reports' && <Reports reports={reports} />}
                <Timeline
                  events={visibleEvents}
                  cursor={replayCursor}
                  maxCursor={maxCursor}
                  playing={playing}
                  onCursor={(v) => {
                    setReplayCursor(v);
                    setSelectedEvent(null);
                  }}
                  onPlaying={setPlaying}
                  selected={selectedEvent}
                  onEvent={selectEvent}
                />
                <p className="workspace-footnote">
                  {run?.workflow_version ?? 'Workflow version 未提供'} ·{' '}
                  {run?.runtime_version ?? 'sdk-phase5-v1'} · 费用未提供，未计算估算账单。
                  {runId && ` 当前归档：${run?.archive_status ?? '未提供'}`}
                </p>
              </>
            )}
          </Suspense>
        </div>
      </main>
      {create && (
        <CreateDialog
          busy={busy}
          onClose={() => setCreate(false)}
          onCreate={(request) => void createRun(request)}
        />
      )}
      {settings && (
        <Modal title="访问设置" onClose={() => setSettings(false)}>
          <div className="research-form">
            <label>
              API 访问令牌
              <input
                autoFocus
                type="password"
                autoComplete="off"
                value={token}
                onChange={(e) => {
                  updateToken(e.target.value);
                }}
                placeholder="Bearer token"
              />
            </label>
            <p className="note">
              令牌仅保留在当前页面内存，经 Authorization Header 传递。刷新后需重新输入。不会写入
              URL、会话存储或事件。
            </p>
            <ErrorNote error={connectionError} />
            <button
              className="button primary"
              onClick={() => {
                applyToken();
                setSettings(false);
              }}
            >
              应用并连接
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}
