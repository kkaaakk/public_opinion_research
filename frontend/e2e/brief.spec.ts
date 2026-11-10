import { test, expect } from '@playwright/test';

test('same Run survives clarification and AI revision reloads, then Brief approval', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  async function authenticate() {
    await page.locator('.topbar').getByRole('button', { name: '访问设置', exact: true }).click();
    await page.getByLabel('API 访问令牌').fill('studio-browser-test-only');
    await page.getByRole('button', { name: '应用并连接' }).click();
  }
  await page.goto('/studio');
  await expect(page).toHaveTitle(/Agent Execution Studio/);
  await authenticate();
  await page.getByRole('button', { name: '新建研究', exact: true }).click();
  await page.getByLabel('研究主题').fill('browser-clarify-multi brand risk with evidence');
  await page.getByRole('button', { name: '创建持久 Run' }).click();
  await expect(page.getByRole('heading', { name: '补充研究需求' })).toBeVisible();
  await expect(page.getByText(/澄清第 1\/3 轮/)).toBeVisible();
  const id = await page.locator('.run-identity strong').textContent();
  await page.reload();
  await authenticate();
  await expect(page.locator('.run-identity strong')).toHaveText(id!);
  await page.getByLabel('Which brand?').fill('Brand A; retain all sources');
  await page.getByRole('button', { name: '提交回答并继续' }).click();
  await expect(page.getByText(/澄清第 2\/3 轮/)).toBeVisible();
  await expect(page.getByLabel('Which brand?')).toHaveCount(0);
  await page.reload();
  await authenticate();
  await expect(page.locator('.run-identity strong')).toHaveText(id!);
  await expect(page.getByText(/澄清第 2\/3 轮/)).toBeVisible();
  await expect(page.getByLabel('Which brand?')).toHaveCount(0);
  await page.screenshot({
    path: (process.env.BRIEF_SCREENSHOT_DIR ?? '../.tmp') + '/clarification-round-two.png',
  });
  const choice = page.getByRole('radio', { name: 'Last 7 days' });
  expect((await choice.boundingBox())?.width).toBeLessThan(32);
  await choice.check();
  await page.getByRole('button', { name: '提交回答并继续' }).click();
  await expect(page.getByRole('button', { name: '批准执行', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '编辑 Brief', exact: true }).click();
  const editor = page.getByLabel('编辑 Research Brief');
  const brief =
    (await editor.inputValue()) + '\nAdditional user constraint: explicitly report unknown facts.';
  await editor.fill(brief);
  await expect(page.getByRole('button', { name: '批准执行', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '保存新版本', exact: true }).click();
  await expect(page.locator('.approval .mono').first()).toContainText('v2');
  await page.getByRole('button', { name: 'AI 修改 Brief', exact: true }).click();
  const feedback = 'browser-delay-revision: compare competitor after-sales complaints.';
  await page.getByLabel('AI 修改意见').fill(feedback);
  await page.getByRole('button', { name: '提交 AI 修改', exact: true }).click();
  await expect(page.getByText('AI 正在修改 Brief')).toBeVisible();
  await page.reload();
  await authenticate();
  await expect(page.locator('.run-identity strong')).toHaveText(id!);
  await expect(page.getByText('AI 正在修改 Brief')).toBeVisible();
  await expect(page.getByRole('button', { name: '批准执行', exact: true })).toHaveCount(0);
  await expect(page.locator('.approval .mono').first()).toContainText('v3');
  await expect(page.getByText(/AI 修改：1 \/ 3 次/)).toBeVisible();
  await expect(page.locator('.approval .markdown')).toContainText(feedback);
  await expect(page.locator('.approval .markdown')).toContainText(
    'explicitly report unknown facts',
  );
  await page.screenshot({
    path: process.env.BRIEF_SCREENSHOT_DIR
      ? process.env.BRIEF_SCREENSHOT_DIR + '/brief-approval.png'
      : '../.tmp/brief-approval.png',
    fullPage: true,
  });
  await page.getByRole('button', { name: '批准执行', exact: true }).click();
  await expect(page.locator('.connection')).toHaveText('事件已交付');
  await expect(page.locator('.approval .mono').first()).toContainText('已锁定');
  await page.getByRole('tab', { name: '报告', exact: true }).click();
  await expect(page.locator('.report-panel .markdown')).not.toBeEmpty();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: process.env.BRIEF_SCREENSHOT_DIR
      ? process.env.BRIEF_SCREENSHOT_DIR + '/brief-mobile.png'
      : '../.tmp/brief-mobile.png',
    fullPage: true,
  });
  expect(errors).toEqual([]);
});
