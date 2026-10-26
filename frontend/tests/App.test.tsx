import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeAll, expect, it, vi } from 'vitest';
import App from '../src/App';
import { Reports } from '../src/components/Reports';
import { persist, restore } from '../src/session';
import { ID, json, snapshot } from './fixtures';
vi.mock('../src/components/WorkflowCanvas', () => ({
  WorkflowCanvas: () => <div>Workflow Canvas</div>,
}));
beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute('open', '');
  };
});

it('stores the create identity before POST and reuses its exact body after response loss', async () => {
  const requests: { key: string | null; body: BodyInit | null | undefined }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>(async (input, init) => {
      if (String(input) === '/api/runs') {
        const key = new Headers(init?.headers).get('Idempotency-Key');
        expect(restore().create?.key).toBe(key);
        requests.push({ key, body: init?.body });
        if (requests.length === 1) throw new TypeError('response lost');
        return json(snapshot({ status: 'completed', events_complete: true }));
      }
      return json(snapshot({ status: 'completed', events_complete: true }));
    }),
  );
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole('button', { name: '新建研究' }));
  await user.type(screen.getByLabelText('研究主题'), 'Brand research');
  await user.click(screen.getByRole('button', { name: '创建持久 Run' }));
  await screen.findByRole('button', { name: '确认原创建请求' });
  await user.click(screen.getByLabelText('关闭'));
  await user.click(screen.getByRole('button', { name: '确认原创建请求' }));
  await waitFor(() => expect(requests).toHaveLength(2));
  expect(requests[1]).toEqual(requests[0]);
  await waitFor(() => expect(restore().runId).toBe(ID));
  expect(restore().create).toBeUndefined();
});
it('binds approval to the exact plan and keeps the token out of persistent storage', async () => {
  persist({ runId: ID });
  let approved = false;
  let approvalBody: unknown;
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>(async (input, init) => {
      if (String(input).endsWith('/approval')) {
        expect(new Headers(init?.headers).get('Authorization')).toBe('Bearer private-token');
        approvalBody = JSON.parse(String(init?.body));
        approved = true;
      }
      return json(
        approved
          ? snapshot({ status: 'completed', events_complete: true })
          : snapshot({
              status: 'awaiting_approval',
              events_paused: true,
              plan: {
                version: 'b'.repeat(64),
                brief: 'Immutable plan',
                sections: [{ name: 'Summary' }],
                expires_at: '2026-10-26T00:00:00Z',
              },
            }),
      );
    }),
  );
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole('button', { name: '访问设置' }));
  await user.type(screen.getByLabelText('API 访问令牌'), 'private-token');
  await user.click(screen.getByRole('button', { name: '应用并连接' }));
  await user.click(await screen.findByRole('button', { name: '批准执行' }));
  await waitFor(() =>
    expect(approvalBody).toEqual({ plan_version: 'b'.repeat(64), decision: 'approve' }),
  );
  expect(sessionStorage.getItem('por-studio-session-v2')).not.toContain('private-token');
});
it('resends the same retry grant and observed version after a lost acknowledgement', async () => {
  persist({ runId: ID });
  const grants: { key: string | null; body: string }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>(async (input, init) => {
      if (String(input).endsWith('/retry')) {
        const grant = {
          key: new Headers(init?.headers).get('Idempotency-Key'),
          body: String(init?.body),
        };
        expect(restore().retry?.key).toBe(grant.key);
        grants.push(grant);
        if (grants.length === 1) throw new TypeError('ACK lost');
      }
      return json(
        snapshot({
          status: 'failed',
          native_status: 'ERROR',
          retry_safe: true,
          state_version: grants.length ? 21 : 12,
          generation: grants.length ? 1 : 0,
          events_complete: true,
        }),
      );
    }),
  );
  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByRole('button', { name: '安全重试' }));
  await user.click(await screen.findByRole('button', { name: '确认原重试请求' }));
  await waitFor(() => expect(grants).toHaveLength(2));
  expect(grants[1]).toEqual(grants[0]);
  expect(JSON.parse(grants[0].body)).toEqual({ state_version: 12 });
  await waitFor(() => expect(restore().retry).toBeUndefined());
});
it('keeps all mutations disabled during read-only replay', async () => {
  persist({ runId: ID });
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>(async (input) =>
      String(input).includes('event-page')
        ? json({
            items: [
              {
                protocol_version: 1,
                run_id: ID,
                seq: 1,
                timestamp: '2026-10-25T00:00:00Z',
                type: 'run_status',
                status: 'failed',
              },
            ],
            next_cursor: 1,
            has_more: false,
          })
        : json(
            snapshot({
              status: 'failed',
              native_status: 'ERROR',
              retry_safe: true,
              events_complete: true,
              event_cursor: 1,
            }),
          ),
    ),
  );
  render(<App />);
  const retry = await screen.findByRole('button', { name: '安全重试' });
  await waitFor(() => expect(screen.getByLabelText('回放游标')).not.toBeDisabled());
  fireEvent.change(screen.getByLabelText('回放游标'), { target: { value: '0' } });
  expect(retry).toBeDisabled();
});
it('renders Markdown without raw HTML, external image requests or javascript links', () => {
  const { container } = render(
    <Reports
      reports={[
        {
          id: 'final',
          label: 'Report',
          content:
            '<script>alert(1)</script>\n\n![remote](https://tracker/image)\n\n[bad](javascript:alert(1))',
        },
      ]}
    />,
  );
  expect(container.querySelector('script')).toBeNull();
  expect(container.querySelector('img')).toBeNull();
  expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
});
it('does not create work if request identity cannot be saved', async () => {
  const transport = vi.fn<typeof fetch>();
  vi.stubGlobal('fetch', transport);
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
    throw new DOMException('Denied', 'SecurityError');
  });
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole('button', { name: '新建研究' }));
  await user.type(screen.getByLabelText('研究主题'), 'Topic');
  await user.click(screen.getByRole('button', { name: '创建持久 Run' }));
  await screen.findByText('浏览器会话存储不可用；请启用后再创建研究。');
  expect(transport).not.toHaveBeenCalled();
});
