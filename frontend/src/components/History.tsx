import { useEffect, useState, type FormEvent } from 'react';
import { History as HistoryIcon, Search } from 'lucide-react';
import type { StudioApi } from '../api';
import type { HistoryPage, HistoryRecord } from '../protocol';
import { Badge, Empty, ErrorNote, time } from './common';
import { Reports } from './Reports';
export function History({ api, onOpen }: { api: StudioApi; onOpen: (id: string) => void }) {
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [page, setPage] = useState<HistoryPage | null>(null);
  const [selected, setSelected] = useState('');
  const [archive, setArchive] = useState<HistoryRecord | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    setError('');
    void api
      .history(search, offset, controller.signal)
      .then((p) => {
        if (!controller.signal.aborted) setPage(p);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError((e as Error).message);
      });
    return () => controller.abort();
  }, [api, search, offset, revision]);
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    setArchive(null);
    void api
      .archive(selected, controller.signal)
      .then((p) => {
        if (!controller.signal.aborted) setArchive(p);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError((e as Error).message);
      });
    return () => controller.abort();
  }, [api, selected]);
  function submit(e: FormEvent) {
    e.preventDefault();
    setSearch(query);
    setOffset(0);
    setRevision((v) => v + 1);
  }
  return (
    <>
      <section className="panel history-panel">
        <div className="panel-head">
          <strong>
            <HistoryIcon size={16} />
            历史报告归档
          </strong>
          <span className="muted">Neo4j History · {page?.total ?? '—'} 条</span>
        </div>
        <form className="evidence-search" onSubmit={submit}>
          <Search size={16} />
          <input
            aria-label="搜索归档"
            value={query}
            maxLength={300}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索研究主题"
          />
          <button className="button">查询归档</button>
        </form>
        <ErrorNote error={error} />
        <div className="archive-list">
          {page?.items.map((item) => (
            <button
              className={`archive-item${selected === item.id ? ' chosen' : ''}`}
              key={item.id}
              onClick={() => setSelected(item.id)}
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
        {!page?.items.length && <Empty>暂无归档记录</Empty>}
        <div className="pagination">
          <button
            className="text-button"
            disabled={!offset}
            onClick={() => setOffset(Math.max(0, offset - 10))}
          >
            上一页
          </button>
          <span>
            {Math.floor(offset / 10) + 1} / {Math.max(1, Math.ceil((page?.total ?? 0) / 10))}
          </span>
          <button
            className="text-button"
            disabled={!page || offset + 10 >= page.total}
            onClick={() => setOffset(offset + 10)}
          >
            下一页
          </button>
        </div>
      </section>
      {archive && (
        <>
          <div className="archive-heading">
            <strong>{archive.topic}</strong>
            <button
              className="button"
              disabled={!/^[a-f0-9]{32}$/.test(archive.run_id)}
              onClick={() => onOpen(archive.run_id)}
            >
              打开持久 Run 轨迹
            </button>
          </div>
          <Reports
            key={archive.id}
            archive
            reports={[
              ...archive.role_reports.map((r, i) => ({
                id: `${r.role}-${i}`,
                label: `${r.role} · ${i + 1}`,
                content: r.content,
              })),
              ...(archive.report
                ? [{ id: 'final', label: '归档最终报告', content: archive.report }]
                : []),
            ]}
          />
        </>
      )}
    </>
  );
}
