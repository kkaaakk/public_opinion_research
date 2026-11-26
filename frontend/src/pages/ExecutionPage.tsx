import { useQuery } from '@tanstack/react-query';
import { RefreshCw } from 'lucide-react';
import { useState } from 'react';
import { useParams, useSearchParams } from 'react-router';
import { useApi } from '@/api/auth';
import { delivered, type Run, type Schema } from '@/api/client';
import {
  AccessRequired,
  ErrorNotice,
  Loading,
  PageTitle,
  Pagination,
  Status,
} from '@/components/shared';
import { Button } from '@/components/ui/button';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { usePages } from '@/features/usePages';
import { dateLabel, durationLabel, nativeStatuses, runStatus, stageName } from '@/lib/presentation';

function usageLabel(value?: number | null, incomplete = false) {
  if (value == null || (incomplete && value === 0)) return '未确认';
  return value.toLocaleString() + (incomplete ? '（部分）' : '');
}

function RunFacts({ run }: { run: Run }) {
  const usage = run.usage;
  const limits = run.budget_limits;
  const incomplete = usage.usage_complete === false;
  const counters = [
    ['模型调用', usage.model_calls, limits.max_model_calls, false],
    ['工具调用', usage.tool_calls, limits.max_tool_calls, false],
    ['搜索调用', usage.search_calls, limits.max_search_calls, false],
    ['输入 Token', usage.input_tokens, limits.max_input_tokens, incomplete],
    ['输出 Token', usage.output_tokens, limits.max_output_tokens, incomplete],
    ['总 Token', usage.total_tokens, null, incomplete],
  ] as const;
  return (
    <>
      <section className="plain-section">
        <div className="section-title">
          <h2>{run.request.topic}</h2>
          <Status status={run.status} label={runStatus(run)} />
        </div>
        <dl className="definition-grid">
          <div>
            <dt>Run ID</dt>
            <dd>
              <code>{run.run_id}</code>
            </dd>
          </div>
          <div>
            <dt>当前阶段</dt>
            <dd>{stageName(run.current_step || run.phase || '—')}</dd>
          </div>
          <div>
            <dt>DBOS 状态</dt>
            <dd>{nativeStatuses[run.native_status] ?? run.native_status}</dd>
          </div>
          <div>
            <dt>Workflow 版本</dt>
            <dd>{run.workflow_version}</dd>
          </div>
          <div>
            <dt>运行代次</dt>
            <dd>{run.generation}</dd>
          </div>
          <div>
            <dt>状态版本 / 事件游标</dt>
            <dd>
              {run.state_version} / {run.event_cursor}
            </dd>
          </div>
          <div>
            <dt>历史交付</dt>
            <dd>{delivered(run) ? '已完成' : run.events_paused ? '等待交互' : '待确认'}</dd>
          </div>
          <div>
            <dt>外部结果与清算</dt>
            <dd>
              {run.settlement === 'settled'
                ? '已结算'
                : run.settlement === 'pending'
                  ? '待结算'
                  : '存在未确认结果'}
            </dd>
          </div>
          <div>
            <dt>恢复运行</dt>
            <dd>
              {run.retry_requested
                ? runStatus(run)
                : run.retry_safe
                  ? '可以恢复'
                  : (run.retry_reason ?? '运行尚未结束')}
            </dd>
          </div>
          <div>
            <dt>结束时间</dt>
            <dd>{dateLabel(run.terminated_at)}</dd>
          </div>
        </dl>
        {Object.keys(run.error).length > 0 && (
          <details className="execution-extra" open>
            <summary>执行错误</summary>
            <dl className="definition-grid">
              {Object.entries(run.error).map(([name, value]) => (
                <div key={name}>
                  <dt>{name}</dt>
                  <dd>{typeof value === 'string' ? value : JSON.stringify(value)}</dd>
                </div>
              ))}
            </dl>
          </details>
        )}
      </section>
      <section className="plain-section">
        <div className="section-title">
          <h2>用量与预算</h2>
          {incomplete && <span className="muted">账本中存在未确认的 Token 用量</span>}
        </div>
        <dl className="definition-grid">
          {counters.map(([label, value, maximum, partial]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>
                {usageLabel(value, partial)}
                {limits.budget_enabled && maximum != null ? ' / ' + maximum.toLocaleString() : ''}
              </dd>
            </div>
          ))}
        </dl>
        {[...(usage.budget_warnings ?? []), ...(usage.degradation_reasons ?? [])].map(
          (item, index) => (
            <p className="muted" key={index}>
              {item}
            </p>
          ),
        )}
      </section>
    </>
  );
}

function CallUsage({ call }: { call: Schema['ExecutionCall'] }) {
  if (call.kind === 'tool')
    return call.search == null ? '未知' : call.search ? '检索工具' : '工具调用';
  if (!call.usage) return '账本未结算';
  const partial = call.usage.usage_complete === false;
  return (
    <span title="输入 / 输出 / 总 Token">
      {usageLabel(call.usage.input_tokens, partial)} /{' '}
      {usageLabel(call.usage.output_tokens, partial)} /{' '}
      {usageLabel(call.usage.total_tokens, partial)}
    </span>
  );
}

function Execution({ id, history }: { id: string; history: boolean }) {
  const { api } = useApi();
  const [workflow, setWorkflow] = useState(id);
  const workflowPages = usePages();
  const stepPages = usePages();
  const callPages = usePages();
  const run = useQuery({
    queryKey: ['run', id],
    queryFn: ({ signal }) => api.run(id, signal),
    refetchInterval: (query) => (query.state.data && !delivered(query.state.data) ? 8000 : false),
  });
  const execution = useQuery({
    queryKey: ['execution', id, workflow, workflowPages.offset, stepPages.offset, callPages.offset],
    queryFn: ({ signal }) =>
      api.execution(
        id,
        {
          workflow_id: workflow,
          offset: stepPages.offset,
          workflow_offset: workflowPages.offset,
          call_offset: callPages.offset,
          limit: 25,
        },
        signal,
      ),
    refetchInterval: run.data && !delivered(run.data) ? 8000 : false,
  });
  const data = execution.data;
  const calls = data?.calls ?? [];
  return (
    <div className="content-page execution-page">
      <PageTitle title="执行详情" back={(history ? '/history/' : '/runs/') + id}>
        <Button
          variant="outline"
          disabled={run.isFetching || execution.isFetching}
          onClick={() => {
            void run.refetch();
            void execution.refetch();
          }}
        >
          <RefreshCw data-icon="inline-start" />
          刷新
        </Button>
      </PageTitle>
      {run.isError ? (
        <ErrorNotice error={run.error} />
      ) : run.data ? (
        <RunFacts run={run.data} />
      ) : (
        <Loading />
      )}
      {execution.isError ? (
        <ErrorNotice error={execution.error} />
      ) : !data ? (
        <Loading />
      ) : (
        <>
          <section className="plain-section">
            <h2>Workflow</h2>
            <Table className="data-table compact">
              <TableHeader>
                <TableRow>
                  <TableHead>名称 / ID</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>队列</TableHead>
                  <TableHead>启动时间</TableHead>
                  <TableHead>结束时间</TableHead>
                  <TableHead>
                    <span className="sr-only">查看 Step</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.workflows.map((item) => (
                  <TableRow key={item.workflow_id}>
                    <TableCell>
                      <strong>{item.name}</strong>
                      <small className="cell-detail">{item.workflow_id}</small>
                    </TableCell>
                    <TableCell>{nativeStatuses[item.status] ?? item.status}</TableCell>
                    <TableCell>{item.queue_name ?? '—'}</TableCell>
                    <TableCell>{dateLabel(item.dequeued_at ?? item.created_at)}</TableCell>
                    <TableCell>{dateLabel(item.completed_at)}</TableCell>
                    <TableCell>
                      <Button
                        variant={workflow === item.workflow_id ? 'secondary' : 'outline'}
                        size="sm"
                        onClick={() => {
                          setWorkflow(item.workflow_id);
                          stepPages.reset();
                        }}
                      >
                        {workflow === item.workflow_id ? '当前 Step 列表' : '查看 Step'}
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <Pagination
              label={workflowPages.label}
              previous={workflowPages.previous}
              next={
                data.workflows_has_more
                  ? () => workflowPages.next(data.next_workflow_offset)
                  : undefined
              }
            />
          </section>
          <section className="plain-section">
            <div className="section-title">
              <h2>Step</h2>
              <code className="wrap-anywhere">{data.workflow_id}</code>
            </div>
            {data.steps.length ? (
              <Table className="data-table compact">
                <TableHeader>
                  <TableRow>
                    <TableHead>序号</TableHead>
                    <TableHead>名称</TableHead>
                    <TableHead>开始时间</TableHead>
                    <TableHead>完成时间</TableHead>
                    <TableHead>耗时</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.steps.map((step) => (
                    <TableRow key={step.function_id}>
                      <TableCell>{step.function_id}</TableCell>
                      <TableCell>
                        <code>{step.function_name}</code>
                        {step.child_workflow_id && (
                          <small className="cell-detail">{step.child_workflow_id}</small>
                        )}
                      </TableCell>
                      <TableCell>{dateLabel(step.started_at_epoch_ms)}</TableCell>
                      <TableCell>{dateLabel(step.completed_at_epoch_ms)}</TableCell>
                      <TableCell>
                        {step.started_at_epoch_ms == null || step.completed_at_epoch_ms == null
                          ? '未确认'
                          : durationLabel(step.completed_at_epoch_ms - step.started_at_epoch_ms)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <p className="empty-row">此 Workflow 暂无可读取的 Step。</p>
            )}
            <Pagination
              label={stepPages.label}
              previous={stepPages.previous}
              next={data.steps_has_more ? () => stepPages.next(data.next_offset) : undefined}
            />
          </section>
          <section className="plain-section">
            <h2>调用账本</h2>
            {!data.calls_available ? (
              <p className="empty-row">该版本的逐次调用账本不可用。</p>
            ) : !calls.length ? (
              <p className="empty-row">此运行暂无已记录调用。</p>
            ) : (
              <Table className="data-table compact">
                <TableHeader>
                  <TableRow>
                    <TableHead>类型</TableHead>
                    <TableHead>调用 / ID</TableHead>
                    <TableHead>状态</TableHead>
                    <TableHead>用量（输入 / 输出 / 总 Token）</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {calls.map((call) => (
                    <TableRow key={call.kind + ':' + call.id}>
                      <TableCell>
                        {call.kind === 'model' ? '模型' : call.search ? '搜索' : '工具'}
                      </TableCell>
                      <TableCell>
                        <strong>{call.name || call.stage_id || '—'}</strong>
                        <small className="cell-detail">{call.id}</small>
                      </TableCell>
                      <TableCell>{call.status ?? '未知'}</TableCell>
                      <TableCell>
                        <CallUsage call={call} />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
            <Pagination
              label={callPages.label}
              previous={callPages.previous}
              next={data.calls_has_more ? () => callPages.next(data.next_call_offset) : undefined}
            />
          </section>
        </>
      )}
    </div>
  );
}

export default function ExecutionPage() {
  const { runId = '' } = useParams();
  const [params] = useSearchParams();
  return useApi().authenticated ? (
    <Execution key={runId} id={runId} history={params.get('from') === 'history'} />
  ) : (
    <AccessRequired />
  );
}
