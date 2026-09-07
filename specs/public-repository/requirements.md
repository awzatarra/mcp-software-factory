# Public Repository Requirements

## Goal and release gate

Publish a reviewable portfolio without secrets, private data, personal machine
paths, generated projects or execution artifacts. PUBLIC-57 (visibility) SHALL
remain blocked until PUBLIC-01 through PUBLIC-56 are complete or explicitly
reviewed. Tests passing is not authorization to publish. See [tasks](tasks.md).

## Security

- Tracked content SHALL contain no real API keys, tokens, passwords, private
  keys/certificates, webhook secrets, credential-bearing URLs or customer data.
- Examples SHALL contain blank or clearly synthetic credentials only.
- Private personal paths and company information SHALL NOT be published.
  Public author attribution is intentional; synthetic redaction fixtures are
  reviewed test data, not real users or credentials.
- All reachable local Git history SHALL be audited; remote/deleted refs and
  binary assets require explicit additional review.
- A committed credential SHALL be revoked at its provider, replaced locally,
  removed from history where necessary and rescanned. Never test a suspected
  live credential against a provider merely to determine validity.

## Configuration and MCP

- Secrets SHALL come from environment variables, never committed defaults.
- `.env.example` and per-deployment example files SHALL remain available.
- Required variables and precedence SHALL be documented.
- Entry-point settings SHALL be shared; existing domain validation is retained.
- Filesystem operations SHALL remain inside configured WORKSPACE_ROOT.
- Testing/shell operations SHALL use closed commands, correct project cwd and
  existing approvals. Do not introduce arbitrary shell commands for readiness.
- MCP SHALL remain private stdio. Workspace checks are not an OS sandbox for
  generated code or package installation; document this boundary honestly.

## Repository hygiene

Real `.env*`, virtual environments, caches, IDE files, logs, SQLite/DB files
and sidecars, generated workspace projects, snapshots, artifacts and videos
SHALL be ignored and absent from tracking. Explicit `.env.example` and
`.env.production.example` exceptions are permitted, including frontend examples.
Demo project names do not exempt generated projects from this rule.

## GitHub Actions

Workflows SHALL use minimum permissions, pinned third-party Actions, and
`persist-credentials: false` for checkout. Deployment workflows SHALL remain
manual-only and separated by target. Review runner registration, environment
access and repository settings before activating deployment. Do not silently modify an
already governed deployment or register a runner as part of this task.

Existing offline Windows runner is outside the public demo scope. It is not
a functional prerequisite for publication. Netlify, Railway and Vercel are
also outside this recruiter-visibility scope. External settings are not a
publication gate unless exposed secrets or sensitive data are identified.
No integration change, runner activation or hosted demo is requested.

## Reproducibility and documentation

A fresh clone SHALL install from public dependencies, use documented example
configuration, start local services and run tests without author-specific data.
Paid workflows require the developer's own account; mock tests SHALL require
no credentials. README SHALL explain requirements, architecture, installation,
configuration, execution, tests, MCP, LangGraph, approvals and security.

Architecture details are in [design](design.md); required settings are in
[configuration](../../docs/configuration.md). Do not publish false claims of
cloud production readiness, active-secret revocation, or passing clean-clone
validation without evidence.

## Definition of done

- [x] PUBLIC-04: Reviewed/Accepted; no evidence of real/active credentials in files or reviewed history. No speculative rotation required.
- [x] PUBLIC-19: owner reviewed GIF, Loom and release video for sensitive data.
- [x] PUBLIC-20: owner approved existing public author identity.
- [x] PUBLIC-50: independent scanner results reviewed, including synthetic history finding.
- [x] PUBLIC-53: final refs review confirmed by owner; history preservation retained.
- [x] Safe example configuration and hardened ignore rules.
- [x] MCP workspace isolation and approval boundaries retained.
- [x] GitHub Actions source reviewed for least privilege and pinned checkout.
- [x] Main tests and clean-clone validation completed and recorded.
- [x] README, architecture and configuration documented.
- [x] PUBLIC-28: Reviewed/Out of Scope; external Apps and offline runner excluded by owner.
- [x] PUBLIC-56: Done for recruiter visibility, using the recorded evidence and accepted scope.
- [x] PUBLIC-57: Done/Public; authorized publication completed and anonymous access verified. See tasks for evidence.
