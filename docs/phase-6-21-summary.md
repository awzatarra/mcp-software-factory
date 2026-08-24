# Phase 6.21 Summary

Phase 6.21 was closed on August 14, 2026 as the local CI/CD governance layer for `mcp-software-factory`.

## Subphases

- `6.21.1` Pipeline execution: local CI pipeline preparation, execution, persistence, and restart reconciliation.
- `6.21.2` Gates: deterministic build, test, lint, and package gate evaluation with explicit decisions.
- `6.21.3` Git to CI integration: workflow commits become the source revision for CI and promotion evidence.
- `6.21.4` CI repair: rejected CI runs can trigger bounded repair attempts that must advance the commit SHA.
- `6.21.5` Observability and audit: CI metrics, audit trail, and promotion evidence became queryable and durable.

## Final Architecture

- Git workflow creates implementation and repair commits.
- CI prepares and runs a local pipeline against a precise source revision.
- Gate evaluation produces one durable decision:
  - `accepted`
  - `accepted_with_warnings`
  - `rejected`
- Promotion eligibility is derived from exact commit-bound CI evidence plus current promotion policy.
- Repair is only entered for repairable rejected CI outcomes.
- Analytics and audit consume persisted CI runs without re-executing CI.

## Principal State

Main workflow state fields used by the phase:

- `git_developer_commit_sha`
- `git_repair_commit_sha`
- `git_head_commit`
- `ci_state`
- `ci_run_id`
- `ci_status`
- `ci_decision`
- `ci_validated_commit`
- `ci_promotion_eligible`
- `ci_promotion_eligibility`
- `ci_repair_state`
- `ci_repair_attempts`
- `ci_repair_source_run_id`
- `ci_repair_source_commit`
- `ci_repair_target_commit`
- `ci_repair_lineage`

## Environment Variables

Core Phase 6.21 configuration:

- `CI_PIPELINE_VERSION`
- `CI_PIPELINE_TIMEOUT_SECONDS`
- `CI_STEP_TIMEOUT_SECONDS`
- `CI_STEP_OUTPUT_MAX_CHARS`
- `CI_GATE_POLICY_VERSION`
- `CI_BUILD_GATE_BLOCKING`
- `CI_TEST_GATE_BLOCKING`
- `CI_LINT_GATE_BLOCKING`
- `CI_PACKAGE_GATE_BLOCKING`
- `CI_GIT_INTEGRATION_VERSION`
- `CI_PROMOTION_REQUIRED`
- `CI_PROMOTION_ALLOW_WARNINGS`
- `CI_REPAIR_VERSION`
- `CI_REPAIR_ENABLED`
- `CI_REPAIR_MAX_ATTEMPTS`
- `CI_OBSERVABILITY_VERSION`
- `CI_ANALYTICS_DEFAULT_LIMIT`
- `CI_ANALYTICS_MAX_LIMIT`

## Endpoints

CI workflow endpoints:

- `GET /api/workflows/{thread_id}/ci`
- `POST /api/workflows/{thread_id}/ci/prepare`
- `POST /api/workflows/{thread_id}/ci/run`
- `GET /api/workflows/{thread_id}/ci/runs`
- `GET /api/workflows/{thread_id}/ci/runs/{run_id}`
- `GET /api/workflows/{thread_id}/ci/audit`

Analytics endpoint:

- `GET /api/evaluations/ci/metrics`

Git promotion continues to use its existing endpoints, but now requires current CI evidence under the same rules both during the workflow and after completion.

## Stable Failure Types

Execution and gate failures:

- `ci_pipeline_stale`
- `ci_pipeline_timeout`
- `ci_step_timeout`
- `ci_environment_unavailable`
- `ci_command_not_allowed`
- `ci_build_failed`
- `ci_tests_failed`
- `ci_lint_failed`
- `ci_package_failed`
- `ci_execution_interrupted`

Repair failures:

- `ci_repair_not_applicable`
- `ci_repair_no_changes`
- `ci_repair_validation_failed`
- `ci_repair_commit_not_advanced`
- `ci_repair_exhausted`
- `ci_repair_failed`

Promotion-blocking reasons:

- `ci_required_for_promotion`
- `ci_commit_mismatch`
- `ci_run_not_completed`
- `ci_promotion_blocked`
- `ci_warnings_blocked`

## Invariants

- Promotion may only use CI evidence with `source_mode=commit`.
- Promotion may only use a valid SHA in `source_commit`.
- `ci_validated_commit` must match the current workflow head to be eligible.
- Accepted CI for commit `A` never authorizes promotion of commit `B`.
- Repair must create a new commit SHA.
- Repair with no attributed changes terminates explicitly with `ci_repair_no_changes`.
- Infrastructure failures never enter the repair loop.
- Replay and time travel are read-only for CI history.
- Historical gate and pipeline policy snapshots are not recomputed retroactively.

## Known Limitations

- Local CI only; no GitHub Actions integration.
- No external CI provider integration.
- No deployment or real CD pipeline.
- No artifact registry publishing.
- No security or SAST gate yet.
- Repair is bounded by `CI_REPAIR_MAX_ATTEMPTS`.
- Promotion is still manually governed; CI acceptance does not auto-promote.
- Command allowlist remains intentionally narrow and framework-specific.
