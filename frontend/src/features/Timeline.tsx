import { ChevronDown, CircleCheck, CircleDot, FileText, TriangleAlert } from 'lucide-react';
import type { ReactNode } from 'react';
import type { Run } from '@/api/client';
import type { FollowupTask, Review, RunEvent } from '@/api/events';
import { Markdown } from '@/components/Markdown';
import { dateLabel, durationLabel, roleNames, stageName } from '@/lib/presentation';
import type { Stage } from './stageTimeline';

const stateLabels = { running: '进行中', completed: '已完成', failed: '失败', recorded: '已记录' };
const toolStatus: Record<string, string> = {
  started: '调用中',
  running: '调用中',
  returned: '已返回',
  success: '已返回',
  completed: '已返回',
  failed: '失败',
  error: '失败',
  cancelled: '已取消',
  skipped: '已跳过',
  unobserved: '结果未确认',
};

export function TimelineItem({
  label,
  meta,
  active = false,
  children,
  id,
  state = 'recorded',
}: {
  label: string;
  meta?: string;
  active?: boolean;
  children?: ReactNode;
  id?: string;
  state?: Stage['state'];
}) {
  const Icon = state === 'completed' ? CircleCheck : state === 'failed' ? TriangleAlert : CircleDot;
  return (
    <div className="timeline-item" data-active={active} data-state={state} id={id}>
      <span className="timeline-marker" aria-hidden="true">
        <Icon size={17} strokeWidth={1.8} />
      </span>
      {children ? (
        <details open={active}>
          <summary className="timeline-summary">
            <span>{label}</span>
            {meta && <small>{meta}</small>}
            <ChevronDown size={16} className="disclosure" />
          </summary>
          <div className="timeline-content">{children}</div>
        </details>
      ) : (
        <div className="timeline-summary">
          <span>{label}</span>
          {meta && <small>{meta}</small>}
        </div>
      )}
    </div>
  );
}

function ReviewList({ title, items }: { title: string; items: string[] }) {
  return (
    items.length > 0 && (
      <div>
        <h4>{title}</h4>
        <ul>
          {items.map((item, index) => (
            <li key={index}>{item}</li>
          ))}
        </ul>
      </div>
    )
  );
}

function ReviewResult({ review }: { review: Review }) {
  return (
    <div className="review-result">
      <p>{review.research_complete ? '现有证据满足研究要求。' : 'Review 识别出尚待解决的问题。'}</p>
      <ReviewList title="研究缺口" items={review.research_gaps} />
      <ReviewList title="已确认发现" items={review.confirmed_findings} />
      <ReviewList title="未解决的主张" items={review.unresolved_claims} />
      <ReviewList title="证据冲突" items={review.conflicts} />
    </div>
  );
}

function ToolEvent({ event }: { event: RunEvent }) {
  return (
    <li>
      <div>
        <strong>{event.name === 'rag_search' ? '企业知识库检索' : event.name}</strong>
        {event.name === 'rag_search' && <small>rag_search</small>}
      </div>
      <span>{event.status && (toolStatus[event.status] ?? event.status)}</span>
      <time dateTime={event.timestamp}>
        {event.duration_ms == null ? dateLabel(event.timestamp) : durationLabel(event.duration_ms)}
      </time>
    </li>
  );
}

export function StageItem({ stage, run }: { stage: Stage; run: Run }) {
  const old = stage.generation < run.generation;
  const active = !old && stage.state === 'running' && run.status === 'running';
  const state = !active && stage.state === 'running' ? 'recorded' : stage.state;
  const label = stageName(stage.step);
  const round =
    stage.round && (stage.step.endsWith('_agent') || stage.step === 'research_review')
      ? '第 ' + stage.round + ' 轮 · '
      : '';
  const meta = round + (old ? '此前运行 · ' : '') + stateLabels[state];
  const tools = Array.from(stage.tools.values());
  const details = Boolean(stage.result || stage.review || tools.length || stage.error);
  return (
    <TimelineItem label={label} meta={meta} active={active} state={state}>
      {details ? (
        <>
          {stage.error && <p className="muted">错误类型 · {stage.error}</p>}
          {tools.length > 0 && (
            <div className="stage-tools">
              <h4>工具活动</h4>
              <ul className="tool-list">
                {tools.slice(-8).map((event) => (
                  <ToolEvent key={event.seq} event={event} />
                ))}
              </ul>
              {tools.length > 8 && (
                <p className="muted">显示最近 8 条。完整调用账本可在执行详情查看。</p>
              )}
            </div>
          )}
          {stage.review && <ReviewResult review={stage.review} />}
          {stage.result && (
            <div className="stage-artifact">
              <h4>
                <FileText size={15} />
                {label}报告
              </h4>
              <Markdown body={stage.result} />
            </div>
          )}
        </>
      ) : undefined}
    </TimelineItem>
  );
}

export function Followup({ event, tasks }: { event: RunEvent; tasks: FollowupTask[] }) {
  return (
    <TimelineItem
      label="定向补研"
      meta={[
        event.round && '第 ' + event.round + ' 轮',
        event.roles?.map((role) => roleNames[role] ?? role).join('、'),
      ]
        .filter(Boolean)
        .join(' · ')}
    >
      {tasks.length > 0 && (
        <ul className="followup-list">
          {tasks.map((task) => (
            <li key={task.task_id}>
              <strong>{task.objective}</strong>
              <span>
                {roleNames[task.target_role] ?? task.target_role} · {task.evidence_needed}
              </span>
              {task.reason && <small>{task.reason}</small>}
            </li>
          ))}
        </ul>
      )}
    </TimelineItem>
  );
}
