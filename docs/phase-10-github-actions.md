# Phase 10.1 — GitHub Actions Local Runner

## Architecture

GitHub Actions dispatches a job to a self-hosted Windows runner on the local
machine. The runner checks out the repository and reaches the existing Backend
through `http://127.0.0.1:8000`; the Backend remains loopback-only.

## Prerequisites

- A GitHub repository containing this workflow.
- Docker Compose or the existing local Backend runtime.
- A Windows account allowed to run the GitHub runner and reach loopback.
- The Backend already healthy before dispatching the workflow.

## Runner Setup

In GitHub, open Repository -> Settings -> Actions -> Runners -> New self-hosted
runner, select Windows, and follow the commands GitHub displays. Install it
outside this repository, for example at `C:\github-runner\`. Do not save the
registration URL or token in source control.

For this demo, start the registered runner interactively from its own directory:

```powershell
.\run.cmd
```

Do not install it as a Windows Service for Phase 10.1.

## Start Backend

Start the existing Backend before the runner job. For the container deployment:

```powershell
docker compose --env-file .env.production up -d
Invoke-RestMethod http://127.0.0.1:8000/health
```

The expected health payload is `{"status":"ok"}`. GitHub Actions does not start
or stop the stack in this phase.

## Run Workflow

Open GitHub -> Actions -> Software Factory Local -> Run workflow. The job uses
the repository's default self-hosted runner labels and serializes executions
with the `software-factory-local` concurrency group.

## Expected Result

`Checkout`, `Runner context`, and `Software Factory health` pass. The final step
prints `Software Factory backend reachable.`

Optional negative check: stop the Backend and dispatch manually. The health step
must fail clearly. Restart the Backend afterward; this destructive check is not
automated.

## Security

The workflow has only `contents: read`, prints no credentials, defines no
secrets, performs no GitHub writes, opens no port, and creates no tunnel. Runner
registration remains a manual GitHub procedure and its token stays outside the
repository.

## Known Limitations

The runner is local, interactive, Windows-only for this demo, and assumes the
Backend is already running. There is no remote CI, service installation,
automatic stack lifecycle, public webhook, cloud deployment, or high
availability.

## Phase 10.2 Handoff

Phase 10.2 extends the validated local runner with the read-only Backend lookup
documented below. It preserves current approvals, Git, CI, Promotion, MCP, and
loopback-only Backend semantics.

## Phase 10.2 — Backend Read Integration

The manual dispatch now accepts an optional string input named `thread_id`. If
it is empty, the job performs only the mandatory Backend health check. If it is
present, the runner issues a read-only `GET` request to
`http://127.0.0.1:8000/api/workflows/{thread_id}`.

Successful lookup logs only `thread_id`, `project_name`, `workflow_intent`,
`terminal_status`, `interrupted`, and `pending_operation`. A missing workflow
fails with `Software Factory workflow not found.` No request body, tool
arguments, files, prompts, responses, tokens, approvals, environment dump, or
credentials are logged. The integration performs no POST, approval, GitHub
write, Git, CI, Promotion, Repair, or LLM operation.
