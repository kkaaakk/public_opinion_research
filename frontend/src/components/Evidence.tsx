import { useEffect, useState, type FormEvent } from 'react';
import { ExternalLink, Network, RefreshCw, Search } from 'lucide-react';
import type { StudioApi } from '../api';
import type { EvidencePage } from '../protocol';
import { Empty, ErrorNote, JsonDetail } from './common';

export function Evidence({
  api,
  runId,
  replay,
}: {
  api: StudioApi;
  runId: string;
  replay: boolean;
}) {
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [kind, setKind] = useState('');
  const [after, setAfter] = useState('');
  const [page, setPage] = useState<EvidencePage | null>(null);
  const [selected, setSelected] = useState('');
  const [detail, setDetail] = useState<EvidencePage | null>(null);
  const [edgeAfter, setEdgeAfter] = useState('');
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState('');
  const [detailError, setDetailError] = useState('');
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError('');
    void api
      .evidence(runId, new URLSearchParams({ q: search, kind, after }), controller.signal)
      .then((p) => {
        if (!controller.signal.aborted)
          setPage((old) => (after && old ? { ...p, nodes: [...old.nodes, ...p.nodes] } : p));
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError((e as Error).message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [api, runId, search, kind, after, revision]);
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    setDetailError('');
    void api
      .evidence(
        runId,
        new URLSearchParams({ node_id: selected, edge_after: edgeAfter }),
        controller.signal,
      )
      .then((p) => {
        if (!controller.signal.aborted)
          setDetail((old) => (edgeAfter && old ? { ...p, edges: [...old.edges, ...p.edges] } : p));
      })
      .catch((e) => {
        if (!controller.signal.aborted) setDetailError((e as Error).message);
      });
    return () => controller.abort();
  }, [api, runId, selected, edgeAfter, revision]);
  function choose(id: string) {
    setSelected(id);
    setDetail(null);
    setEdgeAfter('');
  }
  function submit(e: FormEvent) {
    e.preventDefault();
    setAfter('');
    setSearch(query);
    setRevision((v) => v + 1);
  }
  const node = detail?.nodes[0];
  const sourceUrl = node?.properties.url;
  return (
    <section className="panel evidence-panel">
      <div className="panel-head">
        <strong>
          <Network size={16} />
          Research Graph · 证据溯源
        </strong>
        <button
          className="button small"
          disabled={loading}
          onClick={() => {
            setAfter('');
            setRevision((v) => v + 1);
          }}
        >
          <RefreshCw size={14} />
          刷新证据
        </button>
      </div>
      <p className="panel-note">
        {replay
          ? '当前持久证据视图：没有按事件游标保存的 Graph 快照。'
          : '来自当前 Run 的持久研究证据。'}{' '}
        与 RAG Knowledge Graph、DBOS Workflow Graph 分开管理。来源 URL 已移除查询参数与凭据。
      </p>
      <form className="evidence-search" onSubmit={submit}>
        <Search size={16} />
        <input
          aria-label="搜索证据"
          value={query}
          maxLength={500}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索来源、证据、发现或节点 ID"
        />
        <select
          aria-label="证据类型"
          value={kind}
          onChange={(e) => {
            setKind(e.target.value);
            setAfter('');
          }}
        >
          {[
            '',
            'Source',
            'Evidence',
            'Claim',
            'Finding',
            'SourceFinding',
            'Uncertainty',
            'Coverage',
            'Event',
            'Entity',
            'ResearchTask',
            'ResearchRun',
          ].map((v) => (
            <option key={v} value={v}>
              {v || '所有类型'}
            </option>
          ))}
        </select>
        <button className="button" disabled={loading}>
          查询
        </button>
      </form>
      <ErrorNote error={error} />
      {page?.available === false ? (
        <Empty>{page.reason}</Empty>
      ) : (
        <div className="evidence-grid">
          <div className="evidence-list">
            {page?.nodes.map((n) => (
              <button
                key={n.node_id}
                className={`evidence-item${selected === n.node_id ? ' chosen' : ''}`}
                onClick={() => choose(n.node_id)}
              >
                <span className="type-label">{n.node_type}</span>
                <strong>
                  {String(
                    n.properties.title ??
                      n.properties.statement ??
                      n.properties.summary ??
                      n.properties.name ??
                      n.node_id,
                  )}
                </strong>
                <small className="mono">{n.node_id}</small>
                <small>
                  {n.role ?? '角色未提供'} · Round {n.research_round ?? '未提供'}
                </small>
              </button>
            ))}
            {!page?.nodes.length && !loading && <Empty>没有符合条件的持久证据</Empty>}
            {page?.has_more && (
              <button
                className="button load-more"
                disabled={loading}
                onClick={() => setAfter(page.next_cursor ?? '')}
              >
                加载更多节点
              </button>
            )}
          </div>
          <div className="evidence-detail">
            <ErrorNote error={detailError} />
            {node ? (
              <>
                <div className="section-heading">
                  <h3>{node.node_type}</h3>
                  {typeof sourceUrl === 'string' && /^https?:\/\//.test(sourceUrl) && (
                    <a href={sourceUrl} target="_blank" rel="noreferrer" className="button small">
                      <ExternalLink size={13} />
                      来源
                    </a>
                  )}
                </div>
                <p className="mono muted">{node.node_id}</p>
                <JsonDetail value={node.properties} />
                <h4>引用与关系</h4>
                {detail.edges.map((edge) => (
                  <div className="relation" key={edge.edge_id}>
                    <button className="text-button mono" onClick={() => choose(edge.source_id)}>
                      {edge.source_id}
                    </button>
                    <span>{edge.relation_type}</span>
                    <button className="text-button mono" onClick={() => choose(edge.target_id)}>
                      {edge.target_id}
                    </button>
                  </div>
                ))}
                {!detail.edges.length && <p className="note">没有已记录的相邻关系</p>}
                {detail.edges_has_more && (
                  <button
                    className="button small"
                    onClick={() => setEdgeAfter(detail.next_edge_cursor ?? '')}
                  >
                    加载更多关系
                  </button>
                )}
              </>
            ) : (
              <Empty>选择证据节点查看来源、引用与关联发现</Empty>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
