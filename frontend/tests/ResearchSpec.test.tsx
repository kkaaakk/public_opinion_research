import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { ResearchSpec } from '../src/components/ResearchSpec';
import { parseSnapshot } from '../src/protocol';
import { snapshot } from './fixtures';

const clarification = {
  version: 'c'.repeat(64),
  expires_at: '2026-11-02T00:00:00Z',
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
  await user.click(screen.getByRole('button', { name: '跳过澄清，直接研究' }));
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
