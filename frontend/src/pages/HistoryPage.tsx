import { useQuery } from '@tanstack/react-query';
import { Search } from 'lucide-react';
import { useState } from 'react';
import { Link, useSearchParams } from 'react-router';
import { useApi } from '@/api/auth';
import type { HistoryStatus } from '@/api/client';
import {
  AccessRequired,
  ErrorNotice,
  Loading,
  PageTitle,
  Pagination,
  Status,
} from '@/components/shared';
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
import { usePages } from '@/features/usePages';
import { dateLabel, statusNames } from '@/lib/presentation';

const statuses: HistoryStatus[] = ['completed', 'failed', 'cancelled', 'interrupted'];

export default function HistoryPage() {
  const { api, authenticated } = useApi();
  const [params, setParams] = useSearchParams();
  const q = params.get('q') ?? '';
  const status = statuses.find((value) => value === params.get('status')) ?? '';
  const [draft, setDraft] = useState(q);
  const page = usePages();
  const history = useQuery({
    queryKey: ['history', page.offset, q, status],
    queryFn: ({ signal }) => api.history(page.offset, q, status, signal),
    enabled: authenticated,
  });
  const search = (text: string, state: string) => {
    page.reset();
    const next = new URLSearchParams();
    if (text.trim()) next.set('q', text.trim());
    if (state) next.set('status', state);
    setParams(next, { replace: true });
  };
  return (
    <div className="content-page">
      <PageTitle title="研究历史" />
      {!authenticated ? (
        <AccessRequired />
      ) : (
        <>
          <form
            className="filter-bar"
            onSubmit={(event) => {
              event.preventDefault();
              search(draft, status);
            }}
          >
            <div className="search-input">
              <Search size={17} />
              <Input
                type="search"
                aria-label="搜索研究主题"
                placeholder="搜索研究主题"
                maxLength={200}
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
              />
            </div>
            <select
              className="native-select"
              aria-label="按状态筛选"
              value={status}
              onChange={(event) => search(draft, event.target.value)}
            >
              <option value="">全部状态</option>
              {statuses.map((state) => (
                <option key={state} value={state}>
                  {statusNames[state]}
                </option>
              ))}
            </select>
            <Button type="submit" variant="outline">
              搜索
            </Button>
          </form>
          {history.isError ? (
            <ErrorNotice error={history.error} retry={() => void history.refetch()} />
          ) : !history.data ? (
            <Loading />
          ) : (
            <>
              {history.data.items.length ? (
                <Table className="data-table">
                  <TableHeader>
                    <TableRow>
                      <TableHead>研究主题</TableHead>
                      <TableHead>状态</TableHead>
                      <TableHead>企业知识库</TableHead>
                      <TableHead>研究时间</TableHead>
                      <TableHead>结束时间</TableHead>
                      <TableHead>
                        <span className="sr-only">执行详情</span>
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {history.data.items.map((item) => (
                      <TableRow key={item.run_id}>
                        <TableCell className="table-topic">
                          <Link to={'/history/' + item.run_id}>{item.topic || '查看研究'}</Link>
                        </TableCell>
                        <TableCell>
                          <Status status={item.status} />
                        </TableCell>
                        <TableCell>
                          {item.rag_enabled == null
                            ? '未知'
                            : item.rag_enabled
                              ? '已启用'
                              : '已关闭'}
                        </TableCell>
                        <TableCell>{dateLabel(item.started_at)}</TableCell>
                        <TableCell>{dateLabel(item.completed_at)}</TableCell>
                        <TableCell>
                          <Link to={'/runs/' + item.run_id + '/execution?from=history'}>
                            执行详情
                          </Link>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              ) : (
                <p className="empty-row">
                  {q || status ? '没有符合筛选条件的研究。' : '暂无完成历史交付的研究。'}
                </p>
              )}
              <Pagination
                label={page.label}
                previous={page.previous}
                next={history.data.has_more ? () => page.next(history.data.next_offset) : undefined}
              />
            </>
          )}
        </>
      )}
    </div>
  );
}
