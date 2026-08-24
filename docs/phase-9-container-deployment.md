# Phase 9.2 - Container Deployment

## Services

- `frontend`: multi-stage Vite/React build served by Nginx.
- `backend`: FastAPI, LangGraph, WorkflowRunner, Git, CI, and governance.
- MCP Servers: the existing planning, filesystem, testing, knowledge, and Git
  FastMCP servers run as private `stdio` child processes inside `backend`.
  Their current transport has no network listener, so Compose does not expose or
  emulate MCP ports.

## Network

Both services join `mcp-software-factory-private`. Local host access is limited
to Frontend on `127.0.0.1:5173` and Backend API on `127.0.0.1:8000`. MCPs share
the Backend container namespace and are reachable only by its MCP clients. TLS
and an external reverse proxy are outside this phase.

## Volumes

- `mcp-software-factory-workspace` mounts at `/app/workspace`.
- `mcp-software-factory-data` mounts at `/app/data`.

They are named volumes and survive `docker compose down` and Backend replacement.
`docker compose down -v` intentionally deletes them and must not be used when
state must be preserved.

## Environment

Create a local, ignored production file and set a real secret through the local
environment or secret-management mechanism:

```powershell
Copy-Item .env.production.example .env.production
# Set OPENAI_API_KEY in .env.production without committing the file.
# For this local Compose deployment, also set:
# FRONTEND_BASE_URL=http://127.0.0.1:5173
# BACKEND_BASE_URL=http://127.0.0.1:8000
# APPLICATION_URL=http://127.0.0.1:5173
# VITE_API_BASE_URL=http://127.0.0.1:8000
# CORS_ALLOWED_ORIGINS=http://127.0.0.1:5173
# API_CORS_ORIGINS=http://127.0.0.1:5173
```

Compose maps `WORKSPACE_ROOT`, checkpoint storage, workflow events, CORS, and
the Frontend build-time `VITE_API_BASE_URL` to container-safe paths and URLs.
Run Compose with `--env-file .env.production` so build arguments use the same
public URLs. No secret is copied into either image.

## Build

```powershell
docker compose --env-file .env.production config
docker compose --env-file .env.production build
```

## Start

```powershell
docker compose --env-file .env.production up -d
docker compose ps
```

Open `http://127.0.0.1:5173`. The browser uses the Backend URL embedded during
the Frontend build, which defaults to `http://127.0.0.1:8000` for local use.

## Health Validation

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-WebRequest http://127.0.0.1:5173/health -UseBasicParsing
docker compose ps
```

Backend startup connects all critical MCP child processes before Uvicorn becomes
ready. A startup failure is visible in `docker compose logs backend`. A
non-destructive MCP check can use existing workflow/resource inspection paths;
no paid LLM, Git mutation, or CI execution is required.

## Persistence Validation

```powershell
docker compose exec backend python -c "from pathlib import Path; Path('/app/workspace/.phase-9-persistence').write_text('workspace')"
docker compose exec backend python -c "from pathlib import Path; Path('/app/data/.phase-9-persistence').write_text('data')"
docker compose restart backend
docker compose exec backend python -c "from pathlib import Path; assert Path('/app/workspace/.phase-9-persistence').read_text() == 'workspace'; assert Path('/app/data/.phase-9-persistence').read_text() == 'data'"
docker compose down
docker compose --env-file .env.production up -d
docker compose exec backend python -c "from pathlib import Path; assert Path('/app/workspace/.phase-9-persistence').exists(); assert Path('/app/data/.phase-9-persistence').exists()"
```

Remove the two marker files after validation. Named volumes remain intact.

## Known Limitations

This local deployment has no HTTPS, remote deployment, external load balancer,
autoscaling, Postgres, Redis, external observability stack, or backup automation.
Frontend configuration is embedded at build time. Project-generated dependency
installation still requires outbound network access and its existing approval.
The Backend image includes Python and Git for the current primary workflow;
Node or .NET CI workloads require a derived image with those existing toolchains.

## Phase 9.3 Handoff

Phase 9.3 may add a remote deployment target, managed secrets, concrete backup
procedures, and platform health/readiness integration without changing MCP,
Planning, approval, Git, CI, or Promotion semantics.
