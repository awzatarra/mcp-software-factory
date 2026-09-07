# Security Policy

## Supported version

This repository is a portfolio and educational project. Security fixes are
applied to the latest revision on the default branch.

## Reporting a vulnerability

Do not disclose vulnerabilities, credentials, tokens, prompts containing
private data, or exploit details in a public issue.

Use GitHub's private vulnerability reporting feature when it is available for
this repository. If it is not available, contact the maintainer through the
GitHub profile linked in the README and share only enough information to
establish a private reporting channel.

Reports should include the affected component, reproduction steps, impact, and
suggested mitigation when known. Never include real API keys or third-party
credentials.

## Secrets

The committed `.env.example` files contain placeholders only. Local `.env`,
`.env.production`, SQLite stores, generated workspaces, logs, and build output
are excluded from version control. Anyone deploying this project is
responsible for supplying and protecting their own credentials.

## Public-readiness review

Run `python scripts/audit_public_repository.py --history --include-untracked`
for a read-only redacted pattern audit. Findings contain locations, never
matched credential values. This limited audit cannot prove revocation, inspect
binary content or certify remote/deleted history. Review synthetic fixtures
explicitly; do not suppress entire test directories.

If a credential was committed, revoke it first, replace it locally, agree on
any necessary history cleanup, and scan again. Never publish a suspected
credential or probe its validity using a paid/provider request.

See the [readiness task record](specs/public-repository/tasks.md) for the
owner-approved recruiter-visibility scope and reviewed evidence. Demo media
was reviewed by the owner. External Apps and the offline Windows runner are
outside the public demo scope; no deployment access is promised. The local API
and execution tools are intended for a trusted environment, not a public
multi-tenant service.
