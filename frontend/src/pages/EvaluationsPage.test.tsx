import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EvaluationsPage } from "./EvaluationsPage";

vi.mock("../api/evaluations", () => ({
  getEvaluationDashboard: vi.fn(), getCIOperationalMetrics: vi.fn(), getEvaluationRuns: vi.fn(), getEvaluationRun: vi.fn(),
  getEvaluationCollection: vi.fn(), getEvaluationRubric: vi.fn(), createEvaluationRubricVersion: vi.fn(), disableEvaluationRubric: vi.fn(),
  getAgentRecommendations: vi.fn(), reviewAgentRecommendation: vi.fn(),
  getPlannerRecommendations: vi.fn(), reviewPlannerRecommendation: vi.fn(),
  getPlannerPolicyProposals: vi.fn(), createPlannerPolicyProposal: vi.fn(), transitionPlannerPolicyProposal: vi.fn(),
  getPlannerPolicyApplications: vi.fn(), preparePlannerPolicyApplication: vi.fn(), applyPlannerPolicyApplication: vi.fn(), rollbackPlannerPolicyApplication: vi.fn(),
  getPlannerPolicyRollouts: vi.fn(), preparePlannerPolicyRollout: vi.fn(), startPlannerPolicyRollout: vi.fn(), evaluatePlannerPolicyRollout: vi.fn(), advancePlannerPolicyRollout: vi.fn(), pausePlannerPolicyRollout: vi.fn(), resumePlannerPolicyRollout: vi.fn(), rollbackPlannerPolicyRollout: vi.fn(),
  getPlannerPolicyExperiments: vi.fn(), getPlannerPolicyExperimentPortfolio: vi.fn(), readyPlannerPolicyExperiment: vi.fn(), startPlannerPolicyExperiment: vi.fn(), evaluatePlannerPolicyExperiment: vi.fn(), pausePlannerPolicyExperiment: vi.fn(), resumePlannerPolicyExperiment: vi.fn(), completePlannerPolicyExperiment: vi.fn(), cancelPlannerPolicyExperiment: vi.fn(),
}));
vi.mock("../hooks/useAlertSummary", () => ({ useAlertSummary: () => ({ summary: null }) }));
vi.mock("../hooks/useNotificationSummary", () => ({ useNotificationSummary: () => ({ summary: null }) }));

import { applyPlannerPolicyApplication, createEvaluationRubricVersion, createPlannerPolicyProposal, evaluatePlannerPolicyExperiment, getAgentRecommendations, getCIOperationalMetrics, getEvaluationCollection, getEvaluationDashboard, getEvaluationRubric, getEvaluationRun, getEvaluationRuns, getPlannerPolicyApplications, getPlannerPolicyExperimentPortfolio, getPlannerPolicyExperiments, getPlannerPolicyProposals, getPlannerPolicyRollouts, getPlannerRecommendations, preparePlannerPolicyApplication, preparePlannerPolicyRollout, reviewAgentRecommendation, reviewPlannerRecommendation, rollbackPlannerPolicyApplication, rollbackPlannerPolicyRollout, startPlannerPolicyExperiment, transitionPlannerPolicyProposal, type EvaluationRubric } from "../api/evaluations";

describe("EvaluationsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getAgentRecommendations).mockResolvedValue({ items: [], review_metrics: {} });
  });

  it("renders durable evaluation KPIs", async () => {
    vi.mocked(getEvaluationDashboard).mockResolvedValue({ workflows_evaluated: 4, average_workflow_score: .88, verdicts: { excellent: 2, good: 2 }, regression_count: 1, average_agent_score: .84, average_evaluation_cost: .002, average_evaluation_duration_ms: 120, score_over_time: [{ created_at: "2026-08-08", score: .88 }], score_by_agent: [{ key: "Planner", score: .9 }], score_by_model: [{ key: "gpt-test", score: .86 }], score_vs_cost: [], score_vs_duration: [], regressions_over_time: [] });
    render(<MemoryRouter initialEntries={["/evaluations"]}><EvaluationsPage /></MemoryRouter>);
    expect((await screen.findAllByText("88%")).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Regresiones")).toBeInTheDocument();
    expect(screen.getByText("$0.0020")).toBeInTheDocument();
  });

  it("renders run navigation without exposing payloads", async () => {
    vi.mocked(getEvaluationRuns).mockResolvedValue({ items: [{ evaluation_run_id: "run-1", workflow_id: "workflow-1", trace_id: "trace-1", branch_id: "original", evaluation_type: "deterministic", status: "completed", overall_score: .9, verdict: "excellent", model: null, duration_ms: 10, created_at: "2026-01-01T00:00:00Z" }], total: 1 });
    render(<MemoryRouter initialEntries={["/evaluations/runs"]}><EvaluationsPage /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("workflow-1")).toBeInTheDocument());
    expect(screen.getByText("90%")).toBeInTheDocument();
    expect(screen.queryByText(/prompt|authorization|api key/i)).not.toBeInTheDocument();
  });

  it("keeps invalidated runs visible with an audit badge", async () => {
    vi.mocked(getEvaluationRuns).mockResolvedValue({ items: [{ evaluation_run_id: "legacy-run", workflow_id: "workflow-legacy", trace_id: "trace-legacy", branch_id: "original", evaluation_type: "deterministic", status: "completed", validity_status: "invalidated", invalidation_reason: "Known attribution defect", overall_score: .82, verdict: "good", model: null, duration_ms: 10, created_at: "2026-01-01T00:00:00Z" }], total: 1 });
    render(<MemoryRouter initialEntries={["/evaluations/runs"]}><EvaluationsPage /></MemoryRouter>);
    expect(await screen.findByText("invalidated")).toHaveAttribute("title", "Known attribution defect");
    expect(screen.getByTitle("Analytics exclude invalidated/superseded evaluation runs.")).toBeInTheDocument();
    expect(screen.getByText("82%")).toBeInTheDocument();
  });

  it("renders missing agent evidence as not evaluated instead of zero", async () => {
    vi.mocked(getEvaluationRun).mockResolvedValue({ evaluation_run_id: "run-2", workflow_id: "workflow-2", trace_id: "trace-2", branch_id: "original", evaluation_type: "deterministic", status: "completed", overall_score: 1, verdict: "excellent", model: null, duration_ms: 10, created_at: "2026-01-01T00:00:00Z", results: [{ evaluation_result_id: "result-1", agent_name: "Planner", score: null, verdict: "not_evaluated", reason: "Insufficient agent-specific deterministic evidence" }], metrics: [], evidence: [] });
    render(<MemoryRouter initialEntries={["/evaluations/runs/run-2"]}><EvaluationsPage /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("Planner")).toBeInTheDocument());
    expect(screen.getByText("n/a")).toBeInTheDocument();
    expect(screen.getByText("not evaluated")).toBeInTheDocument();
  });

  it("renders agent counts as integers and score columns as percentages", async () => {
    vi.mocked(getEvaluationCollection).mockResolvedValue({ items: [
      { agent: "Supervisor", evaluations_attempted: 8, evaluations_scored: 7, not_evaluated_count: 1, average_score: .98, min_score: .82, max_score: 1 },
      { agent: "Planner", evaluations_attempted: 8, evaluations_scored: 8, not_evaluated_count: 0, average_score: .96, min_score: .89, max_score: 1 },
      { agent: "Repair", evaluations_attempted: 8, evaluations_scored: 0, not_evaluated_count: 8, average_score: null, min_score: null, max_score: null },
    ] });
    render(<MemoryRouter initialEntries={["/evaluations/agents"]}><EvaluationsPage /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("Supervisor")).toBeInTheDocument());
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(screen.getAllByText("8").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("0").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText("700%")).not.toBeInTheDocument();
    expect(screen.queryByText("800%")).not.toBeInTheDocument();
    expect(screen.queryByText("0%")).not.toBeInTheDocument();
    expect(screen.getByText("98%")).toBeInTheDocument();
    expect(screen.getByText("82%")).toBeInTheDocument();
  });

  it("formats metric scores and renders diagnostics from raw values", async () => {
    vi.mocked(getEvaluationCollection).mockResolvedValue({ items: [
      { metric_name: "reliability_score", metric_type: "heuristic_dimension", unit: "score", samples: 4, average_value: .958333333333, average_raw_value: .958333333333, passed_count: 4, analytics_role: "score", raw_unit: null },
      { metric_name: "human_wait_score", metric_type: "heuristic_dimension", unit: "score", samples: 4, average_value: .699871, average_raw_value: .699871, passed_count: 0, analytics_role: "score", raw_unit: null },
      { metric_name: "parallelism_factor", metric_type: "heuristic", unit: "score", samples: 4, average_value: null, average_raw_value: 2.32, passed_count: null, analytics_role: "diagnostic", raw_unit: "x" },
      { metric_name: "agent_active_duration_total", metric_type: "heuristic", unit: "score", samples: 4, average_value: null, average_raw_value: 12345.6789, passed_count: null, analytics_role: "diagnostic", raw_unit: "ms" },
      { metric_name: "handled_error_count", metric_type: "diagnostic", unit: "count", samples: 4, average_value: null, average_raw_value: 2.5, passed_count: null, analytics_role: "diagnostic", raw_unit: "count" },
    ] });
    render(<MemoryRouter initialEntries={["/evaluations/metrics"]}><EvaluationsPage /></MemoryRouter>);
    await waitFor(() => expect(screen.getByText("reliability_score")).toBeInTheDocument());
    expect(screen.getByText("95.83%")).toBeInTheDocument();
    expect(screen.getByText("69.99%")).toBeInTheDocument();
    expect(screen.getByText("2.32x")).toBeInTheDocument();
    expect(screen.getByText("12,345.679 ms")).toBeInTheDocument();
    expect(screen.getByText("2.5")).toBeInTheDocument();
    expect(screen.getAllByText("n/a").length).toBeGreaterThanOrEqual(3);
    expect(screen.queryByText(/958333333333|699871000000/)).not.toBeInTheDocument();
  });

  it("renders CI operations metrics and empty failure breakdown", async () => {
    vi.mocked(getCIOperationalMetrics).mockResolvedValue({
      version: "6.21.5-v1",
      limit: 500,
      framework: null,
      summary: { total_runs: 10, completed_runs: 10, runs_with_decision: 10, accepted_runs: 8, accepted_with_warnings_runs: 1, rejected_runs: 1, acceptance_rate: .8, warning_rate: .1, rejection_rate: .1, average_pipeline_duration_seconds: 4.2, p50_pipeline_duration_seconds: 3, p95_pipeline_duration_seconds: 9, commit_bound_runs: 9, working_tree_runs: 1 },
      steps: { test: { run_count: 10, passed_count: 9, failed_count: 1, timed_out_count: 0, skipped_count: 0, average_duration_seconds: 4.2, p50_duration_seconds: 3, p95_duration_seconds: 9, failure_rate: .1 } },
      gates: { test: { evaluated_count: 10, passed_count: 9, failed_count: 1, warning_count: 0, skipped_count: 0, not_applicable_count: 0, failure_rate: .1, warning_rate: 0 }, lint: { evaluated_count: 10, passed_count: 8, failed_count: 0, warning_count: 1, skipped_count: 1, not_applicable_count: 0, failure_rate: 0, warning_rate: .1 } },
      failures: { by_failure_type: { ci_tests_failed: 1 }, by_repair_category: { repairable_tests: 1 }, code_related_failure_count: 1, infrastructure_failure_count: 0, configuration_failure_count: 0, unknown_failure_count: 0 },
      repair: { ci_repair_required_count: 1, ci_repair_success_count: 1, ci_repair_failed_count: 0, ci_repair_exhausted_count: 0, ci_repair_success_rate: 1, ci_repair_exhaustion_rate: 0, average_ci_repair_attempts: 1, maximum_ci_repair_attempts_observed: 1, commits_repaired_count: 1, average_commits_per_repair_chain: 1, runs_recovered_after_repair: 1, runs_failed_after_repair: 0 },
      promotion: { promotion_eligible_count: 9, promotion_blocked_count: 1, promotion_blocked_no_ci_count: 0, promotion_blocked_rejected_ci_count: 1, promotion_blocked_commit_mismatch_count: 0, promotion_blocked_stale_ci_count: 0, promotion_eligibility_rate: .9 },
    });
    render(<MemoryRouter initialEntries={["/evaluations/ci"]}><EvaluationsPage /></MemoryRouter>);
    expect(await screen.findByText("CI Operations")).toBeInTheDocument();
    expect(screen.getByText("Total Runs")).toBeInTheDocument();
    expect(screen.getByText("80%")).toBeInTheDocument();
    expect(screen.getAllByText("4.2s").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Failure Breakdown")).toBeInTheDocument();
    expect(screen.getByText("ci tests failed")).toBeInTheDocument();
    expect(screen.getByText("Gate Rates")).toBeInTheDocument();
  });

  it("renders rubric list with human dates and durable status badges", async () => {
    vi.mocked(getEvaluationCollection).mockResolvedValue({ items: [{ rubric_id: "planner-v1", name: "Planner quality", version: "1.0", agent_name: "Planner", dimensions: {}, weights: {}, thresholds: {}, enabled: true, version_status: "current", usage_count: 3, evaluations_valid: 3, evaluations_total: 3, created_at: "2026-01-02T12:30:00Z", updated_at: null }] });
    render(<MemoryRouter initialEntries={["/evaluations/rubrics"]}><EvaluationsPage /></MemoryRouter>);
    expect(await screen.findByRole("link", { name: "Planner quality" })).toHaveAttribute("href", "/evaluations/rubrics/planner-v1");
    expect(screen.getByText("CURRENT")).toBeInTheDocument();
    expect(screen.getByText(/2026/)).not.toHaveTextContent("2026-01-02T12:30:00Z");
  });

  it("shows rubric dimensions, thresholds, history and blocks an invalid weight sum", async () => {
    const rubric: EvaluationRubric = { rubric_id: "planner-v1", name: "Planner quality", version: "1.0", agent_name: "Planner", dimensions: { completeness: 1, feasibility: 1, decomposition_quality: 1, technical_consistency: 1 }, weights: { completeness: .3, feasibility: .25, decomposition_quality: .25, technical_consistency: .2 }, thresholds: { acceptable: .7 }, verdict_thresholds: { excellent: .9, good: .8, acceptable: .7, needs_improvement: .5, failed: 0 }, enabled: true, version_status: "current", usage_count: 4, evaluations_valid: 4, evaluations_total: 5, created_at: "2026-01-02T12:30:00Z", updated_at: null, history: [] };
    rubric.history = [rubric]; vi.mocked(getEvaluationRubric).mockResolvedValue(rubric);
    render(<MemoryRouter initialEntries={["/evaluations/rubrics/planner-v1"]}><EvaluationsPage /></MemoryRouter>);
    expect(await screen.findByText("Total 100%")).toBeInTheDocument();
    expect(screen.getByText("completeness")).toBeInTheDocument(); expect(screen.getByText("excellent")).toBeInTheDocument(); expect(screen.getByText("Version history")).toBeInTheDocument(); expect(screen.getByText("Valid evaluations")).toBeInTheDocument(); expect(screen.getByText("4")).toBeInTheDocument(); expect(screen.getByText("5")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Create new version" }));
    const input=screen.getByLabelText("completeness weight"); await userEvent.clear(input); await userEvent.type(input,"0.4");
    expect(screen.getByRole("alert")).toHaveTextContent("Weights must total 100%");
    expect(screen.getByRole("button", { name: "Create version" })).toBeDisabled();
    expect(createEvaluationRubricVersion).not.toHaveBeenCalled();
  });

  it("creates a new rubric version while retaining version history", async () => {
    const old = { rubric_id: "planner-v1", name: "Planner quality", version: "1.0", agent_name: "Planner", dimensions: { completeness: 1 }, weights: { completeness: 1 }, thresholds: {}, verdict_thresholds: { excellent: .9 }, enabled: false, version_status: "superseded" as const, usage_count: 2, evaluations_valid: 2, evaluations_total: 2, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-02-01T00:00:00Z" };
    const current = { ...old, rubric_id: "planner-v11", version: "1.1", enabled: true, version_status: "current" as const, usage_count: 0, evaluations_valid: 0, evaluations_total: 0, created_at: "2026-02-01T00:00:00Z", updated_at: null, history: [old] };
    vi.mocked(getEvaluationRubric).mockResolvedValue({ ...old, enabled: true, version_status: "current", history: [old] }); vi.mocked(createEvaluationRubricVersion).mockResolvedValue({ ...current, history: [current,old] });
    render(<MemoryRouter initialEntries={["/evaluations/rubrics/planner-v1"]}><EvaluationsPage /></MemoryRouter>);
    await screen.findByText("Planner quality"); await userEvent.click(screen.getByRole("button", { name: "Create new version" })); await userEvent.click(screen.getByRole("button", { name: "Create version" }));
    await waitFor(() => expect(createEvaluationRubricVersion).toHaveBeenCalledWith("planner-v1",expect.objectContaining({ version:"1.1",weights:{ completeness:1 } })));
    expect((await screen.findAllByText("1.1")).length).toBeGreaterThanOrEqual(1); expect(screen.getByRole("link", { name: "v1.0" })).toBeInTheDocument();
  });

  it("shows only safe actions for superseded rubric versions", async () => {
    const historical: EvaluationRubric = { rubric_id: "planner-v1", name: "Planner quality", version: "1.0", agent_name: "Planner", dimensions: { completeness: 1 }, weights: { completeness: 1 }, thresholds: {}, enabled: false, version_status: "superseded", usage_count: 9, evaluations_valid: 8, evaluations_total: 9, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-02-01T00:00:00Z", history: [] };
    vi.mocked(getEvaluationRubric).mockResolvedValue(historical);
    render(<MemoryRouter initialEntries={["/evaluations/rubrics/planner-v1"]}><EvaluationsPage /></MemoryRouter>);
    expect(await screen.findByText("SUPERSEDED")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create new version" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Edit/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disable" })).not.toBeInTheDocument();
  });

  it("offers disable only for the current enabled version", async () => {
    const current: EvaluationRubric = { rubric_id: "planner-v11", name: "Planner quality", version: "1.1", agent_name: "Planner", dimensions: { completeness: 1 }, weights: { completeness: 1 }, thresholds: {}, enabled: true, version_status: "current", usage_count: 0, evaluations_valid: 0, evaluations_total: 0, created_at: "2026-02-01T00:00:00Z", updated_at: null, history: [] };
    vi.mocked(getEvaluationRubric).mockResolvedValue(current);
    render(<MemoryRouter initialEntries={["/evaluations/rubrics/planner-v11"]}><EvaluationsPage /></MemoryRouter>);
    expect(await screen.findByText("CURRENT")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Disable" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Edit/ })).not.toBeInTheDocument();
  });

  it("renders structured baseline scopes, directions, human dates and typed values", async () => {
    vi.mocked(getEvaluationCollection).mockResolvedValue({ items: [
      { baseline_id: "b8d1df1d-a385-462b-b556-04cf84bf62f6", scope: { model: "gpt-5-mini", rubric_version: "1.0", workflow_id: "workflow-123", evaluation_type: "comprehensive" }, scope_key: "{raw-json-must-not-render}", metric: "overall_score", score: .958333, sample_count: 7, created_at: "2026-08-09T07:30:41.637897+00:00" },
      { baseline_id: "validation-baseline", scope: { validation: "phase-6.17-overall-score-regression" }, metric: "reliability_score", score: .9, sample_count: 8, created_at: "2026-08-09T07:30:41.637897+00:00" },
      { baseline_id: "duration-baseline", scope: {}, metric: "duration_ms", score: 1250, sample_count: 3, created_at: "2026-08-09T07:30:41.637897+00:00" },
      { baseline_id: "cost-baseline", scope: {}, metric: "evaluation_cost", score: .0125, sample_count: 2, created_at: "2026-08-09T07:30:41.637897+00:00" },
    ] });
    render(<MemoryRouter initialEntries={["/evaluations/baselines"]}><EvaluationsPage /></MemoryRouter>);
    expect(await screen.findByTitle("b8d1df1d-a385-462b-b556-04cf84bf62f6")).toHaveTextContent("b8d1df1d");
    for (const value of ["Workflow", "workflow-123", "Evaluation", "comprehensive", "Model", "gpt-5-mini", "Rubric", "1.0", "Validation", "phase-6.17-overall-score-regression"]) expect(screen.getByText(value)).toBeInTheDocument();
    expect(screen.queryByText("{raw-json-must-not-render}")).not.toBeInTheDocument();
    expect(screen.getAllByText("higher is better")).toHaveLength(2); expect(screen.getAllByText("lower is better")).toHaveLength(2);
    expect(screen.getByText("95.83%")).toBeInTheDocument(); expect(screen.getByText("1,250 ms")).toBeInTheDocument(); expect(screen.getByText("$0.0125")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument(); expect(screen.queryByText("700%")).not.toBeInTheDocument();
    expect(screen.getAllByText("ACTIVE")).toHaveLength(4);
    expect(screen.getAllByText(/2026/)[0]).not.toHaveTextContent("2026-08-09T07:30:41.637897+00:00");
  });

  it("renders regression metadata operationally without stringifying objects", async () => {
    vi.mocked(getEvaluationCollection).mockResolvedValue({ items: [
      { evidence_id: "regression-1", evaluation_run_id: "b09096d2-full-run-id", reference_id: "baseline-score-full-id", summary: "score_regression detected for overall_score", created_at: "2026-08-09T07:34:32.695105+00:00", metadata: { type: "score_regression", metric: "overall_score", baseline_value: 1, current_value: .942603, delta: .057397, threshold: .05, direction: "higher_is_better", baseline_id: "baseline-score-full-id" } },
      { evidence_id: "regression-2", evaluation_run_id: "duration-run-full-id", reference_id: "baseline-duration-full-id", summary: "latency_regression detected for duration_ms", created_at: "2026-08-09T08:00:00+00:00", metadata_json: JSON.stringify({ type: "latency_regression", metric: "duration_ms", baseline_value: 1000, current_value: 1250, delta: 250, threshold: 100, direction: "lower_is_better" }) },
      { evidence_id: "regression-3", evaluation_run_id: "cost-run-full-id", reference_id: "baseline-cost-full-id", summary: "cost_regression detected for evaluation_cost", created_at: "2026-08-09T08:30:00+00:00", metadata: { type: "cost_regression", metric: "evaluation_cost", baseline_value: .01, current_value: .02, delta: .01, threshold: .005, direction: "lower_is_better" } },
    ] });
    render(<MemoryRouter initialEntries={["/evaluations/regressions"]}><EvaluationsPage /></MemoryRouter>);
    expect(await screen.findByText("score_regression")).toBeInTheDocument();
    expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
    for (const value of ["100%", "94.26%", "5.74%", "5%", "1,000 ms", "1,250 ms", "250 ms", "$0.02", "$0.005"]) expect(screen.getByText(value)).toBeInTheDocument();
    expect(screen.getAllByText("$0.01")).toHaveLength(2);
    expect(screen.getByText("higher is better")).toBeInTheDocument(); expect(screen.getAllByText("lower is better")).toHaveLength(2);
    expect(screen.getByRole("link", { name: "b09096d2" })).toHaveAttribute("href", "/evaluations/runs/b09096d2-full-run-id");
    expect(screen.getByRole("link", { name: "b09096d2" })).toHaveAttribute("title", "b09096d2-full-run-id");
    expect(screen.getByTitle("baseline-score-full-id")).toHaveTextContent("baseline");
    expect(screen.getByRole("button", { name: "Copy baseline baseline-score-full-id" })).toBeInTheDocument();
    expect(screen.getAllByText("REGRESSION")).toHaveLength(3);
    expect(screen.getAllByText(/2026/)[0]).not.toHaveTextContent("2026-08-09T07:34:32.695105+00:00");
  });

  it("renders missing regression metadata as n/a and keeps duplicate summaries independent", async () => {
    vi.mocked(getEvaluationCollection).mockResolvedValue({ items: [
      { evidence_id: "missing-1", evaluation_run_id: "run-one", reference_id: null, summary: "score_regression detected for overall_score", created_at: "2026-08-09T07:34:32Z", metadata: {} },
      { evidence_id: "missing-2", evaluation_run_id: "run-two", reference_id: null, summary: "score_regression detected for overall_score", created_at: "2026-08-09T07:35:32Z", metadata: {} },
    ] });
    render(<MemoryRouter initialEntries={["/evaluations/regressions"]}><EvaluationsPage /></MemoryRouter>);
    expect(await screen.findAllByText("REGRESSION")).toHaveLength(2);
    expect(screen.getAllByText("n/a").length).toBeGreaterThanOrEqual(12);
    expect(screen.getByRole("link", { name: "run-one" })).toHaveAttribute("href", "/evaluations/runs/run-one");
    expect(screen.getByRole("link", { name: "run-two" })).toHaveAttribute("href", "/evaluations/runs/run-two");
    expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
  });

  it("renders planner recommendation governance and accepts with fingerprint", async () => {
    vi.mocked(getAgentRecommendations).mockResolvedValue({
      items: [{
        recommendation_id: "agent-rec-123", analytics_version: "8.5-v1", recommendation_fingerprint: "agent-fingerprint-123",
        policy: "agent:developer:improve_validation", agent: "developer", type: "improve_validation",
        summary: "Review Developer validation.", recommended_action: "Review Developer validation guidance.",
        segment: null, direction: "review", severity: "medium", confidence: .71,
        reason_codes: ["agent_validation_failures"], evidence: { sample_size: 24, root_cause_rate: .25 },
        current_value: "current_agent_configuration", suggested_value: { review: "validation" }, status: "recommendation_only",
        application_status: "not_applied", review: { status: "recommendation_only" }, trend: "stable",
      }],
      review_metrics: { recommendations_accepted: 0 },
    });
    vi.mocked(getPlannerRecommendations).mockResolvedValue({
      items: [{
        recommendation_id: "rec-1234567890", analytics_version: "7.8-v1", recommendation_fingerprint: "fingerprint-123",
        policy: "planning_risk_policy", segment: null, direction: "increase", severity: "high", confidence: .82,
        reason_codes: ["risk_underestimated"], evidence: { sample_size: 20, observed_rate: .35 },
        current_value: { high: 50 }, suggested_value: { review: "risk weights" }, status: "recommendation_only",
        application_status: "not_applicable", review: { status: "recommendation_only" },
      }],
      review_metrics: { recommendations_accepted: 0, recommendations_deferred: 0 },
    });
    vi.mocked(getPlannerPolicyProposals).mockResolvedValue({ items: [] });
    vi.mocked(getPlannerPolicyApplications).mockResolvedValue({ items: [] });
    vi.mocked(getPlannerPolicyRollouts).mockResolvedValue({ items: [] });
    vi.mocked(reviewPlannerRecommendation).mockResolvedValue({} as never);
    render(<MemoryRouter initialEntries={["/evaluations/recommendations"]}><EvaluationsPage /></MemoryRouter>);
    expect(await screen.findByText("Agent Recommendations")).toBeInTheDocument();
    expect(screen.getByText("developer")).toBeInTheDocument();
    expect(screen.getByText("improve validation")).toBeInTheDocument();
    expect(screen.getByText(/no automatic prompt, model, routing or retry changes/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Change Prompt|Change Model|Apply/i })).not.toBeInTheDocument();
    expect(await screen.findByText("planning_risk_policy")).toBeInTheDocument();
    expect(screen.getByText("risk_underestimated")).toBeInTheDocument();
    expect(screen.getByText("82%")).toBeInTheDocument();
    expect(screen.getByText(/sample size: 20/)).toBeInTheDocument();
    expect(screen.getAllByText("RECOMMENDATION_ONLY").length).toBeGreaterThanOrEqual(2);

    await userEvent.click(screen.getAllByRole("button", { name: "Accept" })[1]);

    await waitFor(() => expect(reviewPlannerRecommendation).toHaveBeenCalledWith("rec-1234567890","accept",expect.objectContaining({ fingerprint:"fingerprint-123", reviewer:"ui-reviewer" })));
  });

  it("reviews agent recommendations without creating apply actions", async () => {
    vi.mocked(getAgentRecommendations).mockResolvedValue({
      items: [{
        recommendation_id: "agent-rec-456", analytics_version: "8.5-v1", recommendation_fingerprint: "agent-fingerprint-456",
        policy: "agent:developer:improve_validation", agent: "developer", type: "improve_validation",
        summary: "Review Developer validation.", recommended_action: "Review Developer validation guidance.",
        segment: "framework:dotnet", direction: "review", severity: "high", confidence: .82,
        reason_codes: ["agent_low_first_pass_rate"], evidence: { sample_size: 20, first_pass_success_rate: .5 },
        current_value: "current_agent_configuration", suggested_value: { review: "validation" }, status: "recommendation_only",
        application_status: "not_applied", review: { status: "recommendation_only" }, trend: "degrading",
      }],
      review_metrics: { recommendations_accepted: 0 },
    });
    vi.mocked(getPlannerRecommendations).mockResolvedValue({ items: [], review_metrics: {} });
    vi.mocked(getPlannerPolicyProposals).mockResolvedValue({ items: [] });
    vi.mocked(getPlannerPolicyApplications).mockResolvedValue({ items: [] });
    vi.mocked(getPlannerPolicyRollouts).mockResolvedValue({ items: [] });
    vi.mocked(reviewAgentRecommendation).mockResolvedValue({} as never);
    render(<MemoryRouter initialEntries={["/evaluations/recommendations"]}><EvaluationsPage /></MemoryRouter>);

    expect(await screen.findByText("framework:dotnet")).toBeInTheDocument();
    expect(screen.getByText("degrading")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Apply/i })).not.toBeInTheDocument();

    const buttons = screen.getAllByRole("button", { name: "Accept" });
    await userEvent.click(buttons[0]);

    await waitFor(() => expect(reviewAgentRecommendation).toHaveBeenCalledWith("agent-rec-456","accept",expect.objectContaining({ fingerprint:"agent-fingerprint-456", reviewer:"ui-reviewer" })));
  });

  it("creates and governs policy proposals without offering apply", async () => {
    vi.mocked(getPlannerRecommendations).mockResolvedValue({
      items: [{
        recommendation_id: "rec-accepted", analytics_version: "7.8-v1", recommendation_fingerprint: "fingerprint-accepted",
        policy: "quality_gate_threshold", segment: null, direction: "decrease", severity: "medium", confidence: .76,
        reason_codes: ["quality_gate_false_positives_high"], evidence: { sample_size: 20 },
        current_value: { weak_threshold: 60 }, suggested_value: { suggested_adjustment: -5 }, status: "recommendation_only",
        application_status: "not_applied", review: { status: "accepted", reviewer: "qa-lead" },
      }],
      review_metrics: { recommendations_accepted: 1, recommendations_deferred: 0 },
    });
    vi.mocked(getPlannerPolicyProposals).mockResolvedValue({ items: [{
      proposal_id: "proposal-1", proposal_version: "7.10-v1", source_recommendation_id: "rec-accepted",
      source_recommendation_fingerprint: "fingerprint-accepted", policy_key: "planning.quality_gate.weak_threshold",
      policy_scope: "global_planner", segment: null, current_value: 60, proposed_value: 55,
      change_type: "decrease", status: "draft", application_status: "not_applied",
      proposal_fingerprint: "proposal-fingerprint", proposal_risk_level: "medium",
      affected_workflows_scope: "global_planner", simulation: { sample_size: 20, changed_decision_count: 4 },
      safety_flags: { reduces_safety: true }, created_at: "2026-08-13T00:00:00Z", updated_at: "2026-08-13T00:00:00Z",
    }] });
    vi.mocked(getPlannerPolicyApplications).mockResolvedValue({ items: [{
      application_id: "application-1", proposal_id: "proposal-1", proposal_fingerprint: "proposal-fingerprint",
      application_fingerprint: "application-fingerprint", policy_key: "planning.quality_gate.weak_threshold",
      scope: "global_planner", previous_value: 60, proposed_value: 55, status: "prepared",
      baseline_revision: 0, applied_revision: null, rollback_revision: null, rollback_supported: true,
      verification_strategy: "read_back_and_boundary_check", application_risk_level: "medium",
      created_at: "2026-08-13T00:00:00Z",
    }] });
    vi.mocked(getPlannerPolicyRollouts).mockResolvedValue({ items: [{
      rollout_id: "rollout-1", application_id: "application-1", proposal_id: "proposal-1",
      policy_key: "planning.quality_gate.weak_threshold", scope: "global_planner",
      baseline_revision: 0, target_revision: 1, previous_value: 60, target_value: 55,
      status: "prepared", current_percentage: 0, target_percentage: 10, strategy: "manual_staged",
      stages: [10,25,50,100], baseline_metrics: { policy_sample_size: 20 },
      treatment_metrics: { policy_sample_size: 10, failure_rate: 0 }, control_metrics: { failure_rate: 0 },
      delta_metrics: { repair_rate_delta: 0 }, health_status: "not_started", health_score: 0,
      rollout_fingerprint: "rollout-fingerprint", application_fingerprint: "application-fingerprint",
      version: "7.12-v1", created_at: "2026-08-13T00:00:00Z",
    }] });
    vi.mocked(createPlannerPolicyProposal).mockResolvedValue({} as never);
    vi.mocked(transitionPlannerPolicyProposal).mockResolvedValue({} as never);
    vi.mocked(preparePlannerPolicyApplication).mockResolvedValue({} as never);
    vi.mocked(applyPlannerPolicyApplication).mockResolvedValue({} as never);

    render(<MemoryRouter initialEntries={["/evaluations/recommendations"]}><EvaluationsPage /></MemoryRouter>);

    expect(await screen.findByRole("button", { name: "Create Policy Proposal" })).toBeInTheDocument();
    expect(screen.getAllByText("planning.quality_gate.weak_threshold").length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText("60 → 55").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("4 changed / 20 samples")).toBeInTheDocument();
    expect(screen.getAllByText("not_applied").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("Policy applications")).toBeInTheDocument();
    expect(screen.getByText("Policy Rollouts")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start" })).toBeInTheDocument();
    expect(screen.getByText("read_back_and_boundary_check")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Create Policy Proposal" }));
    await waitFor(() => expect(createPlannerPolicyProposal).toHaveBeenCalledWith("rec-accepted"));
    await userEvent.click(screen.getByRole("button", { name: "Ready for Review" }));
    await waitFor(() => expect(transitionPlannerPolicyProposal).toHaveBeenCalledWith("proposal-1","ready",expect.objectContaining({ proposal_fingerprint:"proposal-fingerprint" })));
    expect(screen.getByRole("button", { name: "Prepare Application" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(applyPlannerPolicyApplication).toHaveBeenCalledWith("application-1",expect.objectContaining({ application_fingerprint:"application-fingerprint" })));
  });

  it("shows rollback for applied applications and no proposal apply for review-only", async () => {
    vi.mocked(getPlannerRecommendations).mockResolvedValue({ items: [], review_metrics: {} });
    vi.mocked(getPlannerPolicyProposals).mockResolvedValue({ items: [{
      proposal_id: "proposal-review", proposal_version: "7.10-v1", source_recommendation_id: "rec-review",
      source_recommendation_fingerprint: "fingerprint-review", policy_key: "planning.refinement.strategy",
      policy_scope: "global_planner", segment: null, current_value: "current_guidance", proposed_value: null,
      change_type: "review_only", status: "approved", application_status: "not_applicable",
      proposal_fingerprint: "proposal-review-fingerprint", proposal_risk_level: "medium",
      affected_workflows_scope: "global_planner", simulation: null, safety_flags: {},
      created_at: "2026-08-13T00:00:00Z", updated_at: "2026-08-13T00:00:00Z",
    }] });
    vi.mocked(getPlannerPolicyApplications).mockResolvedValue({ items: [{
      application_id: "application-applied", proposal_id: "proposal-1", proposal_fingerprint: "proposal-fingerprint",
      application_fingerprint: "application-fingerprint", policy_key: "planning.quality_gate.weak_threshold",
      scope: "global_planner", previous_value: 60, proposed_value: 55, status: "applied",
      baseline_revision: 0, applied_revision: 1, rollback_revision: null, rollback_supported: true,
      verification_strategy: "read_back_and_boundary_check", application_risk_level: "medium",
      created_at: "2026-08-13T00:00:00Z",
    }] });
    vi.mocked(getPlannerPolicyRollouts).mockResolvedValue({ items: [] });
    vi.mocked(rollbackPlannerPolicyApplication).mockResolvedValue({} as never);
    vi.mocked(preparePlannerPolicyRollout).mockResolvedValue({} as never);
    vi.mocked(rollbackPlannerPolicyRollout).mockResolvedValue({} as never);

    render(<MemoryRouter initialEntries={["/evaluations/recommendations"]}><EvaluationsPage /></MemoryRouter>);

    expect(await screen.findByText("review only")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Prepare Application" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Rollback" }));
    await waitFor(() => expect(rollbackPlannerPolicyApplication).toHaveBeenCalledWith("application-applied",expect.objectContaining({ application_fingerprint:"application-fingerprint" })));
    await userEvent.click(screen.getByRole("button", { name: "Prepare Rollout" }));
    await waitFor(() => expect(preparePlannerPolicyRollout).toHaveBeenCalledWith("application-applied",expect.objectContaining({ actor:"ui-reviewer" })));
  });

  it("renders policy experiments with variants and manual actions only", async () => {
    vi.mocked(getPlannerPolicyExperiments).mockResolvedValue({ items: [{
      experiment_id: "experiment-1", experiment_version: "7.13-v1",
      policy_key: "planning.quality_gate.weak_threshold", scope: "global_planner",
      baseline_revision: 0, control_value: 60,
      variants: [{ variant_id: "A", name: "Variant A", value: 55 }, { variant_id: "B", name: "Variant B", value: 50 }],
      allocation: { control: 50, A: 25, B: 25 }, status: "ready", minimum_sample_size: 20,
      observation_window: {}, primary_metric: "successful_with_repair_rate", secondary_metrics: [],
      guardrails: {}, metrics: { control: { repair_rate: .4 }, variants: { A: { repair_rate: .1 }, B: { repair_rate: .4 } } },
      statistical_summary: { decision_confidence: .86, decision_confidence_label: "high", comparisons: { A: { metric: "successful_with_repair_rate", adjusted_delta_ci: [.12,.48], minimum_detectable_effect: .05, adjusted_significant: true, practically_significant: true, decision_confidence: .86, decision_confidence_label: "high" } } },
      promotion_readiness: { status: "ready", score: 88, confidence: .91, winner_variant_id: "A", recommended_action: "recommend_promotion", checks: { sample_sufficient: true, statistical_significance: true, guardrails_clear: true, secondary_metrics_consistent: true, temporal_stability_ok: true, policy_baseline_current: true }, secondary_metric_consistency: { status: "neutral" }, temporal_stability: { status: "stable" }, policy_risk: { risk_level: "medium" } },
      result: "variant_preferred", winner_variant_id: "A", experiment_fingerprint: "experiment-fingerprint",
      created_at: "2026-08-13T00:00:00Z",
    }] });
    vi.mocked(getPlannerPolicyExperimentPortfolio).mockResolvedValue({
      version: "7.16-v1",
      active_experiments: [{ kind: "experiment", id: "experiment-1", policy_key: "planning.quality_gate.weak_threshold", scope: "global_planner", status: "ready", priority: "normal" }],
      active_rollouts: [],
      conflicts: [],
      warnings: [{ type: "metric_interference", severity: "warning" }],
      blocking_conflict_count: 0,
      warning_count: 1,
      isolation_status: "warnings",
    });
    vi.mocked(startPlannerPolicyExperiment).mockResolvedValue({} as never);
    vi.mocked(evaluatePlannerPolicyExperiment).mockResolvedValue({} as never);

    render(<MemoryRouter initialEntries={["/evaluations/experiments"]}><EvaluationsPage /></MemoryRouter>);

    expect(await screen.findByText("Policy Experiments")).toBeInTheDocument();
    expect(screen.getByText("Active Experiments")).toBeInTheDocument();
    expect(screen.getByText("WARNINGS")).toBeInTheDocument();
    expect(screen.getByText("Warnings")).toBeInTheDocument();
    expect(screen.getByText("Variant A: 55")).toBeInTheDocument();
    expect(screen.getByText("control 50%")).toBeInTheDocument();
    expect(screen.getByText("VARIANT_PREFERRED")).toBeInTheDocument();
    expect(screen.getByText("winner A")).toBeInTheDocument();
    expect(screen.getByText(/confidence high 86%/i)).toBeInTheDocument();
    expect(screen.getByText(/statistical yes/i)).toBeInTheDocument();
    expect(screen.getByText(/MDE 5%/i)).toBeInTheDocument();
    expect(screen.getAllByText("READY").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("score 88")).toBeInTheDocument();
    expect(screen.getByText("recommend_promotion")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Apply Winner/i })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(startPlannerPolicyExperiment).toHaveBeenCalledWith("experiment-1",expect.objectContaining({ actor:"ui-reviewer" })));
  });
});
