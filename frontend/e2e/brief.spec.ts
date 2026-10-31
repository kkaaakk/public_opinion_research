import { test, expect } from '@playwright/test';

test('same Run survives clarification reload, Markdown edit, version approval and delivery', async ({
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
  await page.getByLabel('研究主题').fill('browser-clarify brand risk with evidence');
  await page.getByRole('button', { name: '创建持久 Run' }).click();
  await expect(page.getByRole('heading', { name: '补充研究需求' })).toBeVisible();
  const id = await page.locator('.run-identity strong').textContent();
  await page.reload();
  await authenticate();
  await expect(page.locator('.run-identity strong')).toHaveText(id!);
  await page.getByLabel('Which brand?').fill('Brand A; retain all sources');
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
