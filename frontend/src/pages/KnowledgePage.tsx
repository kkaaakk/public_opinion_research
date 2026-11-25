import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Database, RefreshCw, Search } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useApi } from '@/api/auth';
import type { Schema } from '@/api/client';
import { AccessRequired, ErrorNotice, Loading, PageTitle, Pagination } from '@/components/shared';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Textarea } from '@/components/ui/textarea';
import { usePages } from '@/features/usePages';
import { dateLabel, newKey, sizeLabel } from '@/lib/presentation';

const indexStates = {
  ready: '已就绪',
  empty: '已加载，索引为空',
  unknown: '当前进程未确认',
  building: '正在构建',
};
const jobStates = { running: '运行中', completed: '已完成', failed: '失败' };

function Overview({
  status,
  sourceCount,
}: {
  status: Schema['KnowledgeStatus'];
  sourceCount?: number;
}) {
  const info = [
    ['索引状态', indexStates[status.index_state]],
    ['配置的知识源', sourceCount === undefined ? '—' : String(sourceCount)],
    [
      '已加载文档 / 分块',
      status.document_count == null || status.chunk_count == null
        ? '未知'
        : status.document_count + ' / ' + status.chunk_count,
    ],
    ['向量库 / 关键词索引', status.vector_store + ' / ' + status.keyword_backend],
    ['检索模式', status.retrieval_mode],
    ['Embedding', status.embedding_provider + ' / ' + status.embedding_model],
    ['Reranker', status.reranker_provider + ' / ' + status.reranker_model],
    ['查询改写', status.query_rewrite_enabled ? '已启用' : '已关闭'],
    ['多模态', status.multimodal_enabled ? status.multimodal_provider : '已关闭'],
    ['Vision', status.vision_enabled ? status.vision_model : '已关闭'],
  ];
  return (
    <section className="plain-section">
      <div className="section-title">
        <h2>知识库概览</h2>
        <Badge variant="secondary">
          {!status.enabled ? 'RAG 已关闭' : status.configured ? '已配置' : '未配置知识源'}
        </Badge>
      </div>
      <dl className="definition-grid">
        {info.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function Retrieve({ disabled }: { disabled: boolean }) {
  const { api } = useApi();
  const client = useQueryClient();
  const [query, setQuery] = useState('');
  const search = useMutation({
    mutationFn: (text: string) =>
      api.post<Schema['SearchResult']>(
        '/api/knowledge/search',
        { query: text },
        undefined,
        120_000,
      ),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: ['knowledge'] });
      void client.invalidateQueries({ queryKey: ['knowledge-sources'] });
    },
  });
  const result = search.data;
  const matches = result?.matches ?? [];
  return (
    <section className="plain-section">
      <h2>检索测试</h2>
      <form
        className="retrieval-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (!disabled && query.trim() && !search.isPending) search.mutate(query.trim());
        }}
      >
        <Textarea
          aria-label="检索问题"
          rows={2}
          maxLength={2000}
          placeholder="输入要检索的企业知识问题"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <Button type="submit" disabled={disabled || search.isPending || !query.trim()}>
          <Search data-icon="inline-start" />
          {search.isPending ? '检索中…' : '执行检索'}
        </Button>
      </form>
      {search.isError && <ErrorNotice error={search.error} />}
      {result && (
        <>
          <p className="muted">
            「{result.query}」 · {matches.length} 个分块 · {result.elapsed_ms.toLocaleString()} ms
          </p>
          {matches.length === 0 && <p className="empty-row">没有检索到对应分块。</p>}
          <ol className="matches">
            {matches.map((match) => (
              <li key={match.chunk_id}>
                <div className="match-header">
                  <h3>
                    #{match.rank} {match.title || match.source}
                  </h3>
                  <Badge variant="secondary">相关性 {match.score.toFixed(4)}</Badge>
                </div>
                <p className="muted">来源 · {match.source}</p>
                <p className="chunk">{match.content}</p>
                <details>
                  <summary>检索元信息</summary>
                  <dl className="definition-grid">
                    {(
                      [
                        ['向量', match.vector_score],
                        ['关键词', match.keyword_score],
                        ['结构化', match.structured_score],
                        ['重排', match.rerank_score],
                      ] as const
                    )
                      .filter(([, value]) => value != null)
                      .map(([label, value]) => (
                        <div key={label}>
                          <dt>{label}分数</dt>
                          <dd>{value?.toFixed(4)}</dd>
                        </div>
                      ))}
                    {Object.entries(match.metadata).map(([label, value]) => (
                      <div key={label}>
                        <dt>{label}</dt>
                        <dd>{typeof value === 'string' ? value : JSON.stringify(value)}</dd>
                      </div>
                    ))}
                  </dl>
                </details>
              </li>
            ))}
          </ol>
        </>
      )}
    </section>
  );
}

function Knowledge() {
  const { api } = useApi();
  const client = useQueryClient();
  const page = usePages();
  const [q, setQ] = useState('');
  const [draft, setDraft] = useState('');
  const status = useQuery({
    queryKey: ['knowledge'],
    queryFn: ({ signal }) => api.knowledge(signal),
    refetchInterval: (query) => (query.state.data?.job?.status === 'running' ? 2200 : false),
  });
  const allSources = useQuery({
    queryKey: ['knowledge-sources', page.offset, q],
    queryFn: ({ signal }) => api.sources(page.offset, q, signal),
  });
  const sourceSummary = useQuery({
    queryKey: ['knowledge-sources', 0, ''],
    queryFn: ({ signal }) => api.sources(0, '', signal),
    enabled: page.offset !== 0 || Boolean(q),
  });
  const lastBuild = status.data?.job?.finished_at;
  useEffect(() => {
    if (lastBuild) void client.invalidateQueries({ queryKey: ['knowledge-sources'] });
  }, [client, lastBuild]);
  const build = useMutation({
    mutationFn: (force: boolean) =>
      api.post<Schema['IndexJob']>('/api/knowledge/index', { force }, newKey()),
    onSuccess: (job) => {
      client.setQueryData<Schema['KnowledgeStatus']>(['knowledge'], (old) =>
        old
          ? { ...old, job, index_state: job.status === 'running' ? 'building' : old.index_state }
          : old,
      );
    },
    onSettled: () => void status.refetch(),
  });
  const building = build.isPending || status.data?.job?.status === 'running';
  const canMaintain = Boolean(status.data?.enabled && status.data.configured && !building);
  const sources = allSources.data;
  const job = status.data?.job;
  return (
    <div className="content-page">
      <PageTitle title="企业知识库">
        <Button
          variant="outline"
          onClick={() => {
            void status.refetch();
            void client.invalidateQueries({ queryKey: ['knowledge-sources'] });
          }}
        >
          <RefreshCw data-icon="inline-start" />
          刷新
        </Button>
      </PageTitle>
      {status.isError ? (
        <ErrorNotice error={status.error} retry={() => void status.refetch()} />
      ) : status.data ? (
        <Overview
          status={status.data}
          sourceCount={(!q && page.offset === 0 ? sources : sourceSummary.data)?.total}
        />
      ) : (
        <Loading />
      )}

      <section className="plain-section">
        <h2>知识源</h2>
        <form
          className="filter-bar"
          onSubmit={(event) => {
            event.preventDefault();
            page.reset();
            setQ(draft.trim());
          }}
        >
          <div className="search-input">
            <Search size={17} />
            <Input
              type="search"
              aria-label="搜索知识源"
              placeholder="搜索文件名称或来源分类"
              maxLength={200}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
            />
          </div>
          <Button variant="outline" type="submit">
            搜索
          </Button>
        </form>
        {allSources.isError ? (
          <ErrorNotice error={allSources.error} retry={() => void allSources.refetch()} />
        ) : !sources ? (
          <Loading />
        ) : (
          <>
            {sources.items.length > 0 ? (
              <Table className="data-table">
                <TableHeader>
                  <TableRow>
                    <TableHead>文件名称</TableHead>
                    <TableHead>文档类型</TableHead>
                    <TableHead>来源分类</TableHead>
                    <TableHead>大小</TableHead>
                    <TableHead>修改时间</TableHead>
                    <TableHead>索引状态</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {sources.items.map((source) => (
                    <TableRow key={source.id}>
                      <TableCell className="table-topic">{source.name}</TableCell>
                      <TableCell>{source.document_type}</TableCell>
                      <TableCell>{source.category}</TableCell>
                      <TableCell>{sizeLabel(source.size_bytes)}</TableCell>
                      <TableCell>{dateLabel(source.modified_at)}</TableCell>
                      <TableCell>
                        {source.indexed == null ? '未确认' : source.indexed ? '已加载' : '未加载'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <p className="empty-row">
                {q ? '未找到匹配的知识源。' : '配置的目录中没有可加载的文件。'}
              </p>
            )}
            <Pagination
              label={page.label + ' · ' + sources.total + ' 个文件'}
              previous={page.previous}
              next={sources.has_more ? () => page.next(sources.next_offset) : undefined}
            />
          </>
        )}
      </section>

      <section className="plain-section">
        <h2>索引管理</h2>
        <p className="muted">
          索引维护和检索测试在研究空闲时运行，期间新建或重试研究需要等操作结束。
        </p>
        <div className="flex flex-wrap gap-2">
          <Button disabled={!canMaintain} onClick={() => build.mutate(false)}>
            <Database data-icon="inline-start" />
            构建或刷新索引
          </Button>
          <Button
            variant="outline"
            disabled={!canMaintain}
            onClick={() => {
              if (window.confirm('使用当前知识源重建索引？')) build.mutate(true);
            }}
          >
            重建索引
          </Button>
        </div>
        {build.isError && <ErrorNotice error={build.error} />}
        {job && (
          <div className="job-status" role="status">
            <Badge variant="secondary">{jobStates[job.status]}</Badge>
            <span>
              {job.force ? '重建' : '构建或刷新'} · {dateLabel(job.started_at)}
              {job.finished_at && ' → ' + dateLabel(job.finished_at)}
            </span>
            {job.error && <span className="job-error">{job.error}</span>}
          </div>
        )}
      </section>
      <Retrieve disabled={!canMaintain} />
    </div>
  );
}

export default function KnowledgePage() {
  return useApi().authenticated ? <Knowledge /> : <AccessRequired />;
}
