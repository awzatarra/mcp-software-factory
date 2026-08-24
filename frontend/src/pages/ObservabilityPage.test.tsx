import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ObservabilityPage } from "./ObservabilityPage";

const api = vi.hoisted(() => ({
  getObservabilitySummary: vi.fn(), getTraces: vi.fn(), getTrace: vi.fn(),
  getObservabilityCollection: vi.fn(),
}));
vi.mock("../api/observability", () => api);
vi.mock("../hooks/useAlertSummary", () => ({ useAlertSummary: () => ({ summary: null }) }));
vi.mock("../hooks/useNotificationSummary", () => ({ useNotificationSummary: () => ({ summary: null }) }));

const trace = {
  trace_id: "a".repeat(32), workflow_id: "medical-booking", branch_id: "original",
  name: "workflow", status: "completed", source: "live", started_at: "2026-08-05T10:00:00Z",
  ended_at: "2026-08-05T10:00:01Z", duration_ms: 1000, attributes: {},
};
const span = {
  span_id: "b".repeat(16), trace_id: trace.trace_id, parent_span_id: null,
  name: "agent.Planner", category: "agent", kind: "internal", status: "completed",
  agent: "Planner", operation: "plan", started_at: trace.started_at, ended_at: trace.ended_at,
  duration_ms: 800, is_slow: true, error_message: null, attributes: {},
};

function renderPage(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><ObservabilityPage /></MemoryRouter>);
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getObservabilitySummary.mockResolvedValue({ traces: 5, completed: 4, failed: 1, spans: 20, slow_spans: 2, errors: 1, average_duration_ms: 1250, calculated_at: trace.started_at });
  api.getTraces.mockResolvedValue({ items: [trace], total: 1 });
  api.getTrace.mockResolvedValue({ ...trace, spans: [span], events: [], logs: [{ log_id: 1, timestamp: trace.started_at, message: "Workflow completado" }], artifacts: [{ artifact_id: "art-1", artifact_type: "file", relative_path: "medical_booking/main.py" }], hierarchy_validation: { valid: true, root_count: 1, orphan_count: 0, cycle_count: 0, max_depth: 5, terminal_active_spans: 0, relationship_violation_count: 0, precision: "full", hierarchy_degraded: false } });
  api.getObservabilityCollection.mockImplementation((name: string) => Promise.resolve({ items: name === "slow-spans" ? [span] : [] }));
});

describe("ObservabilityPage", () => {
  it("shows durable summary metrics", async () => {
    renderPage("/observability");
    expect(await screen.findByText("5")).toBeInTheDocument();
    expect(screen.getByText("1.25 s")).toBeInTheDocument();
  });

  it.each(["Resumen", "Trazas", "Errores", "Agentes", "Tools", "LLM", "Spans lentos"])("shows the %s navigation tab", async (label) => {
    renderPage("/observability");
    expect(await screen.findByRole("link", { name: label })).toBeInTheDocument();
  });

  it("lists traces with workflow and status", async () => {
    renderPage("/observability/traces?status=completed");
    expect(await screen.findByText("medical-booking")).toBeInTheDocument();
    expect(screen.getAllByText("completed")).toHaveLength(2);
    expect(api.getTraces).toHaveBeenCalledWith(expect.objectContaining({ status: "completed" }), expect.any(AbortSignal));
  });

  it("renders an empty trace result safely", async () => {
    api.getTraces.mockResolvedValue({ items: [], total: 0 });
    renderPage("/observability/traces");
    expect(await screen.findByText("No hay trazas para estos filtros.")).toBeInTheDocument();
  });

  it.each(["errors", "agents", "tools", "llm", "slow-spans"])("loads the %s collection", async (name) => {
    renderPage(`/observability/${name}`);
    await waitFor(() => expect(api.getObservabilityCollection).toHaveBeenCalledWith(name));
  });

  it("renders trace waterfall, logs and artifacts", async () => {
    renderPage(`/observability/traces/${trace.trace_id}`);
    expect(await screen.findByText("Waterfall")).toBeInTheDocument();
    expect(screen.getByText("agent.Planner")).toBeInTheDocument();
    expect(screen.getByText("Workflow completado")).toBeInTheDocument();
    expect(screen.getByText("medical_booking/main.py")).toBeInTheDocument();
  });

  it("does not display artifact content", async () => {
    renderPage(`/observability/traces/${trace.trace_id}`);
    await screen.findByText("Artefactos");
    expect(screen.queryByText("file body secret")).not.toBeInTheDocument();
  });

  it("warns when the trace hierarchy is degraded", async () => {
    api.getTrace.mockResolvedValue({ ...trace, spans: [span], events: [], logs: [], artifacts: [], hierarchy_validation: { valid: false, root_count: 1, orphan_count: 2, cycle_count: 1, max_depth: 2, terminal_active_spans: 3, relationship_violation_count: 1, precision: "partial", hierarchy_degraded: true } });
    renderPage(`/observability/traces/${trace.trace_id}`);
    const warning = await screen.findByRole("alert");
    expect(warning).toHaveTextContent("2 spans huérfanos");
    expect(warning).toHaveTextContent("3 spans activos");
    expect(warning).toHaveTextContent("jerarquía degradada");
  });

  it("shows API errors without retaining a spinner", async () => {
    api.getTraces.mockRejectedValue(new Error("API unavailable"));
    renderPage("/observability/traces");
    expect(await screen.findByRole("alert")).toHaveTextContent("API unavailable");
    expect(screen.queryByText("Cargando telemetría...")).not.toBeInTheDocument();
  });

  it.each(["Trazas", "Spans", "Errores", "Duración media"])("labels the %s KPI", async (label) => {
    renderPage("/observability");
    expect(await screen.findByText(label)).toBeInTheDocument();
  });

  it.each(["running", "completed", "failed", "interrupted"])("renders the %s trace status", async (status) => {
    api.getTraces.mockResolvedValue({ items: [{ ...trace, status }], total: 1 });
    renderPage("/observability/traces");
    await screen.findByText("medical-booking");
    expect(document.querySelector(`.trace-status[data-status="${status}"]`)).toBeInTheDocument();
  });

  it.each(["errors", "agents", "tools", "llm", "slow-spans"])("renders a safe empty state for %s", async (name) => {
    api.getObservabilityCollection.mockResolvedValue({ items: [] });
    renderPage(`/observability/${name}`);
    expect(await screen.findByText("No hay datos registrados en esta categoría.")).toBeInTheDocument();
  });

  it.each(["Authorization: Bearer private", "OPENAI_API_KEY", "full prompt secret", "file body secret"])("does not render sensitive value %s", async (value) => {
    renderPage(`/observability/traces/${trace.trace_id}`);
    await screen.findByText("Waterfall");
    expect(screen.queryByText(value)).not.toBeInTheDocument();
  });
});
