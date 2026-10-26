import { MousePointer2, ScanLine, Terminal } from 'lucide-react';
import {
  describeEvent,
  measuredDuration,
  phaseForEvent,
  PHASES,
  type PhaseId,
  type Projection,
} from '../projection';
import type { RunEvent } from '../protocol';
import { Badge, duration, Empty, JsonDetail, time } from './common';

export function Inspector({
  selected,
  event,
  events,
  projection,
  onEvent,
}: {
  selected: PhaseId | null;
  event: RunEvent | null;
  events: RunEvent[];
  projection: Projection;
  onEvent: (e: RunEvent) => void;
}) {
  const phase = PHASES.find((p) => p.id === selected);
  const instances = projection.instances.filter((i) => i.phase === selected);
  const related = events.filter((e) => phaseForEvent(e) === selected);
  return (
    <aside className={`panel inspector${!phase && !event ? ' empty-inspector' : ''}`}>
      <div className="panel-head">
        <strong>
          <ScanLine size={16} />
          {event ? `事件 #${event.seq}` : '节点检查器'}
        </strong>
      </div>
      <div className="inspector-body">
        {event ? (
          <>
            <h3>{describeEvent(event)}</h3>
            <dl className="facts">
              <dt>Run</dt>
              <dd className="mono">{event.run_id}</dd>
              <dt>记录时间</dt>
              <dd>{time(event.timestamp)}</dd>
              <dt>观测时间</dt>
              <dd>{time(event.observed_at)}</dd>
              <dt>Step / Agent</dt>
              <dd>
                {event.step ?? '未提供'} / {event.role ?? '未提供'}
              </dd>
              <dt>Invocation</dt>
              <dd className="mono">{event.invocation_id ?? '未提供'}</dd>
              <dt>耗时</dt>
              <dd>{duration(event.duration_ms)}</dd>
            </dl>
            <details open>
              <summary>结构化公开事件</summary>
              <JsonDetail value={event} />
            </details>
          </>
        ) : phase ? (
          <>
            <div className="section-heading">
              <h3>{phase.label}</h3>
              <Badge status={projection.statuses[phase.id]} />
            </div>
            <p className="muted">
              {phase.detail} · {instances.length} 个执行实例
            </p>
            <div className="instance-list">
              {instances.map((i, index) => (
                <button
                  key={i.id}
                  className="instance"
                  onClick={() => (i.end ?? i.start) && onEvent((i.end ?? i.start)!)}
                >
                  <div>
                    <strong>{i.step}</strong>
                    <Badge status={i.status} />
                  </div>
                  <small>
                    Instance {index + 1} · Gen {i.generation} · Round {i.round ?? '未提供'}
                  </small>
                  <small className="mono">{i.unitId ?? 'Unit ID 未提供'}</small>
                  <small>
                    {duration(measuredDuration(i.start, i.end))} · {time(i.start?.observed_at)}
                  </small>
                </button>
              ))}
            </div>
            {!instances.length && <Empty>尚未观察到此节点的执行实例。</Empty>}
            <h4>
              <Terminal size={14} />
              关联公开事件
            </h4>
            {related.slice(-12).map((e) => (
              <button className="event-link" key={e.seq} onClick={() => onEvent(e)}>
                <span className="mono">#{e.seq}</span>
                {describeEvent(e)}
              </button>
            ))}
            <p className="note">
              模型与工具调用详见「SDK 追踪」。Prompt、工具参数、原始响应未提供。
            </p>
          </>
        ) : (
          <>
            <Empty>
              <MousePointer2 size={28} />
              <span>选择节点查看真实执行实例</span>
            </Empty>
            <dl className="facts">
              <dt>Step</dt>
              <dd>未提供</dd>
              <dt>Agent</dt>
              <dd>未提供</dd>
              <dt>Invocation</dt>
              <dd>未提供</dd>
              <dt>Duration</dt>
              <dd>未提供</dd>
            </dl>
          </>
        )}
      </div>
    </aside>
  );
}
