import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CostSourceBadge, LlmCostsPage } from "./LlmCostsPage";
import { CostStateBadge } from "../components/FinOps/FinOpsVisuals";

const api = vi.hoisted(() => ({
  getCostSummary: vi.fn(), getTimeseries: vi.fn(), getCalls: vi.fn(), getCall: vi.fn(),
  getWorkflowCosts: vi.fn(), getCostCollection: vi.fn(), getPricing: vi.fn(),
  createPricing: vi.fn(), disablePricing: vi.fn(), getBudgets: vi.fn(),
  createBudget: vi.fn(), disableBudget: vi.fn(), getBudget: vi.fn(),
  getBudgetUsage: vi.fn(), getBudgetEvents: vi.fn(), getBudgetReservations: vi.fn(),
  updateBudget: vi.fn(), resetBudget: vi.fn(),
}));
vi.mock("../api/llmCosts", () => api);
vi.mock("../hooks/useAlertSummary", () => ({ useAlertSummary: () => ({ summary: null }) }));
vi.mock("../hooks/useNotificationSummary", () => ({ useNotificationSummary: () => ({ summary: null }) }));

const summary = {
  state: "partial", currency: "USD", calls: 17, calls_with_usage: 5,
  calls_without_usage: 12, calls_with_cost: 2, calls_without_pricing: 3,
  real_cost: "0.005373", estimated_cost: "0", input_cost: "0.001000",
  cached_input_cost: "0", output_cost: "0.004373", reasoning_cost: "0",
  cache_savings: "0", retry_cost: "0", failed_call_cost: "0.000100",
  total_tokens: 12158, cached_tokens: 0, budget_status: "within_budget",
  budget_limit: "1", budget_consumed: "0.005373", budget_reserved: "0.001",
  budget_remaining: "0.993627", average_cost_per_call: "0.0026865",
  average_cost_per_completed_workflow: "0.005373", active_budgets: 2,
  global_budget_applicable: true, blocked_calls: 3, completed_workflows: 1,
  p50: "0.001250", p90: "0.0028015", p95: "0.0028015", calculated_at: "2026-08-06T10:00:00Z",
};
const call = {
  call_id: "call-1", trace_id: "trace-1", span_id: "span-1", workflow_id: "workflow-1",
  branch_id: "original", agent: "Planner", node: "plan", subgraph: "planning",
  provider: "openai", model: "gpt-5-mini", operation: "parse", status: "completed",
  timestamp: "2026-08-06T10:00:00Z", duration_ms: 50, input_tokens: 974,
  output_tokens: 1279, total_tokens: 2253, cached_tokens: 0, reasoning_tokens: 100,
  usage_source: "provider_reported", usage_available: true, cost_source: "calculated",
  cost_status: "calculated", total_cost: "0.002801500000", estimated_total_cost: null,
  cache_savings: "0", retry_attempt: 0, warnings: [],
};
const budget = { budget_id: "budget-1", name: "Manual concurrency validation", description: "Validation", scope_type: "global", scope_value: null, currency: "USD", limit_amount: "0.0015", warning_percent: "80", enforcement_mode: "hard_limit", enabled: true, period_type: "custom", period_start: "2026-08-01T00:00:00Z", period_end: "2026-08-31T00:00:00Z", reset_timezone: "UTC", include_estimated: false, include_failed_calls: true, include_retries: true };
const usage = { budget, consumed: "0", reserved: "0", remaining: "0.0015", percent: "0" };
const official = { pricing_id: "price-1", provider: "openai", model_pattern: "gpt-5-mini", model_canonical_name: null, currency: "USD", input_price_per_million: "0.25", cached_input_price_per_million: "0.025", output_price_per_million: "2.00", reasoning_price_per_million: null, effective_from: "2026-08-01T00:00:00Z", effective_to: null, source_type: "official", source_reference: "OpenAI pricing", source_verified_at: "2026-08-01T00:00:00Z", reasoning_in_completion: true, enabled: true, priority: 10 };
const fixture = { ...official, pricing_id: "8cc33a1326f74148b91e9ddbbbc2f80b", input_price_per_million: "0.50", cached_input_price_per_million: "0.05", output_price_per_million: "4.00", effective_from: "2099-08-08T00:00:00Z", source_type: "test_fixture", source_reference: "fixture" };
const aggregate = { agent: "Planner", model: "gpt-5-mini", operation: "parse", calls: 2, tokens: 2253, calls_with_usage: 1, calls_without_usage: 1, calls_with_pricing: 1, calls_without_pricing: 0, real_cost: "0.0028015", estimated_cost: "0", failed_call_cost: "0", failures: 0, retries: 0, average_cost_per_call: "0.00140075", percentage_of_total_cost: "100" };

function renderPage(path: string) { return render(<MemoryRouter initialEntries={[path]}><LlmCostsPage /></MemoryRouter>); }

beforeEach(() => {
  vi.clearAllMocks(); vi.spyOn(window, "confirm").mockReturnValue(true);
  api.getCostSummary.mockResolvedValue(summary);
  api.getTimeseries.mockResolvedValue({ items: [{ period: "2026-08-06", calls: 2, tokens: 2253, real_cost: "0.0028015", estimated_cost: "0" }] });
  api.getCalls.mockResolvedValue({ items: [call], total: 1 });
  api.getCall.mockResolvedValue({ call, calculation: { ...call, cost_source: "calculated", total_cost: "0.002801500000", input_cost: "0.0002435", cached_input_cost: "0", output_cost: "0.002558", reasoning_cost: null, other_cost: "0", pricing_id: "price-1", calculation_version: "1.0", calculated_at: "2026-08-06T10:01:00Z", warnings: [], pricing_snapshot: official }, budget_events: [{ budget_event_id: "event-1", budget_id: "budget-1", event_type: "budget_checked", decision: "allow", reason_code: "within_budget", created_at: "2026-08-06T10:00:00Z" }] });
  api.getWorkflowCosts.mockResolvedValue({ summary, items: [call] });
  api.getCostCollection.mockImplementation((name: string) => Promise.resolve({ items: ["agents", "models", "operations"].includes(name) ? [aggregate] : [call] }));
  api.getPricing.mockResolvedValue({ items: [official, fixture] });
  api.getBudgets.mockResolvedValue({ items: [budget] });
  api.getBudget.mockResolvedValue(budget); api.getBudgetUsage.mockResolvedValue(usage);
  api.getBudgetEvents.mockResolvedValue({ items: [{ budget_event_id: "event-1", event_type: "budget_call_blocked", decision: "block", amount: "0.001", remaining_amount: "0.0005", workflow_id: "workflow-1", reason_code: "hard_limit", created_at: "2026-08-06T10:00:00Z" }] });
  api.getBudgetReservations.mockResolvedValue({ items: [{ reservation_id: "reservation-1", workflow_id: "workflow-1", branch_id: "original", llm_call_id: "call-1", agent_name: "Planner", estimated_amount: "0.001", currency: "USD", status: "released", created_at: "2026-08-06T10:00:00Z", expires_at: "2026-08-06T10:05:00Z", released_at: "2026-08-06T10:01:00Z", consumed_amount: null }] });
});

describe("LlmCostsPage", () => {
  it("renders advanced KPIs and all chart groups", async () => {
    renderPage("/llm-costs");
    expect((await screen.findAllByText("USD 0.005373")).length).toBeGreaterThan(0);
    expect(screen.getByText("12,158")).toBeInTheDocument();
    expect(screen.getByText("Budget consumido")).toBeInTheDocument();
    expect(screen.getByText("Budget reservado")).toBeInTheDocument();
    expect(screen.getByText("Budget disponible")).toBeInTheDocument();
    for (const title of ["Coste por día", "Tokens por día", "Coste por agente", "Coste por modelo", "Coste por operación", "Input vs output", "Real vs estimado", "Coste de retries", "Ahorro de caché"]) expect(screen.getByRole("region", { name: title })).toBeInTheDocument();
  });

  it("preserves dashboard filters from the URL", async () => {
    renderPage("/llm-costs?provider=openai&model=gpt-5-mini&workflow_id=workflow-1");
    await waitFor(() => expect(api.getCostSummary).toHaveBeenCalledWith(expect.objectContaining({ provider: "openai", model: "gpt-5-mini", workflow_id: "workflow-1" })));
    expect(api.getTimeseries).toHaveBeenCalledWith(expect.objectContaining({ provider: "openai", model: "gpt-5-mini", workflow_id: "workflow-1" }));
    expect(api.getCostCollection).toHaveBeenCalledWith("agents", expect.objectContaining({ provider: "openai", model: "gpt-5-mini", workflow_id: "workflow-1" }));
  });

  it.each([["real", "REAL"], ["estimated", "ESTIMADO"], ["no-pricing", "SIN PRICING"], ["no-usage", "SIN USAGE"], ["invalid", "INVÁLIDO"]] as const)("renders canonical %s state", (state, label) => {
    render(<CostStateBadge state={state} />); expect(screen.getByText(label)).toBeInTheDocument();
  });

  it("keeps the legacy badge export friendly", () => { render(<CostSourceBadge source="calculated" />); expect(screen.getByText("REAL")).toBeInTheDocument(); });

  it("lists calls with friendly usage and extended columns", async () => {
    renderPage("/llm-costs/calls"); expect(await screen.findByText("Planner")).toBeInTheDocument(); expect(screen.getByRole("columnheader", { name: "Proveedor / modelo" })).toBeInTheDocument(); expect(screen.getByTitle("provider_reported")).toHaveTextContent("Proveedor"); expect(screen.getByText("974")).toBeInTheDocument(); expect(screen.getByText("50.0 ms")).toBeInTheDocument();
  });

  it("normalizes durable JSON warnings returned by SQLite", async () => {
    api.getCalls.mockResolvedValueOnce({ items: [{ ...call, warnings: '["pricing_not_found"]', cost_source: "unavailable" }], total: 1 });
    renderPage("/llm-costs/calls");
    expect(await screen.findByText("SIN PRICING")).toBeInTheDocument();
  });

  it("separates unpriced and unavailable usage views", async () => {
    const first = renderPage("/llm-costs/unpriced"); expect(await screen.findByText("Ver pricing")).toBeInTheDocument(); expect(screen.getByText("pricing_not_found")).toBeInTheDocument(); first.unmount(); renderPage("/llm-costs/unavailable-usage"); await waitFor(() => expect(api.getCostCollection).toHaveBeenCalledWith("unavailable-usage")); expect(screen.getByText("usage_unavailable")).toBeInTheDocument();
  });

  it("renders a clear empty state", async () => {
    api.getCalls.mockResolvedValueOnce({ items: [], total: 0 });
    renderPage("/llm-costs/calls");
    expect(await screen.findByRole("heading", { name: "No hay llamadas LLM." })).toBeInTheDocument();
  });

  it("keeps common filters when navigating between FinOps tabs", async () => {
    renderPage("/llm-costs?provider=openai&workflow_id=workflow-1");
    const callsLink = await screen.findByRole("link", { name: "Llamadas" });
    expect(callsLink).toHaveAttribute("href", "/llm-costs/calls?provider=openai&workflow_id=workflow-1");
  });

  it("shows full call detail and pricing snapshot", async () => {
    renderPage("/llm-costs/calls/call-1"); expect(await screen.findByText("openai/gpt-5-mini")).toBeInTheDocument(); expect(screen.getByText("Desglose de coste")).toBeInTheDocument(); expect(screen.getByText("Snapshot de pricing")).toBeInTheDocument(); expect(screen.getByText("OFFICIAL")).toBeInTheDocument(); expect(screen.getByText("price-1")).toBeInTheDocument();
  });

  it("distinguishes official and test fixture pricing", async () => {
    renderPage("/llm-costs/pricing"); expect((await screen.findAllByText("OFFICIAL")).length).toBeGreaterThan(0); expect(screen.getByText("TEST FIXTURE")).toBeInTheDocument(); expect(screen.getByText("FUTURE")).toBeInTheDocument();
  });

  it("renders expanded agents and models tables", async () => {
    const agents = renderPage("/llm-costs/agents"); expect(await screen.findByText("% total")).toBeInTheDocument(); agents.unmount(); renderPage("/llm-costs/models"); expect((await screen.findAllByText("Sin usage")).length).toBeGreaterThan(0); expect(screen.getByRole("columnheader", { name: "Sin pricing" })).toBeInTheDocument();
  });

  it("renders budget usage, progress, events and reservations", async () => {
    renderPage("/llm-costs/budgets/budget-1"); expect(await screen.findByRole("heading", { name: "Manual concurrency validation" })).toBeInTheDocument(); expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "0"); expect(screen.getByText("Eventos recientes")).toBeInTheDocument(); expect(screen.getByText("Reservas")).toBeInTheDocument(); expect(screen.getByText("RELEASED")).toBeInTheDocument();
  });

  it("renders the budget list with consumed, reserved and remaining", async () => {
    renderPage("/llm-costs/budgets"); expect((await screen.findAllByText("USD 0.001500")).length).toBe(2); expect(screen.getByText("HARD LIMIT")).toBeInTheDocument(); expect(screen.getByText("HEALTHY")).toBeInTheDocument();
  });

  it("can reactivate a disabled budget", async () => {
    const disabled = { ...budget, enabled: false };
    api.getBudgets.mockResolvedValueOnce({ items: [disabled] });
    api.getBudgetUsage.mockResolvedValueOnce({ ...usage, budget: disabled });
    api.updateBudget.mockResolvedValueOnce({ ...budget, enabled: true });
    renderPage("/llm-costs/budgets");
    await userEvent.click(await screen.findByRole("button", { name: "Activar Manual concurrency validation" }));
    expect(api.updateBudget).toHaveBeenCalledWith("budget-1", { enabled: true });
  });

  it.each(["OPENAI_API_KEY", "Authorization: Bearer private", "full prompt secret", "C:\\Users\\secret", "system prompt"]) ("never renders sensitive value %s", async (value) => {
    renderPage("/llm-costs/calls/call-1"); await screen.findByText("openai/gpt-5-mini"); expect(screen.queryByText(value)).not.toBeInTheDocument();
  });
});
