import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  WorkflowListItem,
  WorkflowListResponse,
  WorkflowListStatus,
} from "../api/types";
import * as workflows from "../api/workflows";
import { ApiClientError } from "../api/client";
import { WorkflowStatusBadge } from "../components/WorkflowStatusBadge";
import { resetWorkflowListRequestsForTests } from "../hooks/useWorkflowList";
import { useWorkflowListStore } from "../stores/workflowListStore";
import { WorkflowListPage } from "./WorkflowListPage";

function item(overrides: Partial<WorkflowListItem> = {}): WorkflowListItem {
  return {
    thread_id: "a4529124-aaaa-bbbb-cccc-123456789012",
    project_name: "ui-auto-refresh-api",
    workflow_intent: "Crear API FastAPI con health",
    terminal_status: "completed",
    interrupted: false,
    pending_operation: null,
    pending_tool: null,
    tests_executed: true,
    tests_passed: true,
    test_summary: "1 passed, 2 warnings",
    planning_attempts: 1,
    implementation_attempts: 1,
    repair_phase: "not_started",
    repair_attempts: 0,
    supervisor_decision: "testing_repair",
    created_at: "2026-01-01T10:00:00Z",
    updated_at: "2026-01-01T11:00:00Z",
    ...overrides,
  };
}

function response(items: WorkflowListItem[] = [item()]): WorkflowListResponse {
  return {
    items,
    total: items.length,
    limit: 20,
    offset: 0,
    has_more: false,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}{location.search}</output>;
}

function renderPage(path = "/workflows", strict = false) {
  const content = (
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route
          path="/workflows"
          element={<><WorkflowListPage /><LocationProbe /></>}
        />
        <Route path="/workflows/:threadId" element={<div>Workflow detail</div>} />
        <Route path="/" element={<div>New workflow</div>} />
      </Routes>
    </MemoryRouter>
  );
  return render(strict ? <StrictMode>{content}</StrictMode> : content);
}

describe("workflow list", () => {
  beforeEach(() => {
    resetWorkflowListRequestsForTests();
    useWorkflowListStore.getState().reset();
  });

  it("renders list fields and test summary", async () => {
    vi.spyOn(workflows, "getWorkflows").mockResolvedValue(response());
    renderPage();
    expect(await screen.findByText("ui-auto-refresh-api")).toBeVisible();
    expect(screen.getByText("Tests: 1 passed, 2 warnings")).toBeVisible();
    expect(screen.getByText(/Thread:/)).toBeVisible();
    expect(screen.getByText(/Intentos: plan 1/)).toBeVisible();
  });

  it("shows initial loading", () => {
    vi.spyOn(workflows, "getWorkflows").mockReturnValue(new Promise(() => undefined));
    renderPage();
    expect(screen.getByText("Cargando workflows…")).toBeVisible();
  });

  it("shows the no-workflows empty state", async () => {
    vi.spyOn(workflows, "getWorkflows").mockResolvedValue(response([]));
    renderPage();
    expect(await screen.findByText("Todavía no hay workflows.")).toBeVisible();
    expect(screen.getAllByRole("link", { name: "Nuevo workflow" })).toHaveLength(2);
  });

  it("shows a distinct filtered empty state", async () => {
    vi.spyOn(workflows, "getWorkflows").mockResolvedValue(response([]));
    renderPage("/workflows?status=completed");
    expect(
      await screen.findByText("No encontramos workflows con esos filtros."),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Limpiar filtros" })).toBeVisible();
  });

  it.each<[WorkflowListStatus, string]>([
    ["completed", "Completado"],
    ["waiting", "Esperando aprobación"],
    ["running", "En ejecución"],
    ["pending", "Pendiente"],
    ["failed", "Fallido"],
  ])("renders the %s badge with text", (status, label) => {
    render(<WorkflowStatusBadge status={status} />);
    expect(screen.getByText(label)).toBeVisible();
  });

  it.each([
    ["completed", false, null, "Abrir"],
    ["pending", true, "prepare_environment", "Continuar"],
    ["failed", false, null, "Revisar"],
  ])("uses the correct action for %s", async (status, interrupted, operation, label) => {
    vi.spyOn(workflows, "getWorkflows").mockResolvedValue(
      response([item({
        terminal_status: status,
        interrupted,
        pending_operation: operation,
      })]),
    );
    renderPage();
    expect(await screen.findByRole("link", { name: label })).toBeVisible();
  });

  it("navigates every action to the same detail route", async () => {
    vi.spyOn(workflows, "getWorkflows").mockResolvedValue(response());
    renderPage();
    await userEvent.click(await screen.findByRole("link", { name: "Abrir" }));
    expect(screen.getByText("Workflow detail")).toBeVisible();
  });

  it("copies the full thread id", async () => {
    vi.spyOn(workflows, "getWorkflows").mockResolvedValue(response());
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Copiar thread ID" }));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      "a4529124-aaaa-bbbb-cccc-123456789012",
    );
    expect(screen.getByText("Thread copiado")).toBeInTheDocument();
  });

  it("hydrates status, search, sort and offset from the URL", async () => {
    const get = vi.spyOn(workflows, "getWorkflows").mockResolvedValue(response([]));
    renderPage(
      "/workflows?status=completed&search=ui&sort_by=project_name&sort_order=asc&offset=20",
    );
    await waitFor(() =>
      expect(get).toHaveBeenCalledWith(
        expect.objectContaining({
          status: "completed",
          search: "ui",
          sortBy: "project_name",
          sortOrder: "asc",
          offset: 20,
        }),
      ),
    );
    expect(screen.getByLabelText("Buscar")).toHaveValue("ui");
    expect(screen.getByLabelText("Estado")).toHaveValue("completed");
  });

  it("updates the status query and resets offset", async () => {
    vi.spyOn(workflows, "getWorkflows").mockResolvedValue(response([]));
    renderPage("/workflows?offset=20");
    await userEvent.selectOptions(screen.getByLabelText("Estado"), "waiting");
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(
        "/workflows?status=waiting",
      ),
    );
  });

  it("maps project A-Z sorting to query params", async () => {
    vi.spyOn(workflows, "getWorkflows").mockResolvedValue(response([]));
    renderPage();
    await userEvent.selectOptions(screen.getByLabelText("Orden"), "project_name:asc");
    await waitFor(() => {
      const location = screen.getByTestId("location").textContent ?? "";
      expect(location).toContain("sort_by=project_name");
      expect(location).toContain("sort_order=asc");
    });
  });

  it("debounces search and stores it in the URL", async () => {
    vi.spyOn(workflows, "getWorkflows").mockResolvedValue(response([]));
    renderPage();
    await userEvent.type(screen.getByLabelText("Buscar"), "ui auto");
    await waitFor(
      () => expect(screen.getByTestId("location").textContent).toContain("search=ui+auto"),
      { timeout: 1_000 },
    );
  });

  it("moves to next and previous pages", async () => {
    vi.spyOn(workflows, "getWorkflows").mockResolvedValue({
      ...response([item()]),
      total: 40,
      has_more: true,
    });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /Siguiente/ }));
    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toContain("offset=20"),
    );
    await userEvent.click(screen.getByRole("button", { name: /Anterior/ }));
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/workflows"),
    );
  });

  it("does not duplicate the initial request under StrictMode", async () => {
    const get = vi.spyOn(workflows, "getWorkflows").mockResolvedValue(response());
    renderPage("/workflows", true);
    await screen.findByText("ui-auto-refresh-api");
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("does not display AbortError as a banner", async () => {
    vi.spyOn(workflows, "getWorkflows").mockRejectedValue(
      new DOMException("cancelled", "AbortError"),
    );
    renderPage();
    await waitFor(() => expect(useWorkflowListStore.getState().isLoading).toBe(false));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows retry for retryable failures and keeps old items", async () => {
    useWorkflowListStore.getState().setResult(response([item()]));
    vi.spyOn(workflows, "getWorkflows").mockRejectedValue(
      new ApiClientError({
          title: "API no disponible",
          message: "Sin conexión",
          retryable: true,
      }),
    );
    renderPage();
    expect(await screen.findByText("No se pudieron cargar los workflows")).toBeVisible();
    expect(screen.getByText("Sin conexión")).toBeVisible();
    expect(screen.getByText("ui-auto-refresh-api")).toBeVisible();
    expect(screen.getByRole("button", { name: "Reintentar" })).toBeVisible();
  });

  it("polls only while a workflow is active", async () => {
    vi.useFakeTimers();
    const get = vi.spyOn(workflows, "getWorkflows").mockResolvedValue(
      response([item({ terminal_status: "running" })]),
    );
    renderPage();
    await act(async () => {
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(5_100);
    });
    expect(get.mock.calls.length).toBeGreaterThanOrEqual(2);
    vi.useRealTimers();
  });

  it("does not poll when all workflows are terminal", async () => {
    vi.useFakeTimers();
    const get = vi.spyOn(workflows, "getWorkflows").mockResolvedValue(response());
    renderPage();
    await act(async () => {
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(5_100);
    });
    expect(get).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });
});
