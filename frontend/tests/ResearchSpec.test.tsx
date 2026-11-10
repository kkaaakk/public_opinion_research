import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { ResearchSpec } from '../src/components/ResearchSpec';
import { parseSnapshot } from '../src/protocol';
import { snapshot } from './fixtures';

const clarification = {
  version: 'c'.repeat(64),
  expires_at: '2026-11-02T00:00:00Z',
  round: 1,
  max_rounds: 3,
  questions: [
    { id: 'region', text: 'Which region?', kind: 'single' as const, options: ['China', 'Global'] },
    {
      id: 'sources',
      text: 'Which sources?',
      kind: 'multiple' as const,
      options: ['News', 'Social'],
    },
    { id: 'notes', text: 'Other constraints?', kind: 'text' as const, options: [] },
  ],
};
it('renders persisted clarification and submits mixed answers bound to its version', async () => {
  const submit = vi.fn();
  const user = userEvent.setup();
  const run = parseSnapshot(
    snapshot({ status: 'awaiting_clarification', clarification, events_paused: true }),
  );
  render(<ResearchSpec run={run} busy={false} submit={submit} />);
  expect(screen.getByText(/澄清第 1\/3 轮/)).toBeVisible();
  await user.click(screen.getByLabelText('China'));
  await user.click(screen.getByLabelText('News'));
  await user.type(screen.getByLabelText('Other constraints?'), 'Do not assume');
  await user.click(screen.getByRole('button', { name: '提交回答并继续' }));
  expect(submit).toHaveBeenCalledWith('clarification', {
    version: clarification.version,
    skip: false,
    answers: { region: 'China', sources: ['News'], notes: 'Do not assume' },
  });
});
it('resets answers and renders only the new round even when question IDs are reused', async () => {
  const submit = vi.fn();
  const user = userEvent.setup();
  const run = snapshot({ status: 'awaiting_clarification', clarification });
  const view = render(<ResearchSpec run={run} busy={false} submit={submit} />);
  await user.type(screen.getByLabelText('Other constraints?'), 'Old answer');

  const next = {
    ...clarification,
    version: 'e'.repeat(64),
    round: 2,
    questions: [{ id: 'notes', text: 'New constraints?', kind: 'text' as const, options: [] }],
  };
  view.rerender(
    <ResearchSpec run={{ ...run, clarification: next }} busy={false} submit={submit} />,
  );
  expect(screen.queryByLabelText('Other constraints?')).toBeNull();
  expect(screen.getByText(/澄清第 2\/3 轮/)).toBeVisible();
  expect(screen.getByLabelText('New constraints?')).toHaveValue('');
  expect(screen.getByRole('button', { name: '提交回答并继续' })).toBeDisabled();
  await user.type(screen.getByLabelText('New constraints?'), 'New answer');
  await user.click(screen.getByRole('button', { name: '提交回答并继续' }));
  expect(submit).toHaveBeenCalledWith('clarification', {
    version: next.version,
    skip: false,
    answers: { notes: 'New answer' },
  });
});
it('skip does not submit stale local answers', async () => {
  const submit = vi.fn();
  const user = userEvent.setup();
  render(
    <ResearchSpec
      run={snapshot({ status: 'awaiting_clarification', clarification })}
      busy={false}
      submit={submit}
    />,
  );
  await user.click(screen.getByLabelText('China'));
  await user.click(screen.getByRole('button', { name: '跳过澄清，继续规划' }));
  expect(submit).toHaveBeenCalledWith('clarification', {
    version: clarification.version,
    skip: true,
    answers: {},
  });
});
it('edits Markdown, prevents approval of unsaved text, then approves only the new version', async () => {
  const submit = vi.fn();
  const user = userEvent.setup();
  const plan = {
    version: 'b'.repeat(64),
    revision: 1,
    content_hash: 'd'.repeat(64),
    brief: '# Research\n\n## Goal\nOriginal scope',
    sections: [],
    expires_at: clarification.expires_at,
  };
  const view = render(
    <ResearchSpec
      run={snapshot({ status: 'awaiting_approval', plan })}
      busy={false}
      submit={submit}
    />,
  );
  await user.click(screen.getByRole('button', { name: '编辑 Brief' }));
  await user.type(screen.getByLabelText('编辑 Research Brief'), ' extra constraint');
  expect(screen.getByRole('button', { name: '批准执行' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: '保存新版本' }));
  const brief = plan.brief + ' extra constraint';
  expect(submit).toHaveBeenLastCalledWith('approval', {
    plan_version: plan.version,
    decision: 'edit',
    brief_md: brief,
  });
  view.rerender(
    <ResearchSpec
      run={snapshot({
        status: 'awaiting_approval',
        plan: { ...plan, brief, version: 'e'.repeat(64), revision: 2 },
      })}
      busy={false}
      submit={submit}
    />,
  );
  await user.click(screen.getByRole('button', { name: '批准执行' }));
  expect(submit).toHaveBeenLastCalledWith('approval', {
    plan_version: 'e'.repeat(64),
    decision: 'approve',
  });
});
it('submits revision feedback against its attempt and restores pending and limit states', async () => {
  const user = userEvent.setup();
  const submit = vi.fn();
  const plan = {
    version: 'f'.repeat(64),
    revision: 2,
    brief: '# Brief\n\n## Scope\nCurrent research scope',
    sections: [],
    expires_at: clarification.expires_at,
    ai_revision: { attempt: 1, used: 1, limit: 3, pending: false },
  };
  const run = parseSnapshot(snapshot({ status: 'awaiting_approval', plan }));
  const view = render(<ResearchSpec run={run} busy={false} submit={submit} />);
  await user.click(screen.getByRole('button', { name: 'AI 修改 Brief' }));
  await user.type(screen.getByLabelText('AI 修改意见'), 'Compare competitors.');
  await user.click(screen.getByRole('button', { name: '提交 AI 修改' }));
  expect(submit).toHaveBeenCalledWith('approval', {
    plan_version: plan.version,
    decision: 'revise',
    revision_attempt: 1,
    feedback: 'Compare competitors.',
  });
  view.rerender(
    <ResearchSpec
      run={parseSnapshot(
        snapshot({
          status: 'running',
          plan: { ...plan, ai_revision: { ...plan.ai_revision, pending: true } },
        }),
      )}
      busy={false}
      submit={submit}
    />,
  );
  expect(screen.getByText('AI 正在修改 Brief')).toBeVisible();
  expect(screen.getByText('Current research scope')).toBeVisible();
  expect(screen.queryByRole('button', { name: '批准执行' })).toBeNull();

  view.rerender(
    <ResearchSpec
      run={snapshot({
        status: 'awaiting_approval',
        plan: { ...plan, ai_revision: { ...plan.ai_revision, attempt: 2, error: 'model_error' } },
      })}
      busy={false}
      submit={submit}
    />,
  );
  expect(screen.getByRole('alert')).toHaveTextContent('已保留当前 Brief');
  expect(screen.getByRole('button', { name: '提交 AI 修改' })).toBeEnabled();

  view.rerender(
    <ResearchSpec
      run={snapshot({
        status: 'awaiting_approval',
        plan: {
          ...plan,
          version: 'e'.repeat(64),
          revision: 4,
          ai_revision: { ...plan.ai_revision, attempt: 4, used: 3 },
        },
      })}
      busy={false}
      submit={submit}
    />,
  );
  expect(screen.getByText(/AI 修改：3 \/ 3 次/)).toBeVisible();
  expect(screen.getByRole('button', { name: 'AI 修改 Brief' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '编辑 Brief' })).toBeEnabled();
  expect(screen.getByRole('button', { name: '批准执行' })).toBeEnabled();
});

it('renders Markdown with raw HTML disabled and no active remote image', async () => {
  render(
    <ResearchSpec
      run={snapshot({
        status: 'completed',
        plan: {
          version: 'b'.repeat(64),
          brief: '# Brief\n<script>alert(1)</script>\n![x](https://example.org/tracking.png)',
          sections: [],
          expires_at: clarification.expires_at,
          locked: true,
        },
      })}
      busy={false}
      submit={vi.fn()}
    />,
  );
  await userEvent.setup().click(screen.getByRole('button', { name: '查看 Brief' }));
  expect(document.querySelector('script,img')).toBeNull();
  expect(screen.queryByRole('button', { name: '编辑 Brief' })).toBeNull();
});
