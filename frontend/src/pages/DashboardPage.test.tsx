import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DashboardPage } from "./DashboardPage";

const api = vi.hoisted(() => ({
  summary: vi.fn(), timeseries: vi.fn(), agents: vi.fn(), attention: vi.fn(), activity: vi.fn(),
}));
vi.mock("../api/dashboard", () => ({
  getDashboardSummary: api.summary, getDashboardTimeSeries: api.timeseries,
  getDashboardAgents: api.agents, getDashboardAttention: api.attention,
  getDashboardActivity: api.activity,
}));

const summary = {
  date_from: "2026-07-04T00:00:00Z", date_to: "2026-08-03T23:59:59Z", timezone: "UTC", branch_scope: "original",
  workflow_counts: { total: 4, completed: 2, failed: 0, running: 1, waiting: 1, pending: 0, cancelled: 0, success_rate_percent: 100, failure_rate_percent: 0 },
  scores: { evaluated_workflows: 3, unevaluated_workflows: 1, average_score: 86, median_score: 88, min_score: 70, max_score: 98, excellent: 1, good: 2, acceptable: 0, poor: 0, critical: 0, provisional: 1, scoring_versions: ["1.2"] },
  durations: { workflows_with_duration: 3, discarded_workflows: 0, average_wall_clock_seconds: 180, median_wall_clock_seconds: 120, p50_wall_clock_seconds: 120, p90_wall_clock_seconds: 300, p95_wall_clock_seconds: 340, average_active_seconds: 90, average_approval_wait_seconds: 20, approval_wait_percent: 10, average_planning_seconds: 4, average_implementation_seconds: 60, average_testing_seconds: 20, average_repair_seconds: null },
  testing: { executed: 3, passed: 2, failed: 1, not_executed: 1, pass_rate_percent: 66.67, workflows_with_warnings: 2, total_warnings: 4, average_warnings: 2, repair_required: 1, repair_successful: 1, repair_failed: 0, repair_success_rate_percent: 100, average_repair_attempts: 1 },
  approvals: { total_approvals_requested: 4, total_approvals_granted: 3, total_approvals_rejected: 1, workflows_with_pending_approval: 1, average_approval_wait_seconds: 12, longest_approval_wait_seconds: 20, most_requested_operations: [] },
  frameworks: [{ key: "FastAPI", label: "FastAPI", count: 3, percentage: 100 }], intents: [], top_findings: [{ key: "TESTS_FAILED", label: "Tests failed", count: 1, percentage: 100 }], top_recommendations: [],
  calculated_at: "2026-08-03T12:00:00Z", source_updated_at: "2026-08-03T11:59:00Z", data_complete: true,
};

beforeEach(() => {
  api.summary.mockResolvedValue(summary);
  api.timeseries.mockResolvedValue({ metric: "workflow_count", interval: "day", timezone: "UTC", points: [{ bucket_start: "2026-08-01T00:00:00Z", bucket_end: "2026-08-02T00:00:00Z", value: 2, count: 2, numerator: null, denominator: null }], source_updated_at: null });
  api.agents.mockResolvedValue({ items: [{ agent: "qa reviewer", total_tasks: 4, completed_tasks: 3, failed_tasks: 1, waiting_tasks: 0, skipped_tasks: 0, completion_rate_percent: 75, average_attempts: 1.25, average_duration_seconds: 40, related_workflows: 4, related_files: 3, findings_count: 1, average_score: 85 }], total: 1, limit: 8, offset: 0, has_more: false });
  api.attention.mockResolvedValue({ items: [{ thread_id: "thread-1", branch_id: "original", project_name: "medical-api", terminal_status: "tests_failed", score: 60, grade: "poor", severity: "error", reasons: ["workflow_failed"], finding_codes: ["TESTS_FAILED"], pending_operation: "run_tests", age_seconds: 30, updated_at: null }], total: 1, limit: 8, offset: 0, has_more: false });
  api.activity.mockResolvedValue({ items: [{ event_id: "event-1", thread_id: "thread-1", branch_id: "original", project_name: "medical-api", type: "workflow_completed", status: "completed", message: "Workflow completado", timestamp: "2026-08-03T12:00:00Z", related_event_id: null }], total: 1, limit: 12, offset: 0, has_more: false });
});
afterEach(() => vi.clearAllMocks());

function renderDashboard(path = "/dashboard") {
  return render(<MemoryRouter initialEntries={[path]}><DashboardPage /></MemoryRouter>);
}

describe("DashboardPage", () => {
  it("loads all dashboard endpoints and renders KPIs", async () => {
    renderDashboard();
    expect(await screen.findByText("86/100")).toBeInTheDocument();
    expect(screen.getByText("86/100")).toBeInTheDocument();
    expect(api.summary).toHaveBeenCalledTimes(1);
    expect(api.timeseries).toHaveBeenCalledTimes(1);
    expect(api.agents).toHaveBeenCalledTimes(1);
    expect(api.attention).toHaveBeenCalledTimes(1);
    expect(api.activity).toHaveBeenCalledTimes(1);
  });

  it("renders chart, agents, attention, findings and activity", async () => {
    renderDashboard();
    expect(await screen.findByRole("img", { name: /Serie workflow_count/ })).toBeInTheDocument();
    expect(screen.getByText("qa reviewer")).toBeInTheDocument();
    expect(screen.getByText("medical-api")).toBeInTheDocument();
    expect(screen.getByText("Tests failed")).toBeInTheDocument();
    expect(screen.getByText("Workflow completado")).toBeInTheDocument();
    expect(screen.getByText("error: workflow_failed · run_tests")).toBeInTheDocument();
    expect(screen.getByText("75%")).toBeInTheDocument();
    const formatter = new Intl.DateTimeFormat("es-PE", { dateStyle: "short", timeStyle: "short", timeZone: "UTC" });
    const expectedTitle = `${formatter.format(new Date("2026-08-01T00:00:00Z"))} - ${formatter.format(new Date("2026-08-02T00:00:00Z"))}: 2`;
    const slot = screen.getByRole("img", { name: /Serie workflow_count/ }).querySelector(".dashboard-bar-slot");
    expect(slot).toHaveAttribute("data-bucket-start", "2026-08-01T00:00:00Z");
    expect(slot).toHaveAttribute("data-bucket-end", "2026-08-02T00:00:00Z");
    expect(slot?.getAttribute("title")).toBe(expectedTitle);
  });

  it("restores filters from the URL", async () => {
    renderDashboard("/dashboard?status=failed&project=medical&branches=all");
    await screen.findByText("86/100");
    expect(screen.getByLabelText("Estado")).toHaveValue("failed");
    expect(screen.getByLabelText("Proyecto")).toHaveValue("medical");
    expect(screen.getByLabelText("Ramas")).toHaveValue("all");
    expect(api.summary.mock.calls[0][0]).toMatchObject({ status: "failed", projectName: "medical", branchScope: "all" });
  });

  it("applies filters and reloads", async () => {
    const user = userEvent.setup(); renderDashboard();
    await screen.findByText("86/100");
    await user.selectOptions(screen.getByLabelText("Estado"), "completed");
    await waitFor(() => expect(api.summary.mock.calls.at(-1)?.[0]).toMatchObject({ status: "completed" }));
  });

  it("shows an empty state", async () => {
    api.summary.mockResolvedValue({ ...summary, workflow_counts: { ...summary.workflow_counts, total: 0 } });
    renderDashboard();
    expect(await screen.findByText("Sin datos para estos filtros")).toBeInTheDocument();
  });

  it("shows an error and can retry", async () => {
    api.summary.mockRejectedValueOnce(new Error("API unavailable"));
    const user = userEvent.setup(); renderDashboard();
    expect(await screen.findByRole("alert")).toHaveTextContent("API unavailable");
    await user.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByText("86/100")).toBeInTheDocument();
  });

  it("uses a 30 day default and original branches", async () => {
    renderDashboard(); await screen.findByText("86/100");
    const sent = api.summary.mock.calls[0][0];
    expect(sent.branchScope).toBe("original"); expect(sent.scoringVersion).toBe("latest");
    expect(new Date(sent.dateTo).getTime() - new Date(sent.dateFrom).getTime()).toBeGreaterThan(29 * 86_400_000);
  });

  it("offers every canonical timeseries metric", async () => {
    renderDashboard(); await screen.findByText("86/100");
    expect(screen.getByLabelText("Métrica de evolución").querySelectorAll("option")).toHaveLength(12);
  });

  it("renders null aggregate values as n/a", async () => {
    api.summary.mockResolvedValue({ ...summary, scores: { ...summary.scores, average_score: null }, testing: { ...summary.testing, pass_rate_percent: null }, durations: { ...summary.durations, p50_wall_clock_seconds: null, p90_wall_clock_seconds: null, p95_wall_clock_seconds: null } });
    renderDashboard();
    expect((await screen.findAllByText("n/a")).length).toBeGreaterThanOrEqual(3);
  });
});
