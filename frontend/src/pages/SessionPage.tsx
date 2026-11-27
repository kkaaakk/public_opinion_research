import { useQuery } from '@tanstack/react-query';
import { ArrowUpRight, Plus } from 'lucide-react';
import { Link, useParams } from 'react-router';
import { useApi } from '@/api/auth';
import {
  AccessRequired,
  ErrorNotice,
  LinkButton,
  Loading,
  PageTitle,
  Pagination,
} from '@/components/shared';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { usePages } from '@/features/usePages';
import { dateLabel, nativeStatuses } from '@/lib/presentation';

function Sessions() {
  const { api } = useApi();
  const page = usePages();
  const query = useQuery({
    queryKey: ['sessions', page.offset, 24],
    queryFn: ({ signal }) => api.sessions(page.offset, 24, signal),
  });
  return (
    <div className="content-page">
      <PageTitle title="研究会话">
        <LinkButton variant="default" to="/">
          <Plus data-icon="inline-start" />
          新建研究
        </LinkButton>
      </PageTitle>
      {query.isError ? (
        <ErrorNotice error={query.error} retry={() => void query.refetch()} />
      ) : !query.data ? (
        <Loading />
      ) : (
        <>
          {query.data.items.length ? (
            <Table className="data-table">
              <TableHeader>
                <TableRow>
                  <TableHead>最近研究主题</TableHead>
                  <TableHead>研究次数</TableHead>
                  <TableHead>最近创建时间</TableHead>
                  <TableHead>
                    <span className="sr-only">打开</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {query.data.items.map((session) => (
                  <TableRow key={session.research_session_id}>
                    <TableCell className="table-topic">
                      <Link to={'/sessions/' + session.research_session_id}>
                        {session.topic || '研究会话'}
                      </Link>
                    </TableCell>
                    <TableCell>{session.run_count}</TableCell>
                    <TableCell>{dateLabel(session.updated_at)}</TableCell>
                    <TableCell>
                      <Link to={'/runs/' + session.latest_run_id} className="inline-link">
                        最近一次研究 <ArrowUpRight size={14} />
                      </Link>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <p className="empty-row">暂无研究会话。</p>
          )}
          <Pagination
            label={page.label}
            previous={page.previous}
            next={query.data.has_more ? () => page.next(query.data.next_offset) : undefined}
          />
        </>
      )}
    </div>
  );
}

function Session({ id }: { id: string }) {
  const { api } = useApi();
  const page = usePages();
  const query = useQuery({
    queryKey: ['session', id, page.offset],
    queryFn: ({ signal }) => api.session(id, page.offset, signal),
    refetchInterval: 10_000,
  });
  const canContinue = query.data?.can_continue;
  return (
    <div className="content-page">
      <PageTitle title="研究会话" back="/sessions">
        {canContinue && (
          <LinkButton variant="default" to={'/?session=' + id}>
            <Plus data-icon="inline-start" />
            延续研究
          </LinkButton>
        )}
      </PageTitle>
      {query.isError ? (
        <ErrorNotice error={query.error} retry={() => void query.refetch()} />
      ) : !query.data ? (
        <Loading />
      ) : (
        <>
          <p className="page-description">
            {query.data.run_count} 次研究{!canContinue && ' · 会话中仍有未结束或待处理的研究'}
          </p>
          <Table className="data-table">
            <TableHeader>
              <TableRow>
                <TableHead>研究主题</TableHead>
                <TableHead>DBOS 执行状态</TableHead>
                <TableHead>企业知识库</TableHead>
                <TableHead>开始时间</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {query.data.items.map((run) => (
                <TableRow key={run.run_id}>
                  <TableCell className="table-topic">
                    <Link to={'/runs/' + run.run_id}>{run.topic || '查看研究'}</Link>
                  </TableCell>
                  <TableCell>
                    {run.native_status
                      ? (nativeStatuses[run.native_status] ?? run.native_status)
                      : '未知'}
                  </TableCell>
                  <TableCell>
                    {run.rag_enabled == null ? '未知' : run.rag_enabled ? '已启用' : '已关闭'}
                  </TableCell>
                  <TableCell>{dateLabel(run.created_at)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <Pagination
            label={page.label}
            previous={page.previous}
            next={query.data.has_more ? () => page.next(query.data.next_offset) : undefined}
          />
        </>
      )}
    </div>
  );
}

export default function SessionPage() {
  const { authenticated } = useApi();
  const { sessionId } = useParams();
  if (!authenticated) return <AccessRequired />;
  return sessionId ? <Session key={sessionId} id={sessionId} /> : <Sessions />;
}
