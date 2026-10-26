import { test, expect, type Page } from '@playwright/test';
const pageErrors = new WeakMap<Page, string[]>();
test.beforeEach(({ page }) => {
  const errors: string[] = [];
  pageErrors.set(page, errors);
  page.on('pageerror', (error) => errors.push(error.message));
});
test.afterEach(({ page }) => {
  expect(pageErrors.get(page) ?? []).toEqual([]);
});

async function authenticate(page: Page) {
  await page.locator('.topbar').getByRole('button', { name: '访问设置', exact: true }).click();
  await page.getByLabel('API 访问令牌').fill('studio-browser-test-only');
  await page.getByRole('button', { name: '应用并连接' }).click();
}
async function create(page: Page, topic: string, approval = false) {
  await page.getByRole('button', { name: '新建研究', exact: true }).click();
  await page.getByLabel('研究主题').fill(topic);
  await page.getByLabel('执行前审批计划').setChecked(approval);
  await page.getByRole('button', { name: '创建持久 Run' }).click();
  await expect(page.locator('.run-identity strong')).toHaveText(/^[a-f0-9]{32}$/);
  return (await page.locator('.run-identity strong').textContent())!;
}

test('actual FastAPI → DBOS → SDK → Graph: approval, trace, replay, refresh, evidence, report and history', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto('/studio');
  await expect(page).toHaveTitle(/Agent Execution Studio/);
  await expect(page.getByRole('heading', { name: '研究执行观测台' })).toBeVisible();
  await authenticate(page);
  const id = await create(page, 'Studio controlled real-chain research', true);
  await expect(page.getByRole('button', { name: '批准执行', exact: true })).toBeVisible();
  await expect(page.locator('.state-waiting').filter({ hasText: 'Risk Assessment' })).toBeVisible();
  await page.getByRole('button', { name: '批准执行', exact: true }).click();
  await expect(page.locator('.connection')).toHaveText('事件已交付');
  await expect(page.locator('.node-title').filter({ hasText: 'Report Generation' })).toBeVisible();
  await page.locator('.react-flow__node').filter({ hasText: 'Report Generation' }).focus();
  await page.keyboard.press('Delete');
  await expect(page.locator('.react-flow__node')).toHaveCount(9);
  await page.getByRole('tab', { name: 'SDK 追踪', exact: true }).click();
  await expect(page.locator('.span-row')).toHaveCount(8); // six actual SDK calls + two tools
  await page.locator('.span-row').first().click();
  await expect(page.locator('.selected-detail')).toContainText('invocation_id');
  await page.screenshot({ path: '../.tmp/studio-sdk-desktop.png', fullPage: true });
  await page.getByRole('tab', { name: 'DBOS', exact: true }).click();
  await expect(page.locator('.native-workflow')).not.toHaveCount(0);
  await expect(page.locator('.native-steps')).toContainText(id);
  await page.getByRole('tab', { name: '证据', exact: true }).click();
  await expect(page.locator('.evidence-item')).not.toHaveCount(0);
  await page.getByLabel('证据类型').selectOption('Evidence');
  await expect(page.locator('.evidence-item').first()).toContainText('Evidence');
  await page.locator('.evidence-item').first().click();
  await expect(page.locator('.evidence-detail')).toContainText('引用与关系');
  await expect(page.locator('.relation')).not.toHaveCount(0);
  await page.getByRole('tab', { name: '报告', exact: true }).click();
  await expect(page.locator('.markdown')).toContainText('FINAL brand recall report');
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Markdown 导出' }).click();
  expect((await download).suggestedFilename()).toBe('final.md');
  const cursor = Number(await page.getByLabel('回放游标').getAttribute('max'));
  expect(cursor).toBeGreaterThan(80);
  await page.getByLabel('回放游标').fill('0');
  await page.getByRole('tab', { name: '工作流', exact: true }).click();
  await expect(page.locator('.state-completed')).toHaveCount(0);
  await page.getByRole('button', { name: '返回实时状态' }).click();
  await expect(page.locator('.state-completed')).toHaveCount(9);
  await page.screenshot({ path: '../.tmp/studio-workflow-desktop.png', fullPage: true });
  const posts: string[] = [];
  page.on('request', (r) => {
    if (r.method() === 'POST') posts.push(r.url());
  });
  await page.reload();
  await authenticate(page);
  await expect(page.locator('.connection')).toHaveText('事件已交付');
  expect(Number(await page.getByLabel('回放游标').getAttribute('max'))).toBe(cursor);
  expect(posts).toEqual([]);
  await page.getByRole('button', { name: '历史归档', exact: true }).click();
  await expect(page.locator('.archive-item').filter({ hasText: id })).toBeVisible();
  await page.locator('.archive-item').filter({ hasText: id }).click();
  await expect(
    page.getByText('报告归档与 DBOS 执行事件独立。此视图不代表完整 Trace。'),
  ).toBeVisible();
  await expect(page.locator('.markdown')).toContainText('FINAL brand recall report');
  expect(errors).toEqual([]);
});

test('safe retry stays on the same Run and preserves successful Producer invocations', async ({
  page,
}) => {
  await page.goto('/studio');
  await authenticate(page);
  const id = await create(page, 'retry-once');
  await expect(page.getByRole('button', { name: '安全重试', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '安全重试', exact: true }).click();
  await expect(page.locator('.connection')).toHaveText('事件已交付');
  await expect(page.locator('.run-identity strong')).toHaveText(id);
  await expect(page.locator('.run-context')).toContainText('Gen 1');
  await page.locator('.react-flow__node').filter({ hasText: 'Public Signal' }).click();
  await expect(page.locator('.instance-list .instance')).toHaveCount(1);
  await page.locator('.react-flow__node').filter({ hasText: 'Research Review' }).click();
  await expect(page.locator('.instance-list .instance')).toHaveCount(2);
  await expect(page.locator('.instance-list')).toContainText('失败');
});

test('cancel is explicit and a narrow viewport retains real navigation and controls', async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/studio');
  await authenticate(page);
  await page.getByRole('button', { name: '打开导航', exact: true }).click();
  const id = await create(page, 'cancel-run');
  await page.getByRole('button', { name: '关闭导航', exact: true }).click();
  await expect(page.locator('.run-context')).toContainText('Public Signal');
  await page.getByRole('button', { name: '取消', exact: true }).click();
  await expect(page.locator('.connection')).toHaveText('事件已交付');
  await expect(page.locator('.run-identity strong')).toHaveText(id);
  await expect(page.locator('.run-overview')).toContainText('已取消');
  await page.screenshot({ path: '../.tmp/studio-mobile.png', fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole('button', { name: '打开导航', exact: true }).click();
  await expect(page.getByRole('button', { name: '历史归档', exact: true })).toBeVisible();
});
test('a broken SSE connection automatically reconnects to the same Run', async ({ page }) => {
  let streams = 0;
  let creates = 0;
  await page.route('**/api/runs/*/events', async (route) => {
    streams++;
    if (streams === 1) await route.abort('connectionreset');
    else await route.continue();
  });
  page.on('request', (request) => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/runs') creates++;
  });
  await page.goto('/studio');
  await authenticate(page);
  const id = await create(page, 'SSE reconnect proof');
  await expect(page.locator('.connection')).toHaveText('事件已交付');
  await expect(page.locator('.run-identity strong')).toHaveText(id);
  expect(streams).toBeGreaterThan(1);
  expect(creates).toBe(1);
  const state = await page.request.get(`/api/runs/${id}`, {
    headers: { Authorization: 'Bearer studio-browser-test-only' },
  });
  expect(Number(await page.getByLabel('回放游标').getAttribute('max'))).toBe(
    (await state.json()).event_cursor,
  );
});
