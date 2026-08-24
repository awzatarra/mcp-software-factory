# Phase 9.3 — Production Validation

## Stack Validation

`docker compose config` and a no-cache build completed successfully. Frontend
and Backend started healthy on `127.0.0.1:5173` and `127.0.0.1:8000`.
Backend connected the five existing private stdio MCP Servers: Software Factory,
Filesystem, Testing, Knowledge, and Git.

## E2E Validation

A controlled review workflow entered through `POST /api/workflows`, reached
LangGraph, selected `inspect_workspace`, and completed
`filesystem__list_files` against the mounted workspace. The existing development
failure hook stopped execution immediately afterward, before Planning,
Implementation, Git, CI, or a provider request. Durable evidence:

- thread: `6e70f173-3f02-49a8-8b5c-c1fdb70ddd36`;
- checkpoint: `1f19fe7b-1b50-63e6-8003-dd1390b41408`;
- 15 durable events, including `workspace_inspection_completed`;
- LLM calls: 0.

## Persistence

The named `workspace` and `data` volumes retained marker data after both
`docker compose restart backend` and `docker compose down` / `up -d`. The same
checkpoint and all 15 workflow events remained accessible through the API.

## Restart Recovery

Backend returned healthy after restart, reconnected all five MCP Servers, loaded
the SQLite checkpointer from `/app/data`, and recovered the workflow registry,
snapshot, events, workspace file, and store marker.

## Security Checks

- Backend runs as non-root user `app`.
- Neither container is privileged and no Docker socket is mounted.
- Only `/app/workspace` and `/app/data` are persistent Backend mounts.
- MCP Servers publish no host ports; only Frontend and Backend use loopback ports.
- Image ENV, Backend build context, and Frontend bundle contain no validation key
  or `OPENAI_API_KEY`.
- `.env.production` is ignored and was removed after validation.

## Known Limitations

Deployment remains local Compose without a cloud provider, TLS/domain,
autoscaling, Kubernetes, remote CD, Postgres, Redis, or an external secrets
manager. SQLite remains the initial durable store. Observability retains its
current implementation.

## Result

PASS. Container build, health, MCP connectivity, E2E workspace access,
persistence, restart recovery, and minimum security invariants were validated.

