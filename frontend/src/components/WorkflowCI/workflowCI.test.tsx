import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";

import * as workflows from "../../api/workflows";
import { WorkflowCITab } from "./WorkflowCITab";
import type { CIPipelineRun, CIWorkflowStatus } from "../../api/types";

const pipeline = {
  pipeline_id: "fastapi-default",
  name: "Fastapi default CI",
  version: "6.21.1-v1",
  framework: "fastapi",
  fail_fast: true,
  timeout_seconds: 600,
  steps: [
    { step_id: "test", name: "Run tests", type: "test" as const, command: ["python", "-m", "pytest"], working_directory: null, timeout_seconds: null, required: true, continue_on_error: false },
  ],
};

const status: CIWorkflowStatus = {
  workflow_id: "thread-1",
  project_id: "health-api",
  state: "not_started",
  latest_run: null,
  pipeline,
  pipeline_fingerprint: "f".repeat(64),
  source: { source_mode: "working_tree", source_commit: null, source_branch: "workflow/thread", dirty: true, effective_clean: false },
};

const passedRun: CIPipelineRun = {
  ci_run_id: "run-1",
  workflow_id: "thread-1",
  project_id: "health-api",
  pipeline,
  pipeline_fingerprint: "f".repeat(64),
  status: "passed",
  source: status.source!,
  started_at: "2026-08-01T00:00:00Z",
  completed_at: "2026-08-01T00:00:02Z",
  duration_seconds: 2,
  failed_step: null,
  failure_type: null,
  failure_message: null,
  warnings: [],
  gate_policy_version: "6.21.2-v1",
  decision: "accepted",
  failed_gates: [],
  warning_gates: [],
  blocking_gate: null,
  gate_summary: { total: 4, passed: 1, failed: 0, warning: 0, skipped: 0, not_applicable: 3, blocking_failed_count: 0 },
  gates: [
    { gate_id: "build-gate", type: "build", status: "not_applicable", required: false, blocking: false, reason: "no_source_steps", source_steps: [], failure_type: null },
    { gate_id: "test-gate", type: "test", status: "passed", required: true, blocking: true, reason: "minimum_success_count_met", source_steps: ["test"], failure_type: null },
    { gate_id: "lint-gate", type: "lint", status: "not_applicable", required: false, blocking: false, reason: "no_source_steps", source_steps: [], failure_type: null },
    { gate_id: "package-gate", type: "package", status: "not_applicable", required: false, blocking: false, reason: "no_source_steps", source_steps: [], failure_type: null },
  ],
  steps: [{
    step_id: "test", name: "Run tests", type: "test", status: "passed",
    started_at: "2026-08-01T00:00:00Z", completed_at: "2026-08-01T00:00:02Z",
    duration_seconds: 2, exit_code: 0, stdout_summary: "1 passed", stderr_summary: "",
    output_truncated: false, failure_type: null, failure_message: null,
  }],
};

describe("WorkflowCITab", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("renders pipeline status, source and steps", async () => {
    vi.spyOn(workflows, "getWorkflowCI").mockResolvedValue({ ...status, state: "available", latest_run: passedRun });
    vi.spyOn(workflows, "getWorkflowCIRuns").mockResolvedValue({ workflow_id: "thread-1", runs: [passedRun], total: 1 });
    vi.spyOn(workflows, "getWorkflowCIAudit").mockResolvedValue({
      workflow_id: "thread-1",
      total: 3,
      entries: [
        { event_type: "ci_pipeline_started", timestamp: "2026-08-01T00:00:00Z", workflow_id: "thread-1", ci_run_id: "run-1", commit: "a".repeat(40), step_id: null, gate: null, decision: null, failure_type: null, repair_attempt: null, repair_commit: null, promotion_eligible: null, metadata: {} },
        { event_type: "ci_gate_evaluated", timestamp: "2026-08-01T00:00:02Z", workflow_id: "thread-1", ci_run_id: "run-1", commit: "a".repeat(40), step_id: null, gate: "test", decision: null, failure_type: null, repair_attempt: null, repair_commit: null, promotion_eligible: null, metadata: {} },
        { event_type: "ci_decision_completed", timestamp: "2026-08-01T00:00:02Z", workflow_id: "thread-1", ci_run_id: "run-1", commit: "a".repeat(40), step_id: null, gate: null, decision: "accepted", failure_type: null, repair_attempt: null, repair_commit: null, promotion_eligible: null, metadata: {} },
      ],
    });

    render(<WorkflowCITab threadId="thread-1" />);

    const view = await screen.findByLabelText("CI del workflow");
    expect(within(view).getByText("fastapi")).toBeVisible();
    expect(within(view).getAllByText("accepted").length).toBeGreaterThanOrEqual(1);
    expect(within(view).getByText("minimum_success_count_met")).toBeVisible();
    expect(within(view).getByText("working_tree")).toBeVisible();
    expect(within(view).getByText("CI Audit Trail")).toBeVisible();
    expect(within(view).getByText("ci pipeline started")).toBeVisible();
    expect(within(view).getByText("ci decision completed")).toBeVisible();
    expect(within(view).getAllByText("Run tests")).toHaveLength(2);
    await userEvent.click(within(view).getByText("Run tests", { selector: "summary" }));
    expect(within(view).getByText("1 passed")).toBeVisible();
  });

  it("prepares and runs CI explicitly", async () => {
    vi.spyOn(workflows, "getWorkflowCI").mockResolvedValue(status);
    vi.spyOn(workflows, "getWorkflowCIRuns").mockResolvedValue({ workflow_id: "thread-1", runs: [], total: 0 });
    vi.spyOn(workflows, "getWorkflowCIAudit").mockResolvedValue({ workflow_id: "thread-1", total: 0, entries: [] });
    vi.spyOn(workflows, "prepareWorkflowCI").mockResolvedValue({
      workflow_id: "thread-1", project_id: "health-api", state: "available",
      pipeline, pipeline_fingerprint: "f".repeat(64), source: status.source!,
      gate_policy_version: "6.21.2-v1",
      expected_gates: [
        { gate_id: "build-gate", type: "build", required: false, blocking: false, source_step_types: ["build"], minimum_success_count: null, allow_skipped: false, policy_version: "6.21.2-v1" },
        { gate_id: "test-gate", type: "test", required: true, blocking: true, source_step_types: ["test"], minimum_success_count: 1, allow_skipped: false, policy_version: "6.21.2-v1" },
        { gate_id: "lint-gate", type: "lint", required: false, blocking: false, source_step_types: ["lint"], minimum_success_count: null, allow_skipped: false, policy_version: "6.21.2-v1" },
        { gate_id: "package-gate", type: "package", required: false, blocking: false, source_step_types: ["package"], minimum_success_count: null, allow_skipped: false, policy_version: "6.21.2-v1" },
      ],
    });
    const run = vi.spyOn(workflows, "runWorkflowCI").mockResolvedValue(passedRun);

    render(<WorkflowCITab threadId="thread-1" />);
    expect(await screen.findByText("CI Pipeline")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: /Prepare/i }));
    await userEvent.click(screen.getByRole("button", { name: /Run/i }));

    expect(run).toHaveBeenCalledWith("thread-1", "f".repeat(64));
  });
});
