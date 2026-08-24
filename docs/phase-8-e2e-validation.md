# Phase 8 E2E Validation

This checklist closes Phase 8 without adding new runtime behavior. It validates that Planner Judge, hybrid evaluation, agent performance, failure attribution, and agent recommendations coexist with the deterministic workflow authority.

## Scope

- Phase 8.1: LLM-as-Judge for Planner Evaluation.
- Phase 8.2: Hybrid Evaluation and Judge Calibration.
- Phase 8.3: Agent Performance Evaluation.
- Phase 8.4: Failure Attribution and Root Cause Analysis.
- Phase 8.5: Adaptive Agent Recommendations.

Out of scope:

- Auto-applying recommendations.
- Runtime prompt mutation.
- Model mutation.
- Agent routing mutation.
- New MCP tools or MCP server contract changes.

## Environment

Phase 8 configuration keys must exist in both `.env` and `.env.example`:

- `PLANNER_JUDGE_ENABLED`
- `PLANNER_JUDGE_MODEL`
- `PLANNER_JUDGE_TIMEOUT_SECONDS`
- `PLANNER_JUDGE_MAX_RETRIES`
- `PLANNER_JUDGE_PROMPT_VERSION`
- `PLANNER_HYBRID_VERSION`
- `PLANNER_HYBRID_DETERMINISTIC_WEIGHT`
- `PLANNER_HYBRID_JUDGE_WEIGHT`
- `PLANNER_HYBRID_MAJOR_DISAGREEMENT_THRESHOLD`
- `AGENT_RECOMMENDATION_VERSION`
- `AGENT_RECOMMENDATION_MIN_SAMPLE_SIZE`
- `AGENT_RECOMMENDATION_SEGMENT_MIN_SAMPLE_SIZE`

Default validation should keep `PLANNER_JUDGE_ENABLED=false` unless a paid manual Judge check is explicitly requested.

## Manual Scenarios

### 1. Judge Disabled

Run a normal FastAPI workflow with the Judge disabled.

Expected:

- Workflow reaches terminal completion when tests pass.
- No paid Judge request is made.
- `planning_judge_status=disabled`.
- Hybrid evaluation uses deterministic quality only.

### 2. Judge Enabled, Healthy Plan

Enable Judge only for an explicit validation run.

Expected:

- Planner Judge persists structured advisory output.
- `planning_valid` remains controlled by deterministic validation.
- No MCP tool, filesystem, Git, shell, or network tool is exposed to the Judge beyond the OpenAI structured output request.
- LLM cost tracking records the operation as `planner_judge` when usage is reported by the provider.

### 3. Judge Failure Fallback

Simulate a Judge timeout or invalid structured output.

Expected:

- `planning_judge_status=unavailable`.
- Workflow continues using deterministic planning outputs.
- No terminal failure is caused solely by the Judge.

### 4. Major Disagreement

Use a state where deterministic quality is high and Judge score is low.

Expected:

- Hybrid evaluation flags `hybrid_major_disagreement`.
- Recommendation is `review_recommended`.
- No automatic block, approval change, or routing mutation occurs.

### 5. Recovered Implementation Defect

Run a workflow that fails tests once and is repaired successfully.

Expected:

- RCA marks the result as recovered.
- Developer is the root cause when the generated implementation caused the test failure.
- QA is credited as detector.
- Repair is credited as resolver.

### 6. Infrastructure Failure

Simulate an MCP timeout or infrastructure error.

Expected:

- RCA root cause is `infrastructure`.
- Developer, QA, and Repair are excluded from blame.
- Agent recommendations do not use infrastructure failures as agent defects.

### 7. Agent Recommendations

Analyze enough historical workflows to produce an agent recommendation.

Expected:

- Recommendation has `target.type=agent`.
- Status remains `recommendation_only`.
- `application_status=not_applied`.
- Accepting/reviewing the recommendation does not mutate runtime prompts, models, routing, or policy values.

### 8. Governance Cross-Regression

Attempt to create a Planner Policy Change Proposal from an accepted Agent Recommendation.

Expected:

- The API rejects it with `planner_policy_proposal_invalid_source`.
- No policy proposal is created.
- Planner policy proposals remain limited to planner policy recommendations.

## Automated Validation

Run:

```powershell
pytest tests/test_planner_judge.py tests/test_planner_hybrid_evaluation.py tests/test_agent_performance.py tests/test_failure_attribution.py tests/test_agent_recommendations.py tests/test_planner_policy_proposals.py tests/test_phase_8_closure.py -q
pytest -q
```

Frontend validation:

```powershell
cd frontend
npm.cmd test
npm.cmd run lint
npm.cmd run typecheck
npm.cmd run build
```

## Acceptance Criteria

- No duplicate Judge calls on replay of an unchanged judged plan.
- No duplicate costs from idempotent deterministic nodes.
- No infrastructure failures counted as agent defects.
- No recommendation applies itself.
- No Agent Recommendation can create a Planner Policy Change Proposal.
- UI and API expose advisory evaluation/recommendation data without implying runtime mutation.
- MCP Servers are not functionally modified.

