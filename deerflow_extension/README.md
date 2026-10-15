# Public Opinion Research extension

Distribution: `opinion-deerflow-extension`. Python import: `opinion_deerflow_extension`.
The Phase 0 adapter contributes one offline diagnostic tool and one status action
using only `deerflow_extension_api`. Business behavior lives in `opinion_business`.
No research, sentiment classification or risk assessment is implemented here.

From the repository root, install and test this package independently:

```sh
uv run --no-project --python 3.12 --with ./backend/packages/extension-api --with ./opinion_business --with ./deerflow_extension python -m unittest discover -s deerflow_extension/tests -v
```

The explicit checkout-relative package arguments install the matching business
and official API packages together. Built wheels carry ordinary versioned
distribution dependencies, with no development-machine paths. The DeerFlow
runtime resolves the same local packages through `backend/pyproject.toml` and
its checked-in lockfile. Package-local `[tool.uv.sources]` is intentionally
absent: the official extension manager copies the source below `backend/`,
where repository-relative dependency paths would resolve incorrectly.

Install/enable through the official extension manager from `backend/`:

```sh
uv run deerflow extensions install ../deerflow_extension --yes
```

The operator-owned root `config.yaml` entry is:

```yaml
plugins:
  - use: opinion_deerflow_extension:install
    enabled: true
    config:
      enabled: true
```

Restart Gateway after changes. The outer switch controls Python loading, and
`config.enabled` controls the contributed capabilities (default `false`). Python
plugins do not belong in `extensions_config.json`.

The `deerflow.extensions` entry point is named `opinion`. It installs namespace
`public-opinion.phase0`, declares model tool `opinion_ping` in group `extensions`,
and declares backend action `status`. The host generates the collision-resistant
runtime tool name; discover it using the host tool assembly rather than naming a
second tool manually.

Tool input is `{"text": "hello"}`. It echoes a bounded diagnostic string and
reports the business package version; it makes no network request and needs no
API key. It does not run the old Agent runtime. Status is available to an
authenticated Gateway caller through
`POST /api/plugins/public-opinion.phase0/actions/status` with an empty JSON object.
It reports interface-only business domains without exposing deployment secrets.
