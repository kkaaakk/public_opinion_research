import { Fragment, useMemo, useState } from 'react';
import {
  ChevronLeft,
  ChevronRight,
  ListFilter,
  Pause,
  Play,
  Radio,
  SkipBack,
  SkipForward,
} from 'lucide-react';
import { describeEvent, eventStatus, ROLE_LABELS } from '../projection';
import type { RunEvent } from '../protocol';
import { Badge, Empty, JsonDetail, time } from './common';

export function Timeline({
  events,
  cursor,
  maxCursor,
  playing,
  onCursor,
  onPlaying,
  selected,
  onEvent,
}: {
  events: RunEvent[];
  cursor: number | null;
  maxCursor: number;
  playing: boolean;
  onCursor: (value: number | null) => void;
  onPlaying: (value: boolean) => void;
  selected: RunEvent | null;
  onEvent: (e: RunEvent) => void;
}) {
  const [filters, setFilters] = useState({ role: '', step: '', type: '', status: '' });
  const [page, setPage] = useState(-1);
  const filtered = useMemo(
    () =>
      events.filter(
        (e) =>
          (!filters.role || e.role === filters.role) &&
          (!filters.step || e.step === filters.step) &&
          (!filters.type || `${e.type}${e.event ? `/${e.event}` : ''}` === filters.type) &&
          (!filters.status || eventStatus(e) === filters.status),
      ),
    [events, filters],
  );
  const pages = Math.max(1, Math.ceil(filtered.length / 40));
  const current = page < 0 ? pages - 1 : Math.min(page, pages - 1);
  const options = {
    role: [...new Set(events.map((e) => e.role).filter(Boolean))],
    step: [...new Set(events.map((e) => e.step).filter(Boolean))],
    type: [...new Set(events.map((e) => `${e.type}${e.event ? `/${e.event}` : ''}`))],
    status: [...new Set(events.map(eventStatus).filter(Boolean))],
  };
  for (const key of ['role', 'step', 'type', 'status'] as const) {
    if (filters[key] && !options[key].includes(filters[key])) options[key].push(filters[key]);
  }
  return (
    <section className="panel timeline">
      <div className="panel-head">
        <strong>
          <ListFilter size={16} />
          事件时间线 <span className="muted">{filtered.length} 条</span>
        </strong>
        <span className="muted">原生 DBOS Stream · 连续序号</span>
      </div>
      <div className="timeline-toolbar">
        <div className="filters">
          {(['role', 'step', 'type', 'status'] as const).map((key, index) => (
            <label key={key}>
              {['Agent', '步骤', '事件', '状态'][index]}
              <select
                aria-label={`时间线${key}`}
                value={filters[key]}
                onChange={(e) => {
                  setFilters({ ...filters, [key]: e.target.value });
                  setPage(-1);
                }}
              >
                <option value="">全部</option>
                {options[key].map((v) => (
                  <option key={v} value={v}>
                    {ROLE_LABELS[v ?? ''] ?? v}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </div>
        <div className="replay-controls">
          <button
            className="icon-button"
            aria-label="回放开始"
            disabled={!maxCursor}
            onClick={() => {
              onPlaying(false);
              onCursor(0);
            }}
          >
            <SkipBack size={14} />
          </button>
          <button
            className="icon-button"
            aria-label={playing ? '暂停回放' : '播放回放'}
            disabled={!maxCursor}
            onClick={() => {
              if (!playing && (cursor === null || cursor >= maxCursor)) onCursor(0);
              onPlaying(!playing);
            }}
          >
            {playing ? <Pause size={14} /> : <Play size={14} />}
          </button>
          <button
            className="icon-button"
            aria-label="下一事件"
            disabled={!maxCursor || cursor === null || cursor >= maxCursor}
            onClick={() => {
              onPlaying(false);
              onCursor(Math.min((cursor ?? 0) + 1, maxCursor));
            }}
          >
            <SkipForward size={14} />
          </button>
          <label className="mono">
            #{cursor ?? maxCursor}
            <input
              aria-label="回放游标"
              type="range"
              min={0}
              max={maxCursor}
              value={cursor ?? maxCursor}
              disabled={!maxCursor}
              onChange={(e) => {
                onPlaying(false);
                onCursor(Number(e.target.value));
              }}
            />
          </label>
          <button
            className={`text-button ${cursor === null ? 'accent' : ''}`}
            onClick={() => {
              onPlaying(false);
              onCursor(null);
            }}
          >
            <Radio size={14} />
            实时
          </button>
        </div>
      </div>
      <div className="table-scroll">
        <table className="event-table">
          <thead>
            <tr>
              <th>序号 / 代次</th>
              <th>观测 / 记录时间</th>
              <th>事件</th>
              <th>步骤 / Agent</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            {filtered.slice(current * 40, current * 40 + 40).map((e) => (
              <Fragment key={e.seq}>
                <tr className={selected?.seq === e.seq ? 'chosen' : ''}>
                  <td className="mono">
                    #{e.seq}
                    <small>Gen {e.generation ?? '未提供'}</small>
                  </td>
                  <td>
                    <small>{time(e.observed_at ?? e.timestamp)}</small>
                    {!e.observed_at && <small className="muted">relay 记录时间</small>}
                  </td>
                  <td>
                    <button className="event-open" onClick={() => onEvent(e)}>
                      {describeEvent(e)}
                    </button>
                  </td>
                  <td>
                    <small>{e.step ?? '—'}</small>
                    <small>{ROLE_LABELS[e.role ?? ''] ?? e.role ?? '—'}</small>
                  </td>
                  <td>
                    {eventStatus(e) ? (
                      <Badge status={eventStatus(e)} />
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                </tr>
                {selected?.seq === e.seq && (
                  <tr className="event-expanded">
                    <td colSpan={5}>
                      <JsonDetail value={e} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      {!filtered.length && (
        <Empty>{events.length ? '没有符合筛选条件的事件' : '打开或创建研究以查看持久事件'}</Empty>
      )}
      <div className="pagination">
        <span>
          {filtered.length
            ? `${current * 40 + 1}–${Math.min((current + 1) * 40, filtered.length)} / ${filtered.length}`
            : '0 / 0'}
        </span>
        <div>
          <button
            className="icon-button"
            aria-label="上一页事件"
            disabled={!current}
            onClick={() => setPage(current - 1)}
          >
            <ChevronLeft size={14} />
          </button>
          <span>
            {current + 1} / {pages}
          </span>
          <button
            className="icon-button"
            aria-label="下一页事件"
            disabled={current >= pages - 1}
            onClick={() => setPage(current + 1)}
          >
            <ChevronRight size={14} />
          </button>
          <button className="text-button" onClick={() => setPage(-1)}>
            最新页
          </button>
        </div>
      </div>
    </section>
  );
}
