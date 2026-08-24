# Phase 9.1 - Deployment Model

## Architecture

```text
Internet
   |
   v
Frontend (public)
   |
   v
Backend API / LangGraph (public API)
   |-- MCP Servers (private)
   |-- Persistent workspace
   |-- Git repositories
   `-- Persistent stores
```

Frontend and Backend are separate services. Backend, LangGraph, and
`WorkflowRunner` form the main execution service. MCP Servers run as private
processes or services reachable only by Backend; they are never Internet-facing.

## Components

- **Frontend:** the existing Vite/React application. It calls Backend API only
  and has no direct access to MCPs, workspace, Git, stores, or Backend secrets.
- **Backend:** FastAPI, LangGraph, workflow execution, approvals, Git, CI,
  evaluation, and governance. It is the only component authorized to
  orchestrate MCP Servers.
- **MCP Servers:** the existing private capabilities on an internal transport or
  network. Phase 9.1 adds no MCPs and changes no MCP contract.
- **Workspace:** persistent storage for generated projects, their Git
  repositories, and project-local environments such as `.venv` when applicable.
- **Stores:** the existing durable SQLite stores on persistent storage. Postgres
  is a possible future evolution, not a Phase 9.1 requirement.

## Public vs Private

| Component | Exposure |
| --- | --- |
| Frontend | Public HTTP service |
| Backend | Public governed API |
| MCP Servers | Private; Backend access only |
| Workspace and Git | Private; no direct HTTP or shell access |
| SQLite stores | Private persistent storage |

The public boundary remains the governed Backend API. Filesystem and Git
mutations remain available only through their existing validation, policy, and
approval flows.

## Persistent Data

The following must survive Backend restart or redeploy:

- `workspace/`, generated projects, Git repositories, and project environments;
- LangGraph workflow and checkpoint stores, including pending approvals;
- workflow events, CI runs, Git audit/state, and Promotion state;
- recommendation, evaluation, governance, and policy/runtime stores;
- durable Knowledge, observability, LLM usage, cost, pricing, and budget data.

SQLite database files must live outside an ephemeral process or container
filesystem. Workspace and store volumes have independent lifecycle from the
Backend process. Production backup and restore are required operational
considerations, but backup automation and store migration are outside 9.1.

## Dev vs Prod

| Concern | DEV | PROD |
| --- | --- | --- |
| Frontend | Local Vite server | Separate public service |
| Backend | Local FastAPI process | Separate API/execution service |
| MCP | Local private processes | Private internal processes/services |
| Workspace | Local `workspace/` | Persistent mounted storage |
| Stores | Local SQLite files | SQLite on persistent mounted storage |
| Secrets | Local environment | Environment injection or secret manager |

Staging may be introduced later with the same production invariants; it is not
defined in this phase.

## Configuration

`.env.production.example` is a non-secret production template. Production
values are injected at deployment time and real `.env` files are not committed.
`OPENAI_API_KEY`, OTLP headers, tokens, passwords, and credentials come from
environment variables or a secret manager. The Frontend receives only public
configuration such as `VITE_API_BASE_URL`.

`FRONTEND_BASE_URL`, `BACKEND_BASE_URL`, `CORS_ALLOWED_ORIGINS`,
`WORKSPACE_ROOT`, and `DATA_ROOT` describe the deployment contract. The current
runtime consumes `APPLICATION_URL`, `VITE_API_BASE_URL`, `API_CORS_ORIGINS`,
`LANGGRAPH_CHECKPOINT_DB`, and `WORKFLOW_EVENT_STORE_PATH`; the example maps
both sets explicitly. Wiring root-level mounts directly through new runtime
variables is deferred to 9.2.

## Healthchecks

- **Frontend:** its HTTP service returns a successful response.
- **Backend:** the expected deployment probe is `GET /health`; Phase 9.2 must
  bind it to Backend readiness if the selected API entrypoint does not expose
  that route yet.
- **Critical MCPs:** their existing process/readiness mechanism where available.

This phase defines the expected checks only. It adds no endpoint and changes no
runtime behavior.

## Security Invariants

- MCP Servers remain private and are never exposed directly to the Internet.
- No public shell or direct public filesystem interface exists.
- Git mutations remain behind governed APIs and mandatory approvals.
- CI and Promotion semantics remain unchanged.
- Secrets never enter source control or Frontend bundles.
- Workspace path validation continues to reject absolute paths and traversal.
- Frontend never receives Backend credentials.

## Known Limitations

Phase 9.1 does not provide container images, orchestration, deployment
automation, load balancing, autoscaling, backup automation, Postgres, Redis, or
multi-region operation. SQLite remains suitable only when its files are placed
on storage with the required durability and filesystem semantics.

## Phase 9.2 Handoff

Phase 9.2 may select a concrete deployment target and wire persistent mounts,
service startup, private MCP connectivity, health probes, secret injection, and
backup procedures. It must preserve the public/private boundary and all current
approval, Git, CI, Promotion, and MCP contracts.
