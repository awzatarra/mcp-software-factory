# Configuration

## Required setup

Copy `.env.example` to `.env` in the repository root. Host and API share
`runtime_config.load_settings`: process environment wins over this file.
It never searches a parent directory for another project's secrets.
Secrets remain environment values and are excluded from the settings repr.

| Variable | Required | Purpose |
| --- | --- | --- |
| OPENAI_API_KEY | API/Host startup and paid workflows | Supply your own key locally; examples stay blank. No key needed for unit tests. |
| OPENAI_MODEL | Explicit choice recommended | Model available to your account. Existing fallback remains unchanged; no model is selected or purchased by readiness checks. |
| WORKSPACE_ROOT | No | Defaults to `./workspace`, relative to the repository. Explicit absolute deployment roots are supported; never point it at a personal home. |
| DATA_ROOT | No | Local data root, normally `./data`; stores with explicit database variables retain their existing precedence. |
| USE_LANGGRAPH | No | Set `true` for durable graph execution in the interactive Host. The API uses its durable graph service. |
| LANGGRAPH_CHECKPOINT_DB | No | Optional checkpoint database path. Leave blank for the existing local default. |
| WORKFLOW_EVENT_STORE_PATH | No | Durable event/store database, default `data/workflow-events.sqlite`. |
| API_CORS_ORIGINS | UI access | Comma-separated explicit origins; examples permit local Vite origins, not wildcard access. |

The project does not require a GitHub token to run locally. Do not add
`GITHUB_TOKEN` or `ENVIRONMENT` expecting behavior unless a specific integration
documents those variables. Deployment uses `APP_ENV` and target-specific inputs.

Run from the repository root. For Uvicorn, load environment before imports
so settings read at module initialization also see your configuration:

```powershell
.\.venv\Scripts\uvicorn.exe api.app:app --env-file .env --host 127.0.0.1 --port 8000
```

API startup currently requires a key, but startup/health do not make paid LLM
requests. Do not bypass this requirement with an undocumented shared key.
Mock-based unit tests and frontend tests run without provider credentials.

## Optional settings

The committed `.env.example` is the inventory of supported local defaults;
domain modules retain their existing typed validation and policy semantics.
This readiness work centralizes entry-point secrets/model/workspace settings,
not every policy setting in the application.

| Group | Meaning |
| --- | --- |
| MCP_*_TIMEOUT_* | Connection, transport and cleanup time limits; tool overrides server overrides global. |
| NOTIFICATION_* / APPLICATION_URL | Local notification worker and target restrictions; private destinations disabled by default. |
| ALERT_* | Periodic alert evaluation and startup backfill. |
| OBSERVABILITY_* | Local tracing, polling exclusions, error capture and reconciliation. |
| OTEL_* | Optional external export; blank endpoint/headers keep it unconfigured. Headers may contain secrets. |
| LLM_COSTS_* / LLM_BUDGET* / LLM_RETRY_* | Usage, pricing, reservations and budget policy; review enforcement before paid workflows. |
| KNOWLEDGE_* / *_KNOWLEDGE_* | Retrieval limits, chunking, provenance and failure behavior. |
| GIT_* | Command bounds, protected branches and governed promotion behavior. |
| CI_* | Timeouts, gates, SHA binding, repair bounds and analytics. |
| AGENT_EVALUATION_* / AGENT_RECOMMENDATION_* | Evaluation weights and recommendation sample policies. |
| POLICY_EXPERIMENT_* / PLANNER_* | Experiment thresholds and optional semantic judge; judge disabled by default. |
| LANGGRAPH_DEVELOPMENT / *_FORCE_* / LANGGRAPH_FAIL_AFTER_NODE | Development-only failure hooks; leave disabled for normal use. |
| DEFAULT_*_REQUIREMENT | Generated project dependency defaults, not dependencies of the host environment. |
| WORKFLOW_EVENT_* | Event persistence and retention. |
| MCP_FACTORY_DEBUG | Diagnostic output; leave false outside controlled debugging. |

## Containers and deployment

`.env.production.example` is a separate **dummy template**, not a live local
configuration. Replace `example.com` URLs with your chosen local endpoints
before using it. For local Compose, use backend `http://127.0.0.1:8000`,
frontend `http://127.0.0.1:5173` and matching CORS origins. Compose assigns
container paths and named volumes; do not paste host personal paths into it.

Deployment controller configuration and maintenance evidence remain outside
Git. See [Phase 11](phase-11-cd-demo.md) and
[MiniStack operations](phase-12-ministack-deployment.md). Those examples are
not production endpoints or permission to run a deployment.

## Security boundary

Host/API -> graph -> approval -> MCP client -> private stdio servers.
Filesystem operations resolve paths under WORKSPACE_ROOT and reject escapes.
Testing uses closed command sets and isolated project environments. Git uses
its own allowlist. These are safety controls, **not an OS sandbox**: installing
dependencies or executing generated tests can execute project code. Use a
trusted, isolated machine/container and never expose this unauthenticated local
demo API or a self-hosted runner to untrusted users.
