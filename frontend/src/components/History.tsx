import { useEffect, useState } from 'react';
import { History as HistoryIcon } from 'lucide-react';
import { HISTORY_PAGE_SIZE, type StudioApi } from '../api';
import type { HistoryPage, RunSnapshot } from '../protocol';
import { Badge, Empty, ErrorNote, time } from './common';
import { Reports } from './Reports';
export function History({ api, onOpen }: { api: StudioApi; onOpen: (id: string) => void }) {
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [page, setPage] = useState<HistoryPage | null>(null);
  const [selected, setSelected] = useState('');
  const [run, setRun] = useState<RunSnapshot | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    setError('');
    void api
      .history(offset, controller.signal)
      .then((p) => {
        if (!controller.signal.aborted) setPage(p);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError((e as Error).message);
      });
    return () => controller.abort();
  }, [api, offset, revision]);
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    setRun(null);
    void api
      .historyDetail(selected, controller.signal)
      .then((p) => {
        if (!controller.signal.aborted) setRun(p);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError((e as Error).message);
      });
    return () => controller.abort();
  }, [api, selected]);
  return (
    <>
      <section className="panel history-panel">
        <div className="panel-head">
          <strong>
            <HistoryIcon size={16} />
            历史报告
          </strong>
          <button className="button" onClick={() => setRevision((v) => v + 1)}>
            刷新
          </button>
        </div>
        <ErrorNote error={error} />
        <div className="history-list">
          {page?.items.map((item) => (
            <button
              className={`history-item${selected === item.run_id ? ' chosen' : ''}`}
              key={item.run_id}
              onClick={() => setSelected(item.run_id)}
            >
              <div>
                <strong>{item.topic}</strong>
                <Badge status={item.status} />
              </div>
              <small>{time(item.completed_at)}</small>
              <small className="mono">Run {item.run_id}</small>
            </button>
          ))}
        </div>
        {page && !page.items.length && <Empty>本页暂无研究记录</Empty>}
        <div className="pagination">
          <button
            className="text-button"
            disabled={!offset}
            onClick={() => setOffset(Math.max(0, offset - HISTORY_PAGE_SIZE))}
          >
            上一页
          </button>
          <span>第 {Math.floor(offset / HISTORY_PAGE_SIZE) + 1} 页</span>
          <button
            className="text-button"
            disabled={!page?.has_more || page.next_offset !== offset + HISTORY_PAGE_SIZE}
            onClick={() => {
              if (page) setOffset(page.next_offset);
            }}
          >
            下一页
          </button>
        </div>
      </section>
      {run && (
        <>
          <div className="history-heading">
            <strong>{run.request?.topic ?? run.run_id}</strong>
            <button className="button" onClick={() => onOpen(run.run_id)}>
              打开持久 Run 轨迹
            </button>
          </div>
          <Reports
            key={run.run_id}
            reports={[
              ...run.role_reports.map((r, i) => ({
                id: `${r.role}-${i}`,
                label: `${r.role} · ${i + 1}`,
                content: r.content,
              })),
              ...(run.report ? [{ id: 'final', label: '最终报告', content: run.report }] : []),
            ]}
          />
        </>
      )}
    </>
  );
}
