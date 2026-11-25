import {
  Activity,
  ChevronRight,
  FileText,
  PanelRightClose,
  PanelRightOpen,
  RotateCcw,
  Square,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useParams } from 'react-router';
import { useApi } from '@/api/auth';
import { delivered, runPath, type Run, type Schema } from '@/api/client';
import { observeRun, type Connection, type RunEvent } from '@/api/events';
import { Markdown, Report } from '@/components/Markdown';
import {
  AccessRequired,
  ErrorNotice,
  LinkButton,
  Loading,
  PageTitle,
  Status,
} from '@/components/shared';
import { Button } from '@/components/ui/button';
import { BriefApproval, Clarification } from '@/features/Interactions';
import { Followup, StageItem, TimelineItem } from '@/features/Timeline';
import { stageTimeline, type Stage } from '@/features/stageTimeline';
import { useRunCommands } from '@/features/useRunCommands';
import { dateLabel, durationLabel, runStatus, stageName } from '@/lib/presentation';

function RetainedImage({ id, image }: { id: string; image: Schema['ImageInfo'] }) {
  const { api } = useApi();
  const [url, setUrl] = useState('');
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const abort = new AbortController();
    let objectUrl = '';
    void api
      .request(runPath(id) + '/images/' + image.position, { signal: abort.signal })
      .then((response) => response.blob())
      .then((blob) => {
        if (!abort.signal.aborted) {
          objectUrl = URL.createObjectURL(blob);
          setUrl(objectUrl);
        }
      })
      .catch(() => {
        if (!abort.signal.aborted) setFailed(true);
      });
    return () => {
      abort.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [api, id, image.position]);
  return (
    <figure className="retained-image">
      {url && (
        <a href={url} target="_blank" rel="noreferrer">
          <img alt={image.name} src={url} />
        </a>
      )}
      <figcaption title={image.name}>
        {image.name}
        {failed && ' · 无法读取'}
      </figcaption>
    </figure>
  );
}

function Elapsed({ run }: { run: Run }) {
  const [now, setNow] = useState(Date.now);
  const end = run.retry_requested ? null : run.terminated_at;
  useEffect(() => {
    if (end) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [end]);
  const start = Date.parse(run.created_at);
  const elapsed = (end ? Date.parse(end) : now) - start;
  return Number.isFinite(elapsed) ? durationLabel(elapsed) : '—';
}

function Inspector({ run, stages, dismiss }: { run: Run; stages: Stage[]; dismiss: () => void }) {
  const items = Array.from(new Map(stages.map((stage) => [stage.step, stage])).values());
  return (
    <aside className="run-inspector" aria-label="运行摘要">
      <div className="inspector-title">
        <h2>本次运行</h2>
        <Button variant="ghost" size="icon-sm" aria-label="收起运行摘要" onClick={dismiss}>
          <PanelRightClose />
        </Button>
      </div>
      <dl className="inspector-data">
        <div>
          <dt>当前状态</dt>
          <dd>
            <Status status={run.status} label={runStatus(run)} />
          </dd>
        </div>
        <div>
          <dt>已运行时间</dt>
          <dd>
            <Elapsed run={run} />
          </dd>
        </div>
        {run.current_step && (
          <div>
            <dt>当前阶段</dt>
            <dd>{stageName(run.current_step)}</dd>
          </div>
        )}
        <div>
          <dt>企业知识库</dt>
          <dd>
            {run.request.rag_enabled == null
              ? '未知'
              : run.request.rag_enabled
                ? '已启用'
                : '已关闭'}
          </dd>
        </div>
      </dl>
      {items.length > 0 && (
        <section>
          <h3>阶段进度</h3>
          <ul className="inspector-steps">
            {items.map((stage) => (
              <li key={stage.step} data-state={stage.state}>
                <span>{stageName(stage.step)}</span>
                <small>
                  {stage.state === 'completed'
                    ? '已完成'
                    : stage.state === 'failed'
                      ? '失败'
                      : stage.state === 'running' &&
                          stage.generation === run.generation &&
                          run.status === 'running'
                        ? '进行中'
                        : '已记录'}
                </small>
              </li>
            ))}
          </ul>
        </section>
      )}
      {(run.plan || run.report || run.role_reports.length > 0) && (
        <section>
          <h3>阶段产物</h3>
          <ul className="inspector-links">
            {run.plan && (
              <li>
                <a href="#research-brief">
                  <FileText size={15} />
                  Research Brief
                </a>
              </li>
            )}
            {run.role_reports.length > 0 && (
              <li>
                <a href="#research-stages">
                  <Activity size={15} />
                  阶段报告
                </a>
              </li>
            )}
            {run.report && (
              <li>
                <a href="#final-report">
                  <FileText size={15} />
                  最终报告
                </a>
              </li>
            )}
          </ul>
        </section>
      )}
      <LinkButton to={'/runs/' + run.run_id + '/execution'}>
        执行详情
        <ChevronRight data-icon="inline-end" />
      </LinkButton>
    </aside>
  );
}

function useRunObservation(id: string, history: boolean, enabled: boolean) {
  const { api } = useApi();
  const [run, setRun] = useState<Run>();
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [connection, setConnection] = useState<Connection>('loading');
  const [error, setError] = useState<unknown>();
  const [revision, setRevision] = useState(0);
  const eventsRef = useRef(events);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    void observeRun(
      api,
      id,
      history,
      eventsRef.current,
      {
        snapshot: setRun,
        events: (entries) => {
          eventsRef.current = entries;
          setEvents(entries);
        },
        connection: (state, failure) => {
          setConnection(state);
          setError(failure);
        },
      },
      controller.signal,
    );
    return () => controller.abort();
  }, [id, api, revision, history, enabled]);
  return { run, events, connection, error, refresh };
}

function RunView({ id, history }: { id: string; history: boolean }) {
  const { authenticated } = useApi();
  const { run, events, connection, error, refresh } = useRunObservation(id, history, authenticated);
  const commands = useRunCommands(id, run, events, refresh);
  const [inspector, setInspector] = useState(
    () => window.matchMedia('(min-width: 1120px)').matches,
  );
  const stages = useMemo(() => (run ? stageTimeline(run, events) : []), [run, events]);
  const back = history
    ? '/history'
    : run?.research_session_id
      ? '/sessions/' + run.research_session_id
      : '/sessions';
  if (!authenticated) return <AccessRequired />;
  if (!run)
    return (
      <div className="content-page">
        <PageTitle title="研究详情" back={back} />
        {error ? <ErrorNotice error={error} retry={refresh} /> : <Loading text="正在读取研究…" />}
      </div>
    );

  const topic = run.request.topic || '研究详情';
  const finished = delivered(run);
  const currentClarification =
    run.status === 'awaiting_clarification' && run.clarification?.questions.length
      ? run.clarification
      : null;
  const plan = run.plan;
  const imageStage = stages.find((stage) => stage.step === 'enrich_query_images');
  const planning = stages.filter((stage) =>
    ['enrich_query_images', 'plan_research'].includes(stage.step),
  );
  const activeEvents = events.filter((event) => (event.generation ?? 0) <= run.generation);
  const clarificationEvents = Array.from(
    new Map(
      activeEvents
        .filter(
          (event) =>
            event.type === 'run_status' &&
            event.clarification_version &&
            event.clarification_version !== currentClarification?.version,
        )
        .map((event) => [event.clarification_version, event]),
    ).values(),
  );
  const research = stages.filter(
    (stage) =>
      !['enrich_query_images', 'plan_research', 'revise_brief'].includes(stage.step) &&
      !(stage.step === 'generate_final_report' && run.report),
  );
  const researchItems = [
    ...research.map((stage) => ({
      order: stage.order,
      key: stage.id,
      item: <StageItem stage={stage} run={run} />,
    })),
    ...activeEvents
      .filter((event) => event.type === 'research_followup')
      .map((event) => {
        const review = research.findLast(
          (stage) =>
            stage.review &&
            stage.order < event.seq &&
            stage.generation === (event.generation ?? 0) &&
            (stage.round === undefined || event.round === stage.round + 1),
        );
        const tasks =
          review?.review?.next_tasks.filter((task) => event.roles?.includes(task.target_role)) ??
          [];
        return {
          order: event.seq,
          key: 'followup:' + event.seq,
          item: <Followup event={event} tasks={tasks} />,
        };
      }),
  ].sort((a, b) => a.order - b.order);
  const preparation = [
    ...planning.map((stage) => ({
      order: stage.order,
      key: stage.id,
      item: <StageItem stage={stage} run={run} />,
    })),
    ...clarificationEvents.map((event, index) => ({
      order: event.seq,
      key: 'clarification:' + event.seq,
      item: <TimelineItem label="需求澄清" meta={'第 ' + (index + 1) + ' 轮 · 已请求'} />,
    })),
  ].sort((a, b) => a.order - b.order);
  const approve =
    !history &&
    plan &&
    !plan.locked &&
    ((run.status === 'awaiting_approval' && run.can_approve) ||
      plan.ai_revision?.pending ||
      (run.phase === 'revising_brief' && run.status === 'running'));
  const showContinue = finished && run.settlement === 'settled' && run.research_session_id;

  return (
    <div className="run-layout" data-inspector={inspector ? 'open' : 'closed'}>
      <div className="run-document">
        <PageTitle title={topic} back={back}>
          <div className="run-actions">
            {!history && run.can_cancel && (
              <Button
                variant="outline"
                disabled={commands.locked}
                onClick={() => {
                  if (window.confirm('取消这次研究？已产生的阶段产物仍可查看。'))
                    commands.submit({ action: 'cancel' });
                }}
              >
                <Square data-icon="inline-start" />
                取消研究
              </Button>
            )}
            {!history && run.retry_safe && finished && (
              <Button
                variant="outline"
                disabled={commands.locked}
                onClick={() =>
                  commands.submit({ action: 'retry', body: { state_version: run.state_version } })
                }
              >
                <RotateCcw data-icon="inline-start" />
                重试
              </Button>
            )}
            {showContinue && (
              <LinkButton to={'/?session=' + run.research_session_id}>延续研究</LinkButton>
            )}
            {!inspector && (
              <Button
                variant="ghost"
                size="icon"
                aria-label="展开运行摘要"
                onClick={() => setInspector(true)}
              >
                <PanelRightOpen />
              </Button>
            )}
          </div>
        </PageTitle>
        <div className="run-body">
          <div className="run-meta">
            <Status status={run.status} label={runStatus(run)} />
            <time dateTime={run.created_at}>{dateLabel(run.created_at)}</time>
            {history && <Link to={'/runs/' + id}>打开运行</Link>}
            <Link to={'/runs/' + id + '/execution'}>执行详情</Link>
          </div>
          {connection === 'reconnecting' && (
            <p className="connection-message" role="status">
              连接已中断，正在恢复订阅。
            </p>
          )}
          {connection === 'error' && <ErrorNotice error={error} retry={refresh} />}
          {commands.error !== undefined && !history && (
            <ErrorNotice error={commands.error} retry={refresh} />
          )}
          {!history && commands.pending && (
            <div className="pending-action" role="status">
              <span>
                {commands.busy
                  ? '正在提交操作…'
                  : commands.pending.state === 'accepted'
                    ? '已提交，等待服务端更新。'
                    : '提交结果待确认。'}
              </span>
              <Button variant="outline" size="sm" disabled={commands.busy} onClick={refresh}>
                读取最新状态
              </Button>
              {commands.resend && (
                <Button variant="outline" size="sm" onClick={commands.resend}>
                  再次发送同一请求
                </Button>
              )}
            </div>
          )}

          <section className="original-request" aria-label="研究输入">
            <h2>研究输入</h2>
            <div className="input-bubble">{topic}</div>
            {run.request.org_context && (
              <details className="input-context">
                <summary>企业背景</summary>
                <p>{run.request.org_context}</p>
              </details>
            )}
            {run.images.length > 0 && (
              <div className="retained-images">
                {run.images.map((image) => (
                  <RetainedImage key={image.position} id={id} image={image} />
                ))}
              </div>
            )}
            {run.image_context && (
              <details className="input-context">
                <summary>图片解析结果</summary>
                <Markdown body={run.image_context} />
              </details>
            )}
          </section>

          {(preparation.length > 0 || currentClarification || plan || researchItems.length > 0) && (
            <section className="research-timeline" id="research-stages">
              <h2>研究过程</h2>
              <div className="timeline">
                {preparation.map(({ key, item }) => (
                  <div key={key}>{item}</div>
                ))}
                {currentClarification && (
                  <TimelineItem label="等待澄清" active>
                    {run.can_clarify && !history && (
                      <Clarification
                        key={currentClarification.version}
                        challenge={currentClarification}
                        disabled={commands.locked}
                        send={commands.submit}
                      />
                    )}
                  </TimelineItem>
                )}
                {plan && (
                  <TimelineItem
                    label="Research Brief"
                    meta={
                      plan.locked
                        ? '已确认 · 版本 ' + plan.revision
                        : run.status === 'awaiting_approval'
                          ? '等待审批'
                          : '版本 ' + plan.revision
                    }
                    id={approve ? undefined : 'research-brief'}
                    state={plan.locked ? 'completed' : 'recorded'}
                    active={Boolean(approve)}
                  >
                    {approve ? (
                      <BriefApproval
                        key={plan.version}
                        plan={plan}
                        disabled={commands.locked || !run.can_approve}
                        send={commands.submit}
                      />
                    ) : (
                      <Markdown body={plan.brief} />
                    )}
                  </TimelineItem>
                )}
                {researchItems.map(({ key, item }) => (
                  <div key={key}>{item}</div>
                ))}
              </div>
            </section>
          )}
          {run.images.length > 0 && imageStage?.state === 'completed' && !run.image_context && (
            <p className="muted">图片识别阶段已完成，当前 Run 没有可显示的解析文本。</p>
          )}
          {run.report && <Report body={run.report} topic={topic} />}
          {(run.report || run.terminated_at || run.cancel_requested) && (
            <footer className="run-delivery">
              {run.report && <span>报告已生成</span>}
              {run.terminated_at && !run.retry_requested && (
                <span>运行已结束 · {dateLabel(run.terminated_at)}</span>
              )}
              <span>{finished ? '历史交付已完成' : '历史交付待确认'}</span>
              {run.settlement !== 'settled' && (
                <span>{run.settlement === 'pending' ? '清算中' : '外部结果待核对'}</span>
              )}
              {typeof run.error.kind === 'string' && (
                <Link to={'/runs/' + id + '/execution'}>查看执行错误</Link>
              )}
            </footer>
          )}
        </div>
      </div>
      {inspector && <Inspector run={run} stages={stages} dismiss={() => setInspector(false)} />}
    </div>
  );
}

export default function RunPage({ history = false }: { history?: boolean }) {
  const { runId = '' } = useParams();
  return <RunView key={String(history) + runId} id={runId} history={history} />;
}
