# Phase 0: official frontend and public Skills validation

Validated on 2026-10-15 (Asia/Shanghai), native Windows PowerShell. Upstream source is pinned to `8d994cc1e7ee63c61283a71a169248beb4ed1c52`.

## Frontend environment

| Item | Actual value |
| --- | --- |
| Node.js | `v24.15.0` (official guide requires Node.js 22+) |
| pnpm | `10.26.2`, identical to `frontend/package.json -> packageManager` |
| Next.js | `16.3.8`, resolved by the supplied lockfile |
| Development bundler | Webpack, selected by official `frontend/scripts/dev.mjs` |
| URL | `http://127.0.0.1:3000/` |
| Browser path | Browser plugin not available; connected Playwright browser used |

No frontend source, package manifest, or lockfile was changed. The dependency install used the committed lockfile:

```powershell
Set-Location frontend
node --version
pnpm.cmd --version
pnpm.cmd install --frozen-lockfile
$env:NEXT_TELEMETRY_DISABLED = '1'
$env:DEER_FLOW_AUTH_DISABLED = '1' # Local offline development only, as in official E2E config
pnpm.cmd dev -- --port 3000
```

The install succeeded with 1090 packages in approximately 2 minutes 8 seconds. `frontend/pnpm-lock.yaml` SHA-256 after install was `F545EC2D040A4AFF87FB34E8602323302B00EE842E13DD3B14AC79EC25B02662`.

The first sandboxed launch failed with `Error: spawn EPERM` while Next.js forked its server worker. Re-running the same official command with host process permission succeeded; the server reported `Ready in 3.2s`, listening only on `127.0.0.1:3000`. This was a local execution-permission issue, not a source or dependency workaround. No paid model credential was used. The official `frontend/.env.example` was subsequently copied to ignored `frontend/.env`, without secrets or edited values. The server was restarted with the official development-only `DEER_FLOW_AUTH_DISABLED=1` flag, matching the offline Gateway and the official E2E webServer configuration; readiness took 7.3 seconds on this launch. This flag is not a production authentication configuration.

## Rendered smoke test

The flow under test was: homepage loads → official Docs navigation is clicked → DeerFlow Documentation renders.

| Check | Result |
| --- | --- |
| HTTP request to `/` | `200`, using `Invoke-WebRequest` |
| Page identity | URL `/`; title and `h1` both `DeerFlow` |
| Meaningful content | Official hero, Get Started link, case studies, Skills and runtime sections rendered |
| Desktop viewport | `1920 × 889`, no horizontal overflow |
| Mobile viewport | `390 × 844`, document width `375`, no horizontal overflow |
| Framework overlay | No visible runtime error overlay in desktop screenshot |
| Console | Landing and final workspace: 0 errors and 0 warnings; later all-page collection found a docs React error and CSS preload warnings, detailed below |
| Interaction | Header Docs link reached `/en/docs`; title and `h1` became `DeerFlow Documentation` |
| Screenshot evidence | Desktop homepage, mobile homepage, mobile docs, and real workspace captured through Playwright; copies are session artifacts outside tracked source |
| Real Gateway-backed workspace | `/workspace/chats/new`, title `DeerFlow`, meaningful chat UI at `1440 × 1000` with no horizontal overflow |
| Real proxied APIs | `/api/plugins`, `/api/features`, `/api/models`, `/api/skills`, `/api/threads/search`, `/api/langgraph/threads/search`, and thread-activity requests returned `200` |

First-time compilation of the docs route took appreciably longer than the already compiled homepage. The navigation was confirmed after compilation completed. The Next.js dev portal exists normally even without an error; its mere DOM presence was not treated as an overlay failure.

The Next.js config already proxies `/api/*` to the Gateway at `127.0.0.1:8001`, and maps `/api/langgraph/*` to its native `/api/*`. Leave public backend URL variables unset for this default. The real workspace check used these existing rewrites without API mocking. No model was called and a full Agent conversation remains unverified.

Later collection of all navigation logs exposed an upstream docs-page client error: `Element type is invalid: expected a string ... but got: undefined`, plus unused CSS preload warnings. The docs route returned `200` and its content rendered, but this does not establish error-free docs interaction. No frontend/core source was changed to suppress the error. Priority P2: reproduce and investigate the docs client navigation separately. The landing and final chat workspace checks were healthy.

Nginx is unavailable in the current host environment, so the unified `localhost:2026` ingress and complete `make dev` stack were not claimed to pass. The official frontend's existing rewrites allowed real local Gateway validation at `127.0.0.1:3000`.

## Additional official checks

`pnpm.cmd check` passed with exit code 0 (ESLint plus TypeScript). The five unchanged official tests in `tests/e2e/landing.spec.ts` passed in 7.6 seconds after development compilation was warm. They cover the hero/header, widths 320/375/390, and Get Started navigation to `/workspace/chats/new`. Those E2E tests use the official API fixtures; they are separate from the real Gateway-backed browser check above.

```powershell
pnpm.cmd check
$env:PLAYWRIGHT_BASE_URL = 'http://127.0.0.1:3000'
$env:PLAYWRIGHT_SKIP_WEB_SERVER = '1'
pnpm.cmd exec playwright test tests/e2e/landing.spec.ts --reporter=line --workers=1
```

The first E2E attempt hit the same sandbox `spawn EPERM` restriction. With host process permission, the five tests reached browser launch but failed because Playwright's required Chromium `v1217` had not been installed. Browser installation was started in ignored `frontend/node_modules/.cache/ms-playwright`, without changing the lockfile or global browser configuration, then cancelled because the download was slow and an existing browser was available:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location) 'node_modules/.cache/ms-playwright'
pnpm.cmd exec playwright install chromium
```

Actual passing tests used installed Chrome `155.0.8059.41`. A temporary ignored config at `.playwright-mcp/phase0-playwright.config.ts` imported the official config and changed only test/output paths and `use.launchOptions.executablePath`. It did not modify application source, tests, or lockfiles. Recreate that temporary file at the repository root if repeating this exact Windows run:

```typescript
import original from '../frontend/playwright.config.ts';
export default {
  ...original,
  testDir: '../frontend/tests/e2e',
  outputDir: './phase0-test-results',
  projects: original.projects.map(project => ({
    ...project,
    use: {
      ...project.use,
      launchOptions: {executablePath: process.env.PHASE0_CHROME_EXECUTABLE},
    },
  })),
};
```

```powershell
# From frontend/, with the official dev server already running
$env:PLAYWRIGHT_BASE_URL = 'http://127.0.0.1:3000'
$env:PLAYWRIGHT_SKIP_WEB_SERVER = '1'
$env:PHASE0_CHROME_EXECUTABLE = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
pnpm.cmd exec playwright test --config ../.playwright-mcp/phase0-playwright.config.ts landing.spec.ts --reporter=line --workers=1
```

Earlier full runs had two explained failures: without the frontend development auth flag, Get Started reached `/setup` rather than the expected workspace; immediately after restart, cold route compilation exceeded the official 30-second test timeout. After applying the documented process flag and warming compilation, all five passed with the original test timeout. No real account was created and no mocked test was reported as a real backend execution.

## Public Skills

The three new public Skills use the actual official parser contract: a directory named for the Skill, a UTF-8 `SKILL.md`, YAML frontmatter containing `name` and `description`, and optional `license`. Their names do not collide with upstream Skills:

- `opinion-research`: scope and organize actual public opinion evidence.
- `risk-assessment`: qualitative evidence-based risks and uncertainty.
- `opinion-report`: draft a report from existing evidence.

They are guidance only. They explicitly state that Phase 0 has not migrated automated collection, retrieval, sentiment analysis, risk scoring, the legacy graph, monitoring, or report pipelines. No invented tool dependency, lifecycle hook, or full old workflow was added.

Actual official discovery succeeded, yielding all three entries with category `public` and `enabled=True`:

```powershell
Set-Location backend
& '.venv/Scripts/python.exe' -c 'from deerflow.skills.storage.local_skill_storage import LocalSkillStorage; names={"opinion-research", "risk-assessment", "opinion-report"}; skills=LocalSkillStorage(host_path="../skills").load_skills(); found=[(s.name, s.category.value, s.enabled) for s in skills if s.name in names]; print(found); assert {s[0] for s in found} == names'
```

The real browser then fetched the Gateway through `GET http://127.0.0.1:3000/api/skills` and confirmed status `200` and all three Skill entries with `category: public`, `enabled: true`, `editable: false`.

Public Skills are discovered from the committed `skills/public/` tree. Their enabled state belongs to official Skills configuration; Python extension registration belongs separately to `config.yaml -> plugins:`.
