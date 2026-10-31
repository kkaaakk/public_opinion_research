import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { Plan, RunSnapshot } from '../protocol';
import { exportMarkdown, time } from './common';

type Submit = (action: 'approval' | 'clarification', body: object) => void;

function BriefEditor({
  plan,
  pending,
  busy,
  submit,
}: {
  plan: Plan;
  pending: boolean;
  busy: boolean;
  submit: Submit;
}) {
  const [draft, setDraft] = useState(plan.brief);
  const [editing, setEditing] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const changed = draft.trim() !== plan.brief;
  return (
    <section className="panel approval">
      <div className="panel-head">
        <strong>{pending ? '研究计划待审批' : 'Markdown Research Brief'}</strong>
        <span className="mono">
          v{plan.revision ?? 1} · {plan.version.slice(0, 12)}
          {plan.locked ? ' · 已锁定' : ''}
        </span>
        {!pending && (
          <button
            className="text-button"
            aria-expanded={expanded}
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? '收起 Brief' : '查看 Brief'}
          </button>
        )}
      </div>
      {(pending || expanded) && (
        <div className="approval-body">
          {editing ? (
            <label>
              编辑 Research Brief
              <textarea
                aria-label="编辑 Research Brief"
                rows={16}
                maxLength={30000}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
              />
            </label>
          ) : (
            <article className="markdown">
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                skipHtml
                components={{ img: ({ alt }) => <span>[图片：{alt}]</span> }}
              >
                {draft}
              </ReactMarkdown>
            </article>
          )}
          <small className="mono">
            SHA256: {plan.content_hash || '历史版本未提供'} · 到期：{time(plan.expires_at)}
          </small>
          <p className="note">
            研究规范作为任务输入；预算、工具权限和角色规则由系统配置控制。修改后须保存并审批新版本。上限为
            2500 个应用估算 Token。
          </p>
          <div className="form-actions">
            <button className="button" onClick={() => exportMarkdown(draft, 'research-brief')}>
              导出 Brief
            </button>
            {pending && (
              <>
                <button className="button" disabled={busy} onClick={() => setEditing(!editing)}>
                  {editing ? '预览 Markdown' : '编辑 Brief'}
                </button>
                {changed && (
                  <button
                    className="button"
                    disabled={busy}
                    onClick={() =>
                      submit('approval', {
                        plan_version: plan.version,
                        decision: 'edit',
                        brief_md: draft,
                      })
                    }
                  >
                    保存新版本
                  </button>
                )}
                <button
                  className="button"
                  disabled={busy}
                  onClick={() =>
                    submit('approval', { plan_version: plan.version, decision: 'reject' })
                  }
                >
                  拒绝计划
                </button>
                <button
                  className="button primary"
                  disabled={busy || changed}
                  onClick={() =>
                    submit('approval', { plan_version: plan.version, decision: 'approve' })
                  }
                >
                  批准执行
                </button>
              </>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function ClarificationForm({
  run,
  busy,
  submit,
}: {
  run: RunSnapshot;
  busy: boolean;
  submit: Submit;
}) {
  const pending = run.clarification!;
  const [answers, setAnswers] = useState<Record<string, string | string[]>>({});
  return (
    <section className="panel approval">
      <div className="panel-head">
        <h2>补充研究需求</h2>
        <span>等待回答 · 研究配额已释放</span>
      </div>
      <form
        className="approval-body research-form"
        onSubmit={(e) => {
          e.preventDefault();
          submit('clarification', {
            version: pending.version,
            answers: Object.fromEntries(
              Object.entries(answers).filter(([, v]) =>
                typeof v === 'string' ? v.trim().length > 0 : v.length > 0,
              ),
            ),
            skip: false,
          });
        }}
      >
        {pending.questions.map((q) => (
          <fieldset key={q.id}>
            <legend>{q.text}</legend>
            {q.kind === 'text' ? (
              <textarea
                aria-label={q.text}
                rows={3}
                maxLength={4000}
                value={String(answers[q.id] ?? '')}
                onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })}
              />
            ) : (
              q.options.map((option) => (
                <label key={option}>
                  <input
                    type={q.kind === 'single' ? 'radio' : 'checkbox'}
                    name={q.id}
                    value={option}
                    checked={
                      q.kind === 'single'
                        ? answers[q.id] === option
                        : (answers[q.id] ?? []).includes(option)
                    }
                    onChange={(e) => {
                      const current = Array.isArray(answers[q.id])
                        ? (answers[q.id] as string[])
                        : [];
                      setAnswers({
                        ...answers,
                        [q.id]:
                          q.kind === 'single'
                            ? option
                            : e.target.checked
                              ? [...current, option]
                              : current.filter((v) => v !== option),
                      });
                    }}
                  />
                  {option}
                </label>
              ))
            )}
          </fieldset>
        ))}
        <small>
          到期：{time(pending.expires_at)}
          。可以回答部分问题，或直接开始；未说明的信息将保留为未指定。
        </small>
        <div className="form-actions">
          <button
            type="button"
            className="button"
            disabled={busy}
            onClick={() =>
              submit('clarification', { version: pending.version, answers: {}, skip: true })
            }
          >
            跳过澄清，直接研究
          </button>
          <button
            className="button primary"
            disabled={busy || !Object.values(answers).some((v) => v.length)}
          >
            提交回答并继续
          </button>
        </div>
      </form>
    </section>
  );
}

export function ResearchSpec({
  run,
  busy,
  submit,
}: {
  run: RunSnapshot;
  busy: boolean;
  submit: Submit;
}) {
  if (run.status === 'awaiting_clarification' && run.clarification)
    return (
      <ClarificationForm key={run.clarification.version} run={run} busy={busy} submit={submit} />
    );
  if (run.plan)
    return (
      <BriefEditor
        key={run.plan.version}
        plan={run.plan}
        pending={run.status === 'awaiting_approval'}
        busy={busy}
        submit={submit}
      />
    );
  return null;
}
