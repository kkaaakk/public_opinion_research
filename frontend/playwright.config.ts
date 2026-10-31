import { defineConfig } from '@playwright/test';
const python = process.platform === 'win32' ? '../.venv/Scripts/python.exe' : '../.venv/bin/python';
const port = Number(process.env.STUDIO_TEST_PORT ?? 8765);
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 90000,
  expect: { timeout: 45000 },
  outputDir: '../.tmp/studio-playwright',
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    browserName: 'chromium',
    channel: process.env.STUDIO_BROWSER_CHANNEL ?? 'chromium',
    viewport: { width: 1600, height: 1000 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: `"${python}" ../tests/studio_browser_server.py --port ${port}`,
    url: `http://127.0.0.1:${port}/studio`,
    timeout: 120000,
    reuseExistingServer: process.env.STUDIO_REUSE_SERVER === '1',
  },
});
