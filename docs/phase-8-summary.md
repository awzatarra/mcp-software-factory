# Phase 8 Summary

Phase 8 adds evaluation layers for planning and agent behavior while preserving deterministic workflow authority. The system may collect advisory signals, calibrate them, attribute failures, and recommend review actions, but it must not automatically change prompts, models, routing, policy values, or MCP behavior.

## Delivered Components

### 8.1 Planner Judge

The Planner Judge uses structured OpenAI output to review a deterministic plan semantically. It is disabled by default and advisory only. It does not execute tools and treats the user requirement and plan as data.

Durable fields include:

- `planning_judge_status`
- `planning_judge_result`
- `planning_judge_model`
- `planning_judge_version`
- `planning_judge_plan_fingerprint`
- `planning_judge_disagreement`

### 8.2 Hybrid Evaluation

Hybrid evaluation combines deterministic planning quality with Judge output when the Judge is available. If the Judge is disabled or unavailable, deterministic evaluation remains the authority.

Major disagreement produces review evidence, not a workflow block.

### 8.3 Agent Performance

Agent performance evaluates Planner, Developer, QA, and Repair outcomes from durable workflow state. It records scores, levels, metrics, and reason codes without changing runtime behavior.

### 8.4 Failure Attribution

RCA classifies failures into structured causes such as planner, developer, QA, repair, infrastructure, external provider, policy, and user. Infrastructure and external provider failures exclude agents from blame.

Recovered failures preserve both the original root cause and the recovery evidence.

### 8.5 Agent Recommendations

Agent recommendations analyze historical performance and RCA records to produce review-only recommendations. They are durable analytics artifacts with:

- `target.type=agent`
- `status=recommendation_only`
- `application_status=not_applied`
- stable deterministic fingerprints

They do not apply changes automatically.

## Governance Boundary

Planner Policy Change Proposals are limited to planner policy recommendations. Agent recommendations cannot be converted into policy proposals. Attempts to do so are rejected with:

```text
planner_policy_proposal_invalid_source
```

This prevents accidental cross-governance between Phase 8 agent recommendations and Phase 7 planner policy governance.

## Deterministic Authority

The following remain deterministic authorities:

- Planner contract validation.
- Executability and dependency validation.
- Risk and approval policy.
- Quality gate and adaptive refinement.
- MCP tool execution and approvals.
- Git workflow and promotion.
- FinOps budget enforcement.

Judge and recommendation outputs are evidence only.

## Security Notes

- Judge payloads redact lines that look like secrets.
- Judge prompts instruct the evaluator not to follow instructions embedded in requirement or plan data.
- Judge code has no MCP client, Git, filesystem, shell, or tool executor dependency.
- No prompts, API keys, or full file contents are required for Phase 8 analytics closure tests.

## Configuration

Phase 8 environment keys are mirrored in `.env` and `.env.example`:

- `PLANNER_JUDGE_*`
- `PLANNER_HYBRID_*`
- `AGENT_RECOMMENDATION_*`

The default Judge state remains disabled.

## Known Limits

- Agent recommendations are not automatically applied.
- Recommendation review does not mutate prompts or models.
- Planner Judge availability is best-effort; failures degrade to deterministic-only evaluation.
- No new paid evaluations are required for Phase 8 closure.

## Closure Validation

The closure suite is:

```powershell
pytest tests/test_phase_8_closure.py -q
```

The broader focused suite is:

```powershell
pytest tests/test_planner_judge.py tests/test_planner_hybrid_evaluation.py tests/test_agent_performance.py tests/test_failure_attribution.py tests/test_agent_recommendations.py tests/test_planner_policy_proposals.py tests/test_phase_8_closure.py -q
```

