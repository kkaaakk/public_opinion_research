import { useEffect, useRef, useState } from 'react';
import { useApi } from '@/api/auth';
import { ApiError, TERMINAL, type Run, type RunCommand } from '@/api/client';
import type { RunEvent } from '@/api/events';
import { newKey } from '@/lib/presentation';

type Pending = {
  command: RunCommand;
  key?: string;
  generation: number;
  after: number;
  previousError?: string | null;
  state: 'uncertain' | 'accepted';
};

function stored(storage: string): Pending | null {
  try {
    return JSON.parse(sessionStorage.getItem(storage) ?? 'null');
  } catch {
    return null;
  }
}

function acknowledged(run: Run, events: RunEvent[], pending: Pending) {
  const { command } = pending;
  if (run.generation > pending.generation) return true;
  if (command.action === 'cancel') return run.cancel_requested || TERMINAL.has(run.status);
  if (command.action === 'retry') return run.retry_requested;
  if (command.action === 'clarification') {
    return (
      run.status !== 'awaiting_clarification' || run.clarification?.version !== command.body.version
    );
  }
  const plan = run.plan;
  if (!plan || plan.version !== command.body.version || plan.locked || TERMINAL.has(run.status))
    return true;
  if (command.body.decision !== 'revise') return run.status !== 'awaiting_approval';
  if (run.status !== 'awaiting_approval' || plan.ai_revision?.pending) return false;
  if (plan.ai_revision?.error && plan.ai_revision.error !== pending.previousError) return true;
  const started = events.find(
    (event) =>
      event.seq > pending.after && event.step === 'revise_brief' && event.type === 'step_started',
  );
  return (
    started !== undefined &&
    events.some(
      (event) =>
        event.seq > started.seq &&
        event.type === 'run_status' &&
        event.status === 'awaiting_approval' &&
        event.plan_version === command.body.version,
    )
  );
}

function available(run: Run, command: RunCommand) {
  if (command.action === 'cancel') return run.can_cancel;
  if (command.action === 'retry')
    return run.retry_safe && run.state_version === command.body.state_version;
  if (command.action === 'clarification') {
    return run.can_clarify && run.clarification?.version === command.body.version;
  }
  return (
    run.can_approve &&
    run.plan?.version === command.body.version &&
    !run.plan.locked &&
    !run.plan.ai_revision?.pending
  );
}

export function useRunCommands(
  id: string,
  run: Run | undefined,
  events: RunEvent[],
  refresh: () => void,
) {
  const { api } = useApi();
  const storage = 'por-control-' + id;
  const [pending, setPending] = useState(() => stored(storage));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const sending = useRef(false);
  const resolved = Boolean(run && pending && acknowledged(run, events, pending));
  const locked = busy || Boolean(pending && !resolved);
  useEffect(() => {
    if (resolved) {
      try {
        sessionStorage.removeItem(storage);
      } catch {
        /* The current view can still clear state. */
      }
      setPending(null);
    }
  }, [resolved, storage]);

  function save(value: Pending | null, requireStorage = false) {
    try {
      if (value) sessionStorage.setItem(storage, JSON.stringify(value));
      else sessionStorage.removeItem(storage);
    } catch {
      if (requireStorage) throw new Error('浏览器无法保留请求身份。');
    }
    setPending(value);
  }

  async function send(value: Pending) {
    if (!run || !available(run, value.command) || sending.current) return;
    sending.current = true;
    setBusy(true);
    setError(undefined);
    try {
      save(value, Boolean(value.key));
      await api.command(id, value.command, value.key);
      save({ ...value, state: 'accepted' });
    } catch (failure) {
      if (failure instanceof ApiError && [400, 409, 413, 415, 422].includes(failure.status)) {
        save(null);
        setError(
          failure.status === 409 && value.command.action !== 'retry'
            ? '运行状态或交互版本已变化，正在重新读取。'
            : failure,
        );
      } else {
        setError(failure);
      }
    } finally {
      sending.current = false;
      setBusy(false);
      refresh();
    }
  }

  function submit(command: RunCommand) {
    if (!run || locked) return;
    const needsKey =
      command.action === 'retry' ||
      (command.action === 'approval' && command.body.decision === 'revise');
    void send({
      command,
      key: needsKey ? newKey() : undefined,
      generation: run.generation,
      after: Math.max(run.event_cursor, events.at(-1)?.seq ?? 0),
      previousError: run.plan?.ai_revision?.error,
      state: 'uncertain',
    });
  }

  return {
    submit,
    busy,
    locked,
    error,
    pending: resolved ? null : pending,
    resend:
      !busy && pending?.state === 'uncertain' && run && !resolved && available(run, pending.command)
        ? () => void send(pending)
        : undefined,
  };
}
