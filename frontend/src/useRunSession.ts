import { useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, StudioApi } from './api';
import { RunObserver, type Connection } from './observer';
import type { RunEvent, RunRequest, RunSnapshot } from './protocol';
import { persist, restore, type PendingCreate, type PendingRetry } from './session';

/** Owns tab-local request identity and the subscriber; DBOS owns execution. */
export function useRunSession() {
  const [session, setSession] = useState(restore);
  const [run, setRun] = useState<RunSnapshot | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [token, setToken] = useState('');
  const tokenRef = useRef('');
  const [authRevision, setAuthRevision] = useState(0);
  const api = useMemo(() => new StudioApi(() => tokenRef.current), []);
  const [connection, setConnection] = useState<Connection | 'idle'>('idle');
  const [connectionError, setConnectionError] = useState('');
  const [message, setMessage] = useState('');
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const runId = session.runId;
  const observationKey = `${runId ?? ''}:${revision}:${authRevision}`;
  useEffect(() => {
    setEvents([]);
    setRun(null);
    setConnectionError('');
    if (!runId) {
      setConnection('idle');
      return;
    }
    const controller = new AbortController();
    const observer = new RunObserver(api, {
      events: setEvents,
      snapshot: setRun,
      connection: (state, error) => {
        setConnection(state);
        setConnectionError(error ?? '');
      },
    });
    void observer.observe(runId, controller.signal);
    return () => controller.abort();
  }, [api, runId, revision, authRevision]);

  function commit(next: typeof session) {
    persist(next);
    setSession(next);
  }
  function refresh() {
    setRevision((v) => v + 1);
  }
  function updateToken(value: string) {
    tokenRef.current = value;
    setToken(value);
  }
  function applyToken() {
    setAuthRevision((v) => v + 1);
  }
  function openRun(id: string): boolean {
    if (busyRef.current) return false;
    if (!/^[a-f0-9]{32}$/.test(id.trim())) {
      setMessage('请输入服务端返回的 32 位 Run ID');
      return false;
    }
    try {
      commit({ ...session, runId: id.trim() });
      setMessage('');
      refresh();
      return true;
    } catch {
      setMessage('无法保存浏览器会话，请检查会话存储权限');
      return false;
    }
  }
  async function createRun(request?: RunRequest): Promise<boolean> {
    if (busyRef.current) return false;
    busyRef.current = true;
    setBusy(true);
    setMessage('');
    let savedIdentity = false;
    try {
      const pending: PendingCreate = session.create ?? {
        key: crypto.randomUUID(),
        request: request!,
      };
      if (!pending.request) return false;
      commit({ ...session, create: pending });
      savedIdentity = true;
      const result = await api.create(pending.request, pending.key);
      commit({ ...session, create: undefined, runId: result.run_id });
      refresh();
      return true;
    } catch (error) {
      if (error instanceof ApiError && [400, 413, 422].includes(error.status)) {
        commit({ ...session, create: undefined });
        setMessage(`${error.message} · 请求被拒绝，可修改参数后再次创建。`);
      } else
        setMessage(
          savedIdentity
            ? `${(error as Error).message} · 创建请求身份已保留，可确认原请求。`
            : '浏览器会话存储不可用；请启用后再创建研究。',
        );
      return false;
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }
  async function command(action: 'cancel' | 'approval' | 'retry', decision?: 'approve' | 'reject') {
    if (!run || run.run_id !== runId || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setMessage('');
    try {
      let retry: PendingRetry | undefined;
      if (action === 'retry') {
        retry =
          session.retry?.runId === run.run_id
            ? session.retry
            : {
                key: crypto.randomUUID(),
                runId: run.run_id,
                state_version: run.state_version,
                generation: run.generation,
              };
        commit({ ...session, retry });
      }
      const result = await api.command(
        run.run_id,
        action,
        action === 'approval'
          ? { plan_version: run.plan?.version, decision }
          : action === 'retry'
            ? { state_version: retry!.state_version }
            : undefined,
        retry?.key,
      );
      if (action === 'retry') commit({ ...session, retry: undefined });
      setRun(result);
      refresh();
      setMessage(
        action === 'cancel'
          ? '取消请求已提交；继续观察 SDK 清算与归档。'
          : action === 'retry'
            ? '重试请求已确认；只有 durable Finalizer 可以应用授权。'
            : '审批决定已提交。',
      );
    } catch (error) {
      if (action === 'retry' && error instanceof ApiError && error.status < 500)
        commit({ ...session, retry: undefined });
      setMessage((error as Error).message);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }
  return {
    session,
    runId,
    run: run?.run_id === runId ? run : null,
    events: events.length === 0 || events[0].run_id === runId ? events : [],
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
    openRun,
    createRun,
    command,
  };
}
