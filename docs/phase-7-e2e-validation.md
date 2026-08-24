# Phase 7 E2E Validation Checklist

Use deterministic fixtures whenever possible. Do not run paid LLM evaluations for this closure pass.

## A. Low-Risk Workflow

- Create `phase-7-e2e-low` as a FastAPI project with `GET /health` returning `{"status":"ok"}` and automatic tests.
- Confirm planning contract is valid, execution order exists, risk is `low`, quality gate is `continue`, approval is `not_required`, and the workflow reaches implementation, testing, Git, and success.
- Confirm `planning_evaluation.outcome=successful`.

## B. Risk Approval Approve

- Run a medium or high risk planning fixture that modifies dependencies or configuration.
- Confirm the workflow interrupts at `planning_risk_approval` before implementation.
- Approve once and confirm `workflow_resumed`, `planning_approval_granted`, and exactly one implementation pass.

## C. Risk Rejection

- Reuse the risk approval fixture and reject the planning approval.
- Confirm `terminal_status=user_cancelled`, `failure_type=user_rejected`, `failure_stage=planning_risk_approval`, and no implementation execution.
- Restart and replay must not resume the rejected workflow.

## D. Quality Refinement

- Use a controlled low-quality initial plan with vague tasks such as `hacer backend` and `tests`.
- Confirm quality gate returns `refine`, then full revalidation runs after refinement.
- Confirm `planning_quality_refinement_attempts > 0`, `planning_quality_score_delta > 0`, final gate is `continue`, and stale approval fingerprints are invalidated.

## E. Policy Governance

- Generate planner analytics, accept one recommendation, create a policy proposal, move it to `ready_for_review`, and approve it.
- Confirm `application_status=not_applied` and runtime policy revision is unchanged.

## F. Application And Rollback

- Prepare an application from the approved proposal.
- Inspect the preview, then apply explicitly.
- Confirm runtime revision increments and verification succeeds.
- Roll back manually and confirm the previous value is restored with a new revision.

## G. Rollout

- Prepare and start rollout for a compatible applied policy.
- Confirm deterministic assignment and snapshot pinning.
- With healthy fixture data, advance one stage.
- With degraded fixture data, confirm advance is blocked.
- Roll back and confirm new workflows resolve the base policy while in-flight workflows keep their pinned snapshot.

## H. Experiment

- Create a control/A/B experiment with valid allocation.
- Confirm deterministic assignment, snapshot pinning, statistical summary, guardrails, and promotion readiness.
- With fixture data where A wins and B is unsafe, confirm winner is A and the promotion output is recommendation-only.
- Runtime policy must not change.

## I. Portfolio Conflict

- Start an experiment for `planning.quality_gate.weak_threshold/global_planner`.
- Attempt another experiment for the same policy and scope and expect `planner_experiment_portfolio_conflict`.
- Attempt an independent policy/scope and confirm it is permitted.
- Attempt a rollout for the same active policy and confirm it is blocked.
- Inspect `/api/evaluations/planner/policy-experiments/portfolio`.

## Restart And Idempotency

- Restart with pending risk approval, accepted recommendation, ready proposal, prepared application, running rollout, and running experiment.
- Confirm IDs, fingerprints, status, and snapshots survive.
- Repeat approve/reject/review/proposal/application/rollout/experiment actions and confirm no duplicate critical events or policy mutations.

## Replay And Time Travel

- Replay must show original planning evaluation and policy assignments from the snapshot.
- Replay must not approve, apply, roll back, advance rollout, start experiment, create recommendations, or mutate runtime policy state.

