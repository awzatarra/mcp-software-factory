# Phase 7 Summary

Phase 7 closes governed planner intelligence for the Software Factory. It validates planner contracts, executability, risk, quality, calibration, policy governance, rollout, experiments, statistics, promotion readiness, and portfolio conflict handling without automatic policy optimization.

## Architecture

- Planning remains a LangGraph subgraph with explicit state contracts.
- Planner evaluation is deterministic and stored with workflow snapshots.
- Calibration reads durable workflow records and emits recommendation-only insights.
- Human governance converts accepted recommendations into proposals.
- Runtime policy changes happen only through explicit application and rollback actions.
- Rollouts and experiments pin per-workflow policy snapshots.
- Portfolio checks prevent conflicting active experiments and rollouts.

## Subphases

- 7.1: Planner contract validation for schema, supported values, and requirements.
- 7.2: Executability and dependency validation with deterministic task ordering.
- 7.3: Plan risk and impact analysis.
- 7.4: Risk-based approval policy with durable human interrupt.
- 7.5: Plan quality scoring and decision confidence.
- 7.6: Quality gate and adaptive refinement.
- 7.7: Planner evaluation and calibration record generation.
- 7.8: Calibration analytics and policy recommendations.
- 7.9: Recommendation review and governance.
- 7.10: Policy change proposal generation.
- 7.11: Controlled policy application and rollback.
- 7.12: Policy rollout, canary, and impact monitoring.
- 7.13: Multi-variant policy experimentation.
- 7.14: Statistical confidence and experiment decision quality.
- 7.15: Experiment promotion readiness and governed recommendations.
- 7.16: Experiment portfolio and conflict governance.

## Core Invariants

- Recommendation accepted does not mean applied.
- Proposal approved does not mean applied.
- Application prepared does not mutate runtime policy.
- Rollout healthy does not auto-advance.
- Experiment winner does not auto-promote.
- Promotion ready does not auto-apply.
- Critical or sensitive planning changes require explicit human approval.
- Stale fingerprints never produce mutations.
- Replay and time travel are read-only for governance stores.
- Workflow policy assignment snapshots remain pinned across runtime changes.

## Environment Variables

- `POLICY_EXPERIMENT_CONFIDENCE_LEVEL`
- `POLICY_EXPERIMENT_WINNER_CONFIDENCE_MIN`
- `POLICY_EXPERIMENT_EQUIVALENCE_MARGIN`
- `POLICY_EXPERIMENT_SAFETY_MIN_SAMPLE`
- `POLICY_EXPERIMENT_PROMOTION_READINESS_MIN`
- `POLICY_EXPERIMENT_STABILITY_MIN_SAMPLE`

`.env` and `.env.example` must contain the same variable names. Secret values are not printed by validation tests.

## Main API Areas

- `/api/evaluations/planner/recommendations`
- `/api/evaluations/planner/policy-proposals`
- `/api/evaluations/planner/policy-applications`
- `/api/evaluations/planner/policy-rollouts`
- `/api/evaluations/planner/policy-experiments`
- `/api/evaluations/planner/policy-experiments/portfolio`

## Durable Stores And Tables

- `planner_recommendation_reviews`
- `planner_recommendation_review_history`
- `planner_policy_proposals`
- `planner_policy_proposal_history`
- `planner_runtime_policies`
- `planner_policy_applications`
- `planner_policy_application_history`
- `planner_policy_rollouts`
- `planner_policy_rollout_history`
- `planner_policy_workflow_snapshots`
- `planner_policy_experiments`
- `planner_policy_experiment_variants`
- `planner_policy_experiment_history`
- `planner_policy_experiment_snapshots`

## Known Limitations

- No LLM judge is implemented for Phase 7.
- No auto-tuning is implemented.
- No auto-promote is implemented.
- No automatic winner application is implemented.
- Experiment statistics are controlled approximations, not a general scientific platform.
- Governance requires explicit human actions.
- Evaluation quality depends on structured failure classification.
- Portfolio governance covers registered planner policies and declared relationships only.

## Explicitly Not Implemented

- Bandits.
- Adaptive experiments.
- Causal inference.
- Automatic policy optimization.
- Automatic policy application.
- New MCP Servers.

