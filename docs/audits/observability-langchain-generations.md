# LangChain request Generations (PR A)

2026-10-28, Asia/Shanghai. Base: main `9b479fa4fd4ee412305d81ddba3bf2ecb62b364c`.
Independent branch `codex/observability-langchain-generations`; no merge or production deployment.
The original checkout and its uncommitted files were preserved. Remote main was
checked with `ls-remote`; noninteractive fetch confirmed the same commit.

## Actual gap and PoC

The existing single Provider exported DBOS, OI Agents and business scopes. Core
models had stage totals but no request Generations. Checked installed sources:
DBOS 3.3.0, Agents SDK 0.24.0, Langfuse 4.18.0, OI Agents 2.6.0, OTel 1.45.1,
Core 1.6.11. The only new locked package is OI LangChain **0.1.80**, using existing
OI instrumentation 0.1.72 and semconv 0.1.42. No top-level `langchain` or LangGraph
was installed. Root lock entries: 296 → 297; all other fixed versions retained.

An in-memory PoC over a real Core model, Runnable sequence and tool produced
**1 LLM, 1 TOOL, 2 CHAIN**, plus the existing outer tool span. Parent closure and
the final content filter passed. OI's global hook therefore does not meet the
no-duplicate-tool requirement. Its public instrument API has no model-only option.

The small adapter obtains OI's configured handler via public
`CallbackManager.configure().handlers`, then calls public `uninstrument()` to
restore Core before serving work. Core's public `register_configure_hook` installs
one model-only callback, deduplicated by its handler class. It forwards only
`on_chat_model_start`, `on_llm_start`, `on_llm_end`, `on_llm_error`; OI still creates,
times and ends the Generation. The adapter does not import or mutate OI's private
`_tracer`, wrap model invocation, maintain a span store or create another Provider.
Dropping unknown LC parent-run IDs lets OI use the current native OTel parent,
without exporting then deleting ancestors or leaving orphaned children.

This deliberate startup composition remains a compatibility surface: its fixed
versions and public callback discovery are regression tested. Initialization
fails closed for telemetry if an OI LC hook is already owned elsewhere. Other
code must not install competing instrumentors after initialization.

## Usage, privacy and ownership

The exact LC scope joins the existing egress allowlist. The same TraceConfig hides
content; SafeExporter still strips attributes, events, links, resource/scope data,
status text and tracestate. Only the bounded `llm.provider` label is newly admitted.
Generation names are constant. No prompt, image bytes, tool args, response, evidence
or private memory is intentionally exported. Exporter errors do not retry models.

OI reads successful canonical usage. On model error its fixed callback ignores
Core's partial LLMResult, so the adapter copies only already-reported numeric
usage onto that same span before the official error callback ends it. It creates
no second Generation and never touches budget admission, capture or reducers.
SDK Models continue through OI Agents. Cache/reasoning details partition native
usage and must not be added to the already-inclusive input/output totals.

All Core models using normal callbacks are covered, including intake/brief,
planning, review/gap, report sections, Graph extraction, working context, rolling
compaction, RAG rewrite, memory and synchronous Vision. Custom external models
that bypass Core callbacks cannot be observed by this integration. Provider HTTP
retries below Core are not separate Generations. Project model caching is not
configured; this integration does not claim to identify externally installed Core
cache hits as network requests.

## Verification and remaining boundaries

Initial regression before implementation: **7 failed**. Final added matrix:
**13 passed**, including sync/async, official ChatOpenAI over mock HTTP with image
input, structured retry, mixed SDK+LC, real tool/chain suppression, parallel thread
identity, canonical cache/reasoning details, partial failure and synchronous cancel.
Structured retry: **2 requests / 2 Generations / 21 tokens**, business delta
**2 calls / 21 tokens**. Mixed SDK tool: **2 SDK + 1 LC Generations / 1 Tool**.

Final full offline: **927 passed / 107 deselected**, 214.95s, dotenv/tracing defaults
disabled and pytest-socket limited to loopback/unix. Earlier full run with three
incorrect new fixture assertions was **918 passed / 3 failed**; preserved, not
reported as success. The model declares provider `scripted`; timestamps from OI's
datetime lifecycle are compared at microsecond precision. A synthetic async
provider raising CancelledError also exposes a Core 1.6.11 error-path defect
(AttributeError while treating BaseException as a result); this PR does not patch
the vendor or change business exception semantics. Core-delivered cancel callbacks
are tested. A separate outer-awaiter cancellation regression first failed with no
ended Generation. The adapter now uses the real inline callback's Task completion
to close only its already-started observation, marking cancellation/unknown effect
and `model_callback_missing`; it does not run or wait for the model. OI removes its
own request state through its official error callback. No missing Generation or
usage is fabricated and no execution grant is issued.

Admission denial also first reproduced an unissued Generation because Core ran
the new inline observer before its non-inline budget callback. The existing
`ModelAttemptCounter` now sets `run_inline=True`, so its unchanged admission
predicate executes first. No budget arithmetic, capture, counter increment or
reducer changed. The regression asserts **0 requests / 0 calls / 0 tokens / 0
Generations** for a real exhausted policy; the provider script remains unconsumed.
Budget, token and tracing targeted matrix: **100 passed**. A prior full run's two
cache-directory permission failures were sandbox temp-file rename restrictions;
the authoritative local run above passed without changing those tests.

Studio typecheck/lint, **42 unit tests**, production build and **7 Classic protocol
tests** passed. Ruff E/F/I, changed-file format, compile and **32 production imports**
passed. `uv lock --check` passed. `uv pip check` remains **exit 1** solely for the
pre-existing `markitdown-mcp` → `mcp~=1.8.0` versus locked `mcp==1.23.0` conflict.
Full native DBOS/PostgreSQL/Neo4j: **101 passed / 25 deselected**, 1542.37s.
Studio real Chrome E2E: **6 passed**. The first native run had 99 pass / 2 fail:
one old 6-LLM assertion now distinguishes 6 SDK + 15 Core requests; one test
submitted a version invalidated by late terminal receipt advancement. Its initial
grant now uses the repository's bounded same-generation `retry_observed_failure`
helper and retains every negative old-key/cancel/unknown-effect assertion. Targeted
retest: **3 passed**; the full matrix above then passed. Intermediate context/
container-interrupted matrices are preserved and are not counted as success.

The final real Langfuse Gap Run is `d52075990da24412aba441cc0604e2b6`, Trace
`eca6f20515d57c3b96de5769a8d5e4d9`: **892 observations**, **30 Generations**
(10 SDK + 20 Core), **340 tokens**, equal to the 417-token ledger less its
77-token fixture baseline. All 20 Core model labels are `fixture-service` and the
10 SDK labels `pr32-offline-model`; response-only model labels were initially
missing in OI and now populate the same existing span from reported metadata.
The initial UI trace had 894 observations due to different helper polling counts;
it was not used as a performance comparison. Complete-field API inspection,
privacy and UI evidence are also recorded in the final hardening report.

Official references checked alongside installed source:
[OI Core/Classic support](https://arize-ai.github.io/openinference/python/instrumentation/openinference-instrumentation-langchain/),
[OI instrumentor](https://github.com/Arize-ai/openinference/blob/main/python/instrumentation/openinference-instrumentation-langchain/src/openinference/instrumentation/langchain/__init__.py),
[Core callback hook](https://reference.langchain.com/python/langchain_core/tracers/context/register_configure_hook),
[OpenAI tracing](https://developers.openai.com/api/docs/guides/agents/integrations-observability).

Tests use ScriptedModel/Fake models/MockTransport. Paid provider requests: **0**.
No DBOS commands, workflow inputs, application/runtime versions, Finalizer,
authorization, operations, role prompts or Research Graph storage were changed.

## Independent review follow-up

The authorized read-only reviewer found one P1 in inherited callback ordering:
inline dispatch alone did not place admission before a ModelOnlyHandler inherited
from a Tool/Runnable. Synchronous rejection could leave OI request state; async
rejection could export an unissued Generation. Merely prioritizing the newly
created Counter was insufficient when an older inherited Counter had a stricter
policy. Eight sync/async parent/config regressions first failed (6 + 2).

`model_accounting_config` now stably partitions **all** ModelAttemptCounter objects
before observers in the merged copy's public handler and inheritable-handler
lists. Existing Counters keep their relative order, so an older rejection occurs
before a new counter increments. The caller manager/list, membership, local
callbacks, metadata and parent identity remain unchanged. Rejection tests assert
0 provider requests, 0 captured calls/tokens, no Generation and empty OI run state.
Targeted budget/usage/SDK/trace matrix: **106 passed**. The reviewer confirmed the
P1 closed with no further blocker. Full final offline/remote CI results are
recorded in the PR freeze comment; previous 927/101 results above are kept as
historical checkpoints, not substituted for the updated tree.

The Task cleanup guarantee applies to the project's bounded invoke/ainvoke paths.
Arbitrary cross-Task `astream` iteration is not a claimed supported ownership
pattern. No new Agent loop, execution grant or trace replay was added.
