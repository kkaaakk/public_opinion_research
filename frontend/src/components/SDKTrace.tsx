import { useMemo, useState } from 'react';
import { Boxes, Cpu, Wrench } from 'lucide-react';
import { describeEvent, measuredDuration, ROLE_LABELS, sdkSpans } from '../projection';
import type { RunEvent } from '../protocol';
import { Badge, count, duration, Empty } from './common';

export function SDKTrace({
  events,
  onEvent,
}: {
  events: RunEvent[];
  onEvent: (e: RunEvent) => void;
}) {
  const [role, setRole] = useState('');
  const [invocation, setInvocation] = useState('');
  const selected = useMemo(
    () =>
      events.filter(
        (e) => (!role || e.role === role) && (!invocation || e.invocation_id === invocation),
      ),
    [events, role, invocation],
  );
  const spans = useMemo(() => sdkSpans(selected), [selected]);
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
            onChange={(e) => setInvocation(e.target.value)}
          >
            <option value="">所有 Invocation</option>
            {invocations.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </div>
      </div>
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
