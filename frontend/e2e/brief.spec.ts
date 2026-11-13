import { randomUUID } from 'node:crypto';
import { test, expect, type Page } from '@playwright/test';

async function authenticateStudio(page: Page) {
  await page.locator('.topbar').getByRole('button', { name: '访问设置', exact: true }).click();
  await page.getByLabel('API 访问令牌').fill('studio-browser-test-only');
  await page.getByRole('button', { name: '应用并连接' }).click();
}

test('same Run survives clarification and AI revision reloads, then Brief approval', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/studio');
  await expect(page).toHaveTitle(/Agent Execution Studio/);
  await authenticateStudio(page);
  await page.getByRole('button', { name: '新建研究', exact: true }).click();
  await page.getByLabel('研究主题').fill('browser-clarify-multi brand risk with evidence');
  await page.getByRole('button', { name: '创建持久 Run' }).click();
  await expect(page.getByRole('heading', { name: '补充研究需求' })).toBeVisible();
  await expect(page.getByText(/澄清第 1\/3 轮/)).toBeVisible();
  const id = await page.locator('.run-identity strong').textContent();
  await page.reload();
  await authenticateStudio(page);
  await expect(page.locator('.run-identity strong')).toHaveText(id!);
  await page.getByLabel('Which brand?').fill('Brand A; retain all sources');
  await page.getByRole('button', { name: '提交回答并继续' }).click();
  await expect(page.getByText(/澄清第 2\/3 轮/)).toBeVisible();
  await expect(page.getByLabel('Which brand?')).toHaveCount(0);
  await page.reload();
  await authenticateStudio(page);
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
  await authenticateStudio(page);
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

for (const surface of ['studio', 'classic'] as const) {
  test(
    surface + ' confirms a lost revision response after reload and retries a settled failure',
    async ({ page }) => {
      const studio = surface === 'studio';
      const errors: string[] = [];
      page.on('pageerror', (error) => errors.push(error.message));
      if (!studio) {
        await page.route('https://cdn.jsdelivr.net/**', (route) =>
          route.fulfill({ contentType: 'application/javascript', body: '' }),
        );
      }
      async function authenticate() {
        if (studio) await authenticateStudio(page);
        else {
          const token = page.getByLabel('访问令牌');
          // Classic's first unauthenticated history read can open this panel.
          if (!(await token.isVisible())) await page.locator('#api-settings > summary').click();
          await token.fill('studio-browser-test-only');
        }
      }

      const submissions: { key: string | undefined; body: unknown }[] = [];
      await page.route('**/api/runs/*/approval', async (route) => {
        const request = route.request();
        const body = request.postDataJSON();
        if (body.decision !== 'revise') {
          await route.continue();
          return;
        }
        submissions.push({ key: request.headers()['idempotency-key'], body });
        const response = await route.fetch();
        expect(response.status()).toBe(200);
        if (submissions.length === 1) await route.abort('timedout');
        else await route.fulfill({ response });
      });

      await page.goto('/' + surface);
      await expect(page).toHaveTitle(studio ? /Agent Execution Studio/ : /Public Opinion Research/);
      await authenticate();
      if (studio) {
        await page.getByRole('button', { name: '新建研究', exact: true }).click();
        await page.getByLabel('研究主题').fill('brand risk with evidence');
        await page.getByRole('button', { name: '创建持久 Run' }).click();
      } else {
        await page.locator('#topic').fill('brand risk with evidence');
        await page.getByLabel('开始研究前审批计划').check();
        await page.getByRole('button', { name: '开始舆情分析' }).click();
      }
      const version = page.locator(studio ? '.approval .mono' : '#approval-version').first();
      const renderedBrief = page.locator(studio ? '.approval .markdown' : '#approval-plan');
      const edit = page.getByRole('button', { name: 'AI 修改 Brief', exact: true });
      const opinion = page.getByLabel('AI 修改意见');
      const confirm = page.getByRole('button', { name: '确认原 AI 修改请求', exact: true });
      const approve = page.getByRole('button', {
        name: studio ? '批准执行' : '批准并继续',
        exact: true,
      });
      await expect(approve).toBeEnabled();
      const initialVersion = await version.innerText();
      const initialBrief = await renderedBrief.innerText();
      await edit.click();
      const feedback =
        'browser-fail-once-revision ' + randomUUID() + ': compare service response times.';
      await opinion.fill(feedback);
      await page.getByRole('button', { name: '提交 AI 修改', exact: true }).click();
      await expect(confirm).toBeEnabled();
      expect(submissions).toHaveLength(1);

      await page.reload();
      await authenticate();
      if (!studio) await page.getByRole('button', { name: '恢复后台进度' }).click();
      await expect(confirm).toBeEnabled();
      await expect(page.getByText(/已保留当前 Brief/)).toBeVisible();
      await expect(page.getByText(/AI 修改：0 \/ 3 次/)).toBeVisible();
      await expect(version).toHaveText(initialVersion);
      await expect(renderedBrief).toHaveText(initialBrief);
      await expect(approve).toBeDisabled();
      expect(submissions).toHaveLength(1);
      await confirm.click();
      await expect(confirm).toBeHidden();
      expect(submissions[0].key).toMatch(/^[a-f0-9-]{36}$/);
      expect(submissions[1]).toEqual(submissions[0]);

      await expect(approve).toBeEnabled();
      if (!(await opinion.isVisible())) await edit.click();
      await expect(opinion).toHaveValue(feedback);
      await page.screenshot({
        path:
          (process.env.BRIEF_SCREENSHOT_DIR ?? '../.tmp') +
          '/studio-brief-retry-' +
          surface +
          '.png',
      });
      await page.getByRole('button', { name: '重试 AI 修改', exact: true }).click();
      await expect(page.getByText(/AI 修改：1 \/ 3 次/)).toBeVisible();
      await expect(version).toContainText('v2');
      await expect(renderedBrief).toContainText(feedback);
      expect(submissions).toHaveLength(3);
      expect(submissions[2].body).toEqual(submissions[0].body);
      expect(submissions[2].key).toMatch(/^[a-f0-9-]{36}$/);
      expect(submissions[2].key).not.toBe(submissions[0].key);
      await expect(approve).toBeEnabled();
      await approve.click();
      if (studio) await expect(version).toContainText('已锁定');
      else await expect(page.locator('#status-text')).toHaveText('已完成');
      expect(errors).toEqual([]);
    },
  );
}
