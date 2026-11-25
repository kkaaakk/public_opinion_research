import { Check, Pencil, Sparkles } from 'lucide-react';
import { useState } from 'react';
import type { RunCommand, Schema } from '@/api/client';
import { Markdown } from '@/components/Markdown';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { dateLabel } from '@/lib/presentation';

type InteractionProps = { disabled: boolean; send: (command: RunCommand) => void };

export function Clarification({
  challenge,
  disabled,
  send,
}: InteractionProps & { challenge: Schema['Clarification'] }) {
  const [answers, setAnswers] = useState<Record<string, string | string[]>>({});
  const chosen = Object.fromEntries(
    Object.entries(answers).filter(([, value]) =>
      typeof value === 'string' ? Boolean(value.trim()) : value.length > 0,
    ),
  );
  return (
    <form
      className="interaction-card"
      onSubmit={(event) => {
        event.preventDefault();
        send({
          action: 'clarification',
          body: { version: challenge.version, answers: chosen, skip: false },
        });
      }}
    >
      <div className="interaction-title">
        <h3>需求澄清</h3>
        <span>
          第 {challenge.round} / {challenge.max_rounds} 轮
        </span>
      </div>
      <p className="muted">可以只回答部分问题。截止 {dateLabel(challenge.expires_at)}</p>
      {challenge.questions.map((question) => (
        <fieldset className="question" key={question.id} disabled={disabled}>
          <legend>{question.text}</legend>
          {question.kind === 'text' ? (
            <Textarea
              aria-label={question.text}
              rows={2}
              maxLength={4000}
              value={String(answers[question.id] ?? '')}
              onChange={(event) => setAnswers({ ...answers, [question.id]: event.target.value })}
            />
          ) : (
            question.options.map((option) => {
              const multiple = question.kind === 'multiple';
              const selected = answers[question.id];
              const values = Array.isArray(selected) ? selected : [];
              return (
                <label className="choice" key={option}>
                  <input
                    type={multiple ? 'checkbox' : 'radio'}
                    name={question.id}
                    checked={multiple ? values.includes(option) : selected === option}
                    onChange={(event) =>
                      setAnswers({
                        ...answers,
                        [question.id]: multiple
                          ? event.target.checked
                            ? [...values, option]
                            : values.filter((value) => value !== option)
                          : option,
                      })
                    }
                  />
                  <span>{option}</span>
                </label>
              );
            })
          )}
        </fieldset>
      ))}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" disabled={disabled || !Object.keys(chosen).length}>
          提交回答
        </Button>
        <Button
          variant="outline"
          type="button"
          disabled={disabled}
          onClick={() =>
            send({
              action: 'clarification',
              body: { version: challenge.version, answers: {}, skip: true },
            })
          }
        >
          跳过澄清
        </Button>
      </div>
    </form>
  );
}

export function BriefApproval({
  plan,
  disabled,
  send,
}: InteractionProps & { plan: Schema['Brief'] }) {
  const [draft, setDraft] = useState(plan.brief);
  const [editing, setEditing] = useState(false);
  const [feedback, setFeedback] = useState('');
  const dirty = draft.trim() !== plan.brief.trim();
  const revision = plan.ai_revision;
  const remaining = revision ? revision.limit - revision.used : null;
  const held = disabled || Boolean(revision?.pending);

  function decide(
    decision: Schema['Approval']['decision'],
    fields: Partial<Schema['Approval']> = {},
  ) {
    send({ action: 'approval', body: { ...fields, decision, version: plan.version } });
  }

  return (
    <section className="interaction-card" id="research-brief">
      <div className="interaction-title">
        <h3>Research Brief</h3>
        <span>
          版本 {plan.revision} · 截止 {dateLabel(plan.expires_at)}
        </span>
      </div>
      {editing ? (
        <div className="field">
          <label htmlFor="brief-draft">编辑 Markdown</label>
          <Textarea
            id="brief-draft"
            rows={14}
            maxLength={30_000}
            value={draft}
            disabled={held}
            onChange={(event) => setDraft(event.target.value)}
            className="mono-editor"
          />
          <div className="flex flex-wrap gap-2">
            <Button
              disabled={held || !dirty || !draft.trim()}
              onClick={() => decide('edit', { brief_md: draft.trim() })}
            >
              保存编辑
            </Button>
            <Button
              disabled={held}
              variant="outline"
              onClick={() => {
                setDraft(plan.brief);
                setEditing(false);
              }}
            >
              放弃编辑
            </Button>
          </div>
        </div>
      ) : (
        <div className="brief-content">
          <Markdown body={plan.brief} />
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        <Button disabled={held || dirty} onClick={() => decide('approve')}>
          <Check data-icon="inline-start" />
          批准
        </Button>
        {!editing && (
          <Button disabled={held} variant="outline" onClick={() => setEditing(true)}>
            <Pencil data-icon="inline-start" />
            编辑
          </Button>
        )}
        <Button
          disabled={held}
          variant="outline"
          onClick={() => {
            if (window.confirm('拒绝这份 Brief 并结束该研究？')) decide('reject');
          }}
        >
          拒绝
        </Button>
      </div>
      {dirty && <p className="muted">请先保存编辑，再批准已保存的版本。</p>}
      {remaining !== null && (
        <div className="brief-revise">
          <div className="interaction-title">
            <label htmlFor="brief-feedback">AI 修改反馈</label>
            <span>
              剩余 {remaining} / {revision?.limit} 次
            </span>
          </div>
          {revision?.error && (
            <p className="muted" role="status">
              上次修改未完成 · {revision.error}
            </p>
          )}
          <Textarea
            id="brief-feedback"
            rows={3}
            maxLength={4000}
            placeholder="说明要修改的范围、优先事项或约束"
            value={feedback}
            disabled={held || remaining <= 0}
            onChange={(event) => setFeedback(event.target.value)}
          />
          <Button
            variant="outline"
            className="self-start"
            disabled={held || dirty || remaining <= 0 || !feedback.trim()}
            onClick={() => decide('revise', { feedback: feedback.trim() })}
          >
            <Sparkles data-icon="inline-start" />
            {revision?.pending ? 'AI 修改中…' : '请求 AI 修改'}
          </Button>
        </div>
      )}
    </section>
  );
}
