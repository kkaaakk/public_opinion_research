import { useMemo, useState } from 'react';
import { Boxes, Cpu, Wrench } from 'lucide-react';
import {
  describeEvent,
  measuredDuration,
  ROLE_LABELS,
  sdkSpans,
  STATUS_LABELS,
} from '../projection';
import type { RunEvent } from '../protocol';
import { Badge, count, duration, Empty } from './common';
import { eventsForSDKFocus, type SDKFocus } from '../inspection';

export function SDKTrace({
  events,
  onEvent,
  focus,
  onClearFocus,
}: {
  events: RunEvent[];
  onEvent: (e: RunEvent) => void;
  focus?: SDKFocus | null;
  onClearFocus?: () => void;
}) {
  const [role, setRole] = useState(focus?.role ?? '');
  const [invocation, setInvocation] = useState(focus?.invocationId ?? '');
  const [kind, setKind] = useState<'all' | 'model' | 'tool'>(focus?.kind ?? 'all');
  const scopedEvents = useMemo(
    () => (focus ? eventsForSDKFocus(events, focus) : events),
    [events, focus],
  );
  const selected = useMemo(
    () =>
      scopedEvents.filter(
        (e) => (!role || e.role === role) && (!invocation || e.invocation_id === invocation),
      ),
    [scopedEvents, role, invocation],
  );
  const spans = useMemo(
    () => sdkSpans(selected).filter((s) => kind === 'all' || s.kind === kind),
    [selected, kind],
  );
  const invocations = [
    ...new Set(
      events
        .filter((e) => e.invocation_id && (!role || e.role === role))
        .map((e) => e.invocation_id!),
    ),
  ];
  if (invocation && !invocations.includes(invocation)) invocations.push(invocation);
  const lifecycle = selected.filter(
    (e) =>
      e.type === 'sdk_event' &&
      ['session_started', 'agent_started', 'agent_completed', 'agent_failed', 'harness'].includes(
        e.event ?? '',
      ),
  );
  return (
    <section className="panel trace-panel">
      <div className="panel-head">
        <strong>
          <Boxes size={16} />
          SDK 内部执行
        </strong>
        <div className="filters">
          <select
            aria-label="追踪 Agent"
            value={role}
            disabled={!!focus}
            onChange={(e) => {
              setRole(e.target.value);
              setInvocation('');
            }}
          >
            <option value="">所有 Agent</option>
            {Object.entries(ROLE_LABELS).map(([v, label]) => (
              <option key={v} value={v}>
                {label}
              </option>
            ))}
          </select>
          <select
            aria-label="追踪 Invocation"
            value={invocation}
            disabled={!!focus}
            onChange={(e) => setInvocation(e.target.value)}
          >
            <option value="">所有 Invocation</option>
            {invocations.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
          <select
            aria-label="调用类型"
            value={kind}
            onChange={(e) => setKind(e.target.value as typeof kind)}
          >
            <option value="all">全部调用</option>
            <option value="model">模型调用</option>
            <option value="tool">工具调用</option>
          </select>
        </div>
      </div>
      {focus && (
        <div className="trace-focus">
          <span>
            定位到真实执行实例 · Gen {focus.generation ?? '未提供'}
            <small className="mono">
              Invocation {focus.invocationId}
              <br />
              Execution {focus.executionId}
            </small>
          </span>
          <button className="button small" onClick={onClearFocus} disabled={!onClearFocus}>
            显示全部追踪
          </button>
        </div>
      )}
      <p className="panel-note">
        按原始序号展示 SDK Loop。工具效果回执与 Harness 业务结果分别记录。单次模型用量不含
        Graph、压缩与报告等有界业务服务。
      </p>
      <div className="trace-grid">
        <div className="spans">
          {spans.length ? (
            spans.map((span) => (
              <button
                key={span.id}
                className="span-row"
                onClick={() => onEvent(span.end ?? span.start!)}
              >
                <span className={`span-icon ${span.kind}`}>
                  {span.kind === 'model' ? <Cpu size={17} /> : <Wrench size={17} />}
                </span>
                <div className="span-main">
                  <strong>{span.label}</strong>
                  <small>
                    {ROLE_LABELS[span.role ?? ''] ?? span.role ?? 'Agent 未提供'} ·{' '}
                    <span className="mono">
                      {span.invocation?.slice(0, 12) ?? 'Invocation 未提供'}
                    </span>
                  </small>
                  {span.kind === 'tool' && (
                    <small
                      className={
                        span.events.some((e) => e.event === 'tool' && e.status === 'uncertain')
                          ? 'warning-note'
                          : ''
                      }
                    >
                      效果回执 ·{' '}
                      {STATUS_LABELS[
                        span.events.findLast((e) => e.event === 'tool')?.status ?? ''
                      ] ??
                        span.events.findLast((e) => e.event === 'tool')?.status ??
                        '未提供'}
                    </small>
                  )}
                  <small className="mono">
                    #{span.start?.seq ?? '—'} → #{span.end?.seq ?? '—'}
                  </small>
                </div>
                <div className="span-measure">
                  <Badge status={span.status} />
                  <small>{duration(measuredDuration(span.start, span.end))}</small>
                  <small>
                    {span.kind === 'model'
                      ? `${count(span.usage?.input_tokens)} in / ${count(span.usage?.output_tokens)} out`
                      : span.end?.status === 'returned'
                        ? '业务结果未提供'
                        : `${span.events.length} 条回执`}
                  </small>
                </div>
              </button>
            ))
          ) : (
            <Empty>没有已发布的模型或工具追踪。旧事件可能只有阶段元数据。</Empty>
          )}
        </div>
        <div className="lifecycle">
          <h4>Agent / Context Harness</h4>
          {lifecycle.map((e) => (
            <button key={e.seq} className="lifecycle-row" onClick={() => onEvent(e)}>
              <span className="mono">#{e.seq}</span>
              <span>
                {describeEvent(e)}
                <small>
                  {ROLE_LABELS[e.role ?? ''] ?? e.role} · {e.status ?? e.reason ?? ''}
                </small>
              </span>
            </button>
          ))}
          {!lifecycle.length && <Empty>尚无生命周期事件</Empty>}
        </div>
      </div>
    </section>
  );
}
