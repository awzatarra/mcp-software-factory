# Phase 6.21 CI Pipeline

Phase 6.21.1 introduces local CI pipeline modeling and execution. It does not integrate GitHub Actions, deployment, PR creation, or automatic CI repair.

Phase 6.21.2 adds deterministic CI gates on top of step execution. Step execution answers "what command ran and how did it exit"; gate evaluation answers "does this CI result satisfy governance for build/test/lint/package".

## Model

The CI model is durable and stored in SQLite:

- `CIPipelineDefinition`
- `CIPipelineStep`
- `CIPipelineRun`
- `CIStepRun`
- `CIPipelineResult` semantics are represented by the final `CIPipelineRun` status and failure summary.

Pipeline definitions include:

- `pipeline_id`
- `name`
- `version`
- `framework`
- `steps`
- `fail_fast`
- `timeout_seconds`

Each step includes:

- `step_id`
- `name`
- `type`
- `command`
- `working_directory`
- `timeout_seconds`
- `required`
- `continue_on_error`

## Supported Frameworks

### FastAPI / Python

Discovery checks Python project files such as `requirements.txt`, `pyproject.toml`, and `pytest.ini`.

Default command:

```text
python -m pytest
```

CI does not perform arbitrary dependency installation. It reuses the environment already available to the workflow.

### Node

Discovery reads `package.json` and uses only existing scripts:

- `npm run build`
- `npm test`
- `npm run lint`

Missing scripts are not invented.

### .NET

Discovery checks `.sln` and `.csproj` files.

Default commands:

```text
dotnet restore
dotnet build --no-restore
dotnet test --no-build
```

## Command Safety

Commands execute with:

- `shell=False`
- `stdin=DEVNULL`
- `stdout=PIPE`
- `stderr=PIPE`
- explicit timeout
- `CREATE_NO_WINDOW` on Windows when available

Allowed executables:

- `python`
- `pytest`
- `pip`
- `uv`
- `dotnet`
- `npm`
- `npx`

Shell entrypoints and destructive commands are rejected, including `powershell`, `cmd /c`, `bash -c`, `rm`, `del`, pipes, redirection, and `eval`.

## Statuses

Step statuses:

- `pending`
- `running`
- `passed`
- `failed`
- `skipped`
- `timed_out`
- `cancelled`
- `interrupted`

Pipeline statuses:

- `pending`
- `running`
- `passed`
- `failed`
- `timed_out`
- `cancelled`
- `interrupted`

CI status is separate from workflow `terminal_status`.

## Step vs Gate

CI steps and CI gates are intentionally separate:

- A step is a subprocess execution result.
- A gate is a deterministic decision over one or more step results.
- Gates never execute subprocesses.
- Gates are persisted with each run and are not silently recalculated with a newer policy.

Supported gate types:

- `build`
- `test`
- `lint`
- `package`

Gate statuses:

- `passed`
- `failed`
- `warning`
- `skipped`
- `not_applicable`

`not_applicable` means the project legitimately does not expose that gate. `skipped` means a relevant source step was skipped, usually because a previous blocking gate/step failed.

## Required vs Blocking

`required` and `blocking` are independent.

Example:

```text
lint required=true blocking=false
```

means lint should run when configured, but a lint failure produces a warning instead of rejecting the CI decision.

Default policy:

- build: blocking when a build step exists
- test: blocking when a test step exists
- lint: non-blocking warning by default
- package: non-blocking by default

## CI Decision

Each run now includes a governance decision:

- `accepted`
- `accepted_with_warnings`
- `rejected`

Rules:

- Any failed blocking gate => `rejected`
- No blocking failure but at least one warning gate => `accepted_with_warnings`
- Required gates pass and no warnings => `accepted`

Pipeline `status` remains the technical execution result. `decision` is the governance result.

Example:

```text
status=passed
decision=accepted_with_warnings
lint gate=warning
```

This can happen when lint is non-blocking and fails, while required build/test gates pass.

## Failure Types

- `ci_pipeline_stale`
- `ci_pipeline_timeout`
- `ci_step_timeout`
- `ci_command_not_allowed`
- `ci_environment_unavailable`
- `ci_build_failed`
- `ci_tests_failed`
- `ci_lint_failed`
- `ci_package_failed`
- `ci_execution_interrupted`
- `ci_build_gate_failed`
- `ci_test_gate_failed`
- `ci_lint_gate_failed`
- `ci_package_gate_failed`
- `ci_required_for_promotion`
- `ci_promotion_blocked`
- `ci_commit_mismatch`
- `ci_repair_not_applicable`
- `ci_repair_no_changes`
- `ci_repair_validation_failed`
- `ci_repair_commit_not_advanced`
- `ci_repair_exhausted`
- `ci_repair_failed`

## API

```text
GET  /api/workflows/{thread_id}/ci
POST /api/workflows/{thread_id}/ci/prepare
POST /api/workflows/{thread_id}/ci/run
GET  /api/workflows/{thread_id}/ci/runs
GET  /api/workflows/{thread_id}/ci/runs/{run_id}
```

`prepare` discovers the project, builds a deterministic pipeline, validates commands, captures source revision, and returns a fingerprint.

`run` requires the prepared fingerprint. If source or definition changed, it rejects with:

```text
ci_pipeline_stale
```

## Source Revision

If Git is available, CI records:

- source branch
- source commit
- dirty state
- effective clean state using the shared Git dirty path policy

If the repository is clean and a commit exists:

```text
source_mode=commit
```

Otherwise:

```text
source_mode=working_tree
```

## Git Binding

When CI is used as promotion evidence, the run validates an exact Git commit:

- `source_mode=commit`
- `source_commit=<40/64 hex SHA>`
- `source_branch=<workflow branch or current branch>`
- `workflow_branch=<workflow branch>`
- `repository_root=<project root>`

Symbolic values such as `HEAD` are not accepted as `source_commit`.

After a Git commit, LangGraph routes through `ci_pipeline` when `CI_PROMOTION_REQUIRED=true` and the durable CI evidence does not match the workflow head. If the exact commit already has an accepted durable CI run, the node reuses it and does not rerun subprocesses.

## Promotion Eligibility

Promotion uses the latest completed CI run for the exact workflow commit. It ignores `running`, `cancelled`, `interrupted`, and stale commit runs.

Default promotion policy:

- `accepted` => eligible
- `accepted_with_warnings` => eligible when `CI_PROMOTION_ALLOW_WARNINGS=true`
- `rejected` => blocked
- no exact CI run => blocked when `CI_PROMOTION_REQUIRED=true`

Promotion preview persists CI evidence and the promotion fingerprint includes CI run id, source commit, decision, pipeline fingerprint, and gate policy version.

## CI Repair Loop

Rejected commit-bound CI runs are classified before Repair is allowed.

Repairability categories:

- `repairable_code`
- `repairable_tests`
- `repairable_lint`
- `repairable_build`
- `infrastructure`
- `configuration`
- `non_repairable`
- `unknown`

Only structured repairable code/test/lint/build failures enter Repair. Infrastructure and configuration failures terminate without invoking Repair.

```text
CI rejected
classify failure
repairable + attempts remaining
TestingRepair/local validation
Git repair commit approval
new repair commit SHA
CI rerun on new SHA
accepted => promotion eligible
rejected => retry or exhausted
```

CI repair attempts are tracked separately from normal test repair attempts. Every repair attempt must produce a new Git commit before CI can run again; otherwise the workflow fails with `ci_repair_commit_not_advanced`.

The workflow stores bounded `ci_repair_lineage` with source CI run, source commit, repair commit, result CI run, and result decision.

## Operational Metrics And Audit

Phase 6.21.5 adds read-only operational analytics. It does not change CI execution, gate policy, Repair semantics, Git promotion, or retry limits.

Metrics are exposed through:

```text
GET /api/evaluations/ci/metrics
```

Supported query parameters:

- `limit`
- `framework`
- `from`
- `to`

The default limit is bounded by:

```text
CI_ANALYTICS_DEFAULT_LIMIT=500
CI_ANALYTICS_MAX_LIMIT=5000
```

The response includes:

- summary counts and rates
- step metrics by step type
- gate metrics by gate type
- failure type and repair category breakdown
- code vs infrastructure vs configuration classification
- CI Repair success, exhaustion, attempts, and recovery counts
- promotion eligibility and block reason counts
- commit vs working-tree source mode counts

Rate denominators are explicit:

- `acceptance_rate`, `warning_rate`, and `rejection_rate` use completed runs with a persisted CI decision.
- duration averages and percentiles use terminal runs with a valid persisted duration.
- `repair_success_rate` uses repair chains with a terminal repair result.
- infrastructure/configuration failures are separated from code-related failures and do not penalize code metrics.

Percentiles use a deterministic nearest-rank method over sorted persisted durations.

Workflow audit is exposed through:

```text
GET /api/workflows/{thread_id}/ci/audit
```

The audit trail reconstructs:

```text
workflow -> source commit -> CI run -> gates -> decision
         -> repair classification -> repair attempt -> repair commit
         -> next CI run -> promotion eligibility
```

Audit entries are safe summaries only. They must not include full stdout/stderr, prompts, file contents, patches, tokens, credentials, authorization headers, or secrets.

Operational labels, where applicable, should stay low cardinality:

- allowed: `framework`, `step_type`, `gate_type`, `decision`, `failure_type`
- disallowed as labels: `workflow_id`, `ci_run_id`, commit SHA

## Output Handling

Step stdout and stderr are summarized and bounded by:

```text
CI_STEP_OUTPUT_MAX_CHARS
```

Output is sanitized before persistence to avoid storing tokens, passwords, API keys, credentials, or authorization material.

## Configuration

```text
CI_PIPELINE_VERSION=6.21.1-v1
CI_PIPELINE_TIMEOUT_SECONDS=600
CI_STEP_TIMEOUT_SECONDS=180
CI_STEP_OUTPUT_MAX_CHARS=20000
CI_GATE_POLICY_VERSION=6.21.2-v1
CI_BUILD_GATE_BLOCKING=true
CI_TEST_GATE_BLOCKING=true
CI_LINT_GATE_BLOCKING=false
CI_PACKAGE_GATE_BLOCKING=false
CI_GIT_INTEGRATION_VERSION=6.21.3-v1
CI_PROMOTION_REQUIRED=true
CI_PROMOTION_ALLOW_WARNINGS=true
CI_REPAIR_VERSION=6.21.4-v1
CI_REPAIR_ENABLED=true
CI_REPAIR_MAX_ATTEMPTS=2
CI_OBSERVABILITY_VERSION=6.21.5-v1
CI_ANALYTICS_DEFAULT_LIMIT=500
CI_ANALYTICS_MAX_LIMIT=5000
```

These keys are mirrored in `.env` and `.env.example`.

## Known Limitations

- No GitHub Actions.
- No deployment.
- No unlimited CI repair retries.
- No global audit endpoint in 6.21.5.
- No cancel endpoint in 6.21.1.
- No dependency installation beyond already available project environment.
- No deployment/CD.
