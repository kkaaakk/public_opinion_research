import { useEffect, useState } from 'react';
import { Database, RefreshCw } from 'lucide-react';
import type { StudioApi } from '../api';
import type { ExecutionPage } from '../protocol';
import { Badge, duration, Empty, ErrorNote, time } from './common';

export function NativeTrace({ api, runId }: { api: StudioApi; runId: string }) {
  const [workflow, setWorkflow] = useState(runId);
  const [offset, setOffset] = useState(0);
  const [workflowOffset, setWorkflowOffset] = useState(0);
  const [data, setData] = useState<ExecutionPage | null>(null);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError('');
    const params = new URLSearchParams({
      workflow_id: workflow,
      offset: String(offset),
      workflow_offset: String(workflowOffset),
    });
    void api
      .execution(runId, params, controller.signal)
      .then((page) => {
        if (!controller.signal.aborted) setData(page);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError((e as Error).message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [api, runId, workflow, offset, workflowOffset, revision]);
  function select(id: string) {
    setWorkflow(id);
    setOffset(0);
  }
  return (
    <section className="panel native-panel">
      <div className="panel-head">
        <strong>
          <Database size={16} />
          DBOS 原生工作流 / Journal
        </strong>
        <button
          className="button small"
          disabled={loading}
          onClick={() => setRevision((v) => v + 1)}
        >
          <RefreshCw size={14} />
          刷新原生日志
        </button>
      </div>
      <p className="panel-note">
        原生状态是执行权威。Journal 中的命令包含审批等待、子 Workflow 和业务
        Step；未写入的调用可能仍在执行。DBOS 终态与 SDK 清算、归档分别显示。
      </p>
      <ErrorNote error={error} />
      <div className="native-grid">
        <div className="native-workflows">
          <h4>Native Workflows {loading && '· 读取中'}</h4>
          {data?.workflows.map((w) => (
            <button
              key={w.workflow_id}
              className={`native-workflow${workflow === w.workflow_id ? ' chosen' : ''}`}
              onClick={() => select(w.workflow_id)}
            >
              <div>
                <strong>{w.name}</strong>
                <Badge status={w.status} />
              </div>
              <small className="mono">{w.workflow_id}</small>
              <small>Queue: {w.queue_name ?? '未提供'}</small>
              <small>Parent: {w.parent_workflow_id?.replace(runId, 'run') ?? '未提供'}</small>
              <small>
                {time(w.created_at)} → {time(w.completed_at)}
              </small>
            </button>
          ))}
          <div className="pagination">
            <button
              className="text-button"
              disabled={!workflowOffset || loading}
              onClick={() => setWorkflowOffset(Math.max(0, workflowOffset - 100))}
            >
              上一页
            </button>
            <button
              className="text-button"
              disabled={!data?.workflows_has_more || loading}
              onClick={() => setWorkflowOffset(data?.next_workflow_offset ?? 0)}
            >
              下一页
            </button>
          </div>
        </div>
        <div className="native-steps">
          <h4 className="mono">{data?.workflow_id ?? workflow}</h4>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>命令</th>
                  <th>函数 / 子工作流</th>
                  <th>实际时间</th>
                </tr>
              </thead>
              <tbody>
                {data?.steps.map((s) => (
                  <tr key={s.function_id}>
                    <td className="mono">#{s.function_id}</td>
                    <td>
                      <strong>{s.function_name}</strong>
                      {s.child_workflow_id && (
                        <button
                          className="event-link mono"
                          onClick={() => select(s.child_workflow_id!)}
                        >
                          {s.child_workflow_id.replace(runId, 'run')}
                        </button>
                      )}
                    </td>
                    <td>
                      <small>{time(s.started_at_epoch_ms)}</small>
                      <small>{time(s.completed_at_epoch_ms)}</small>
                      <small>
                        {s.started_at_epoch_ms != null && s.completed_at_epoch_ms != null
                          ? duration(s.completed_at_epoch_ms - s.started_at_epoch_ms)
                          : '耗时未提供'}
                      </small>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!data?.steps.length && <Empty>此工作流尚无已写入的 Journal 命令</Empty>}
          <div className="pagination">
            <button
              className="text-button"
              disabled={!offset || loading}
              onClick={() => setOffset(Math.max(0, offset - 100))}
            >
              上一页命令
            </button>
            <span>
              {offset + 1}–{offset + (data?.steps.length ?? 0)}
            </span>
            <button
              className="text-button"
              disabled={!data?.steps_has_more || loading}
              onClick={() => setOffset(data?.next_offset ?? 0)}
            >
              下一页命令
            </button>
          </div>
        </div>
      </div>
      <p className="panel-note">
        输入、输出、原始异常和凭据均不开放。控制台只读本页 Journal，不提供强制恢复命令。
      </p>
    </section>
  );
}
