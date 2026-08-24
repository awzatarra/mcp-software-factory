# Phase 9 Summary

## 9.1 Deployment Model

Defined the public Frontend/Backend boundary, private MCP boundary, persistent
workspace and stores, environment contract, health expectations, and security
invariants without changing runtime behavior.

## 9.2 Containerization

Added reproducible Backend and Frontend images, a private Compose network,
loopback-only host ports, healthchecks, non-root Backend execution, and named
volumes for `/app/workspace` and `/app/data`. Existing stdio MCP Servers remain
private child processes inside Backend.

## 9.3 Validation

Validated a no-cache build, healthy startup, MCP discovery, a non-paid workflow
through API/LangGraph/Filesystem MCP, durable checkpoints and events, volume
persistence across restart and down/up, and container/image security controls.

## Architecture

Browser -> Frontend -> Backend API / LangGraph -> private stdio MCP Servers.
Frontend and Backend share a private Compose network; MCPs have no network
listener or published port.

## Persistent Data

`mcp-software-factory-workspace` stores generated projects and Git state.
`mcp-software-factory-data` stores LangGraph checkpoints, workflow events, CI,
Git audit, governance, evaluation, observability, Knowledge, and FinOps SQLite
data.

## Security Invariants

Backend is non-root, containers are not privileged, the Docker socket and host
filesystem are not mounted, MCPs remain private, and secrets are injected at
runtime rather than copied into images or the Frontend bundle.

## Known Limitations

The deployment is local Compose only. It has no cloud provider, TLS/domain,
autoscaling, Kubernetes, remote CD, Postgres migration, Redis, external secrets
manager, or external observability stack.

## Final Status

Phase 9 is closed. Its deployment model, local containerization, persistence,
recovery, health, private MCP connectivity, and minimum hardening have been
implemented and validated without adding runtime capabilities.
