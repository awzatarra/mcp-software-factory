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
