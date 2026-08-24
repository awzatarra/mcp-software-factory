# Phase 6.21 E2E Validation

Manual checklist for Phase 6.21 closure as of August 14, 2026.

## Scope

Validate end-to-end coexistence of:

- CI pipeline execution
- CI gates
- Git to CI binding
- CI repair
- CI analytics and audit
- Supervisor routing
- Persistence and restart behavior
- Replay and time travel read-only behavior

## A. Happy CI

Scenario:

- Create a FastAPI project with `GET /health` returning `{"status":"ok"}`
- Tests pass locally
- Git commit is created
- CI runs for the exact workflow SHA

Expected:

- `git_developer_commit_sha`, CI `source.source_commit`, `ci_validated_commit`, and promotion preview commit match exactly
- `decision=accepted`
- `ci_promotion_eligible=true`
- No repair state is started

## B. Warning CI

Scenario:

- Build and tests pass
- Lint fails as a non-blocking gate

Expected:

- `lint` gate status is `warning`
- `decision=accepted_with_warnings`
- Promotion eligibility depends on `CI_PROMOTION_ALLOW_WARNINGS`
- No repair is triggered

## C. Rejected CI

Scenario:

- Tests fail in CI

Expected:

- `test` gate status is `failed`
- `decision=rejected`
- Repairability category is code-related and repairable
- Promotion remains blocked

## D. Repair Success

Scenario:

- Commit `A` is rejected by CI
- Repair changes are applied
- Local validation passes
- Repair commit `B` is created
- CI reruns only for `B`

Expected:

- Repair lineage records `A -> B`
- `ci_repair_state=repaired` or running to accepted completion depending on observation point
- `ci_validated_commit=B`
- `ci_promotion_eligible=true`
- CI evidence from `A` is not reused for `B`

## E. Repair Exhausted

Scenario:

- CI rejects `A`
- Repair commit `B` is still rejected
- Repair commit `C` is still rejected
- `CI_REPAIR_MAX_ATTEMPTS=2`

Expected:

- `ci_repair_state=exhausted`
- `terminal_status=ci_failed`
- `failure_type=ci_repair_exhausted`
- No promotion path is available

## F. Infrastructure Failure

Scenario:

- Simulate `ci_step_timeout`, `ci_pipeline_timeout`, or `ci_environment_unavailable`

Expected:

- No repair loop starts
- `terminal_status=infrastructure_failed`
- Failure remains classified as infrastructure in analytics

## G. Commit Mismatch and Stale Promotion

Scenario 1:

- CI validated commit `A`
- Workflow head changes to `B`

Expected:

- Promotion is blocked with `ci_commit_mismatch`

Scenario 2:

- Promotion preview was prepared for `A`
- Repair or new commit advances head to `B`

Expected:

- Old promotion preview becomes stale
- Old approval cannot be reused
- New promotion requires fresh CI evidence for `B`

## H. Analytics

Seed or produce runs covering:

- accepted
- accepted with warnings
- rejected
- infrastructure failure
- repair success
- repair exhausted

Validate in `GET /api/evaluations/ci/metrics`:

- acceptance, warning, and rejection rates
- duration percentiles
- gate-level metrics
- repair metrics
- promotion metrics
- deterministic limit behavior

## I. Audit Trail

For a repaired workflow:

- commit `A`
- CI rejected
- repair
- commit `B`
- CI accepted
- promotion eligible

Validate in `GET /api/workflows/{thread_id}/ci/audit`:

- events are ordered correctly
- repair lineage is reconstructible
- commit binding is visible
- secret-like strings are redacted

## Restart and Idempotency

Validate restart safety for:

- commit already created and CI pending
- CI accepted
- CI rejected with repair pending
- repair waiting for Git approval
- repair commit created and CI pending

Expected:

- no duplicate CI runs
- no duplicate repair attempts
- no duplicate commits

## Replay and Time Travel

Replay must show:

- CI run history
- steps
- gates
- decision
- repair lineage
- promotion eligibility

Replay must not:

- execute subprocesses
- create repair commits
- mutate promotion state
- alter audit history

## Closure Criteria

Phase 6.21 is considered closed when:

- CI remains safely local
- gates drive decision semantics deterministically
- promotion requires exact, current commit-bound CI evidence
- repair always produces a new SHA or fails explicitly
- infrastructure failures do not enter repair
- restart and replay are safe
- analytics and audit are correct and sanitized
- legacy runs remain readable
