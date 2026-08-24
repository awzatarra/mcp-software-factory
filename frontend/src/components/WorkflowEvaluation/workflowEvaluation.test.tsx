import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as workflows from "../../api/workflows";
import { resetEvaluationLoadsForTests } from "../../hooks/useWorkflowEvaluation";
import { useWorkflowEvaluationStore } from "../../stores/workflowEvaluationStore";
import {
  workflowEvaluation,
  workflowEvent,
} from "../../test/fixtures";
import { WorkflowEvaluationTab } from "./WorkflowEvaluationTab";


function renderEvaluation(
  options: {
    category?: string | null;
    finding?: string | null;
    branchId?: string;
    events?: ReturnType<typeof workflowEvent>[];
  } = {},
) {
  const onSelectCategory = vi.fn();
  const onSelectFinding = vi.fn();
  const onOpenTask = vi.fn();
  const onOpenEvent = vi.fn();
  const onOpenFile = vi.fn();
  const view = render(
    <WorkflowEvaluationTab
      threadId="thread-1"
      branchId={options.branchId ?? "original"}
      events={options.events ?? []}
      selectedCategory={options.category ?? null}
      selectedFinding={options.finding ?? null}
      onSelectCategory={onSelectCategory}
      onSelectFinding={onSelectFinding}
      onOpenTask={onOpenTask}
      onOpenEvent={onOpenEvent}
      onOpenFile={onOpenFile}
    />,
  );
  return {
    ...view,
    onSelectCategory,
    onSelectFinding,
    onOpenTask,
    onOpenEvent,
    onOpenFile,
  };
}


describe("WorkflowEvaluationTab", () => {
  beforeEach(() => {
    resetEvaluationLoadsForTests();
    useWorkflowEvaluationStore.setState({
      key: "thread-1:original",
      evaluation: workflowEvaluation(),
      isLoading: false,
      isRefreshing: false,
      error: null,
    });
    vi.spyOn(workflows, "getWorkflowEvaluation").mockResolvedValue(
      workflowEvaluation(),
    );
  });

  it("shows the overall score, grade, final badge and version", () => {
    renderEvaluation();
    expect(screen.getByRole("heading", { name: "Evaluación general" })).toBeVisible();
    expect(screen.getByText("92")).toBeVisible();
    expect(screen.getByText("Excelente")).toBeVisible();
    expect(screen.getByText("Final")).toBeVisible();
    expect(screen.getByText("Scoring v1.2")).toBeVisible();
  });

  it("exposes an accessible progress bar independent from color", () => {
    renderEvaluation();
    expect(screen.getByRole("progressbar", { name: "Puntuación global" }))
      .toHaveAttribute("aria-valuenow", "92");
  });

  it.each([
    ["Planning", "20 / 20"],
    ["Implementación", "23 / 25"],
    ["Testing", "23 / 25"],
    ["Efficiency", "13 / 15"],
    ["Reliability", "13 / 15"],
  ])("renders the %s category", (label, score) => {
    renderEvaluation();
    const button = screen.getByRole("button", { name: new RegExp(label) });
    expect(button).toHaveTextContent(score);
  });

  it("expands a category selected in the query", () => {
    renderEvaluation({ category: "testing" });
    expect(screen.getByRole("button", { name: /Testing/ }))
      .toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("2 warnings en la ejecución de pruebas.")).toBeVisible();
  });

  it("selects and clears a category", async () => {
    const first = renderEvaluation();
    await userEvent.click(screen.getByRole("button", { name: /Testing/ }));
    expect(first.onSelectCategory).toHaveBeenCalledWith("testing");
    first.rerender(
      <WorkflowEvaluationTab
        threadId="thread-1"
        branchId="original"
        events={[]}
        selectedCategory="testing"
        selectedFinding={null}
        onSelectCategory={first.onSelectCategory}
        onSelectFinding={first.onSelectFinding}
        onOpenTask={first.onOpenTask}
        onOpenEvent={first.onOpenEvent}
        onOpenFile={first.onOpenFile}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /Testing/ }));
    expect(first.onSelectCategory).toHaveBeenLastCalledWith(null);
  });

  it("shows requirement and acceptance coverage", () => {
    renderEvaluation();
    expect(screen.getByRole("heading", { name: "Requisitos funcionales" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Criterios de aceptación" })).toBeVisible();
    expect(screen.getByText("GET /health devuelve ok")).toBeVisible();
    expect(screen.getByText("La prueba de health pasa")).toBeVisible();
  });

  it("shows explicit unknown coverage for partial data", () => {
    const partial = workflowEvaluation({
      evaluation_status: "partial",
      is_provisional: true,
      final_grade_available: false,
      grade: null,
      provisional_grade: "excellent",
      earned_points: 67,
      available_points: 75,
      provisional_percentage: 89.33,
      requirement_coverage: {
        total: 1,
        satisfied: 0,
        unsatisfied: 0,
        unknown: 1,
        coverage_percent: 0,
        items: [{
          index: 1,
          text: "Pending requirement",
          status: "unknown",
          evidence: [],
          related_task_ids: [],
          related_event_ids: [],
          related_files: [],
        }],
      },
    });
    useWorkflowEvaluationStore.setState({ evaluation: partial });
    renderEvaluation();
    expect(screen.getByRole("heading", { name: "Evaluación provisional" })).toBeVisible();
    expect(screen.getByText("Testing pendiente")).toBeVisible();
    expect(screen.queryByText("Excelente")).not.toBeInTheDocument();
    expect(screen.getByText("Sin evidencia")).toBeVisible();
  });

  it("shows pending Testing without a perfect score or final grade", () => {
    const base = workflowEvaluation();
    useWorkflowEvaluationStore.setState({
      evaluation: workflowEvaluation({
        evaluation_status: "partial",
        earned_points: 72,
        available_points: 75,
        provisional_percentage: 96,
        is_provisional: true,
        final_grade_available: false,
        grade: null,
        provisional_grade: "excellent",
        testing: {
          ...base.testing,
          score: 0,
          percentage: null,
          evaluation_state: "not_started",
          available_points: 0,
          positive_signals: [],
          penalties: [],
        },
      }),
    });
    renderEvaluation({ category: "testing" });
    const testing = screen.getByRole("button", { name: /Testing/ });
    expect(testing).toHaveTextContent("Pendiente de evaluación");
    expect(testing).toHaveTextContent("0 puntos disponibles de 25");
    expect(testing).not.toHaveTextContent("100%");
    expect(testing).not.toHaveTextContent("25 / 25");
    expect(screen.getByText("72")).toBeVisible();
    expect(screen.getByText("de 75 puntos disponibles")).toBeVisible();
    expect(screen.getByText("96% provisional")).toBeVisible();
    expect(screen.getByText("Las pruebas todavía no fueron ejecutadas.")).toBeVisible();
    expect(screen.queryByText("Excelente")).not.toBeInTheDocument();
  });

  it("shows pending Implementation before create_project", () => {
    const base = workflowEvaluation();
    useWorkflowEvaluationStore.setState({
      evaluation: workflowEvaluation({
        evaluation_status: "partial",
        earned_points: 50,
        available_points: 50,
        provisional_percentage: 100,
        is_provisional: true,
        final_grade_available: false,
        grade: null,
        provisional_grade: "excellent",
        implementation: {
          ...base.implementation,
          score: 0,
          percentage: null,
          evaluation_state: "not_started",
          available_points: 0,
          positive_signals: [],
          penalties: [],
        },
        testing: {
          ...base.testing,
          score: 0,
          percentage: null,
          evaluation_state: "not_started",
          available_points: 0,
          positive_signals: [],
          penalties: [],
        },
      }),
    });
    renderEvaluation({ category: "implementation" });
    const implementation = screen.getByRole("button", { name: /Implementación/ });
    expect(implementation).toHaveTextContent("Pendiente de evaluación");
    expect(implementation).toHaveTextContent("0 puntos disponibles de 25");
    expect(implementation).not.toHaveTextContent("25 / 25");
    expect(implementation).not.toHaveTextContent("100%");
    expect(screen.getByText("El proyecto todavía no fue creado.")).toBeVisible();
    expect(screen.getByText("50")).toBeVisible();
    expect(screen.getByText("de 50 puntos disponibles")).toBeVisible();
    expect(screen.getByText("Faltan 50 puntos por evaluar.")).toBeVisible();
    expect(screen.getByText("Testing pendiente")).toBeVisible();
  });

  it("shows partial Implementation evidence after project creation", () => {
    const base = workflowEvaluation();
    useWorkflowEvaluationStore.setState({
      evaluation: workflowEvaluation({
        evaluation_status: "partial",
        earned_points: 69,
        available_points: 69,
        provisional_percentage: 100,
        is_provisional: true,
        final_grade_available: false,
        grade: null,
        implementation: {
          ...base.implementation,
          score: 19,
          percentage: 100,
          evaluation_state: "partial",
          available_points: 19,
          penalties: [],
          positive_signals: [{
            code: "PROJECT_CREATED",
            category: "implementation",
            message: "Proyecto creado.",
            related_task_id: null,
            related_event_id: null,
            related_file: null,
          }],
        },
      }),
    });
    renderEvaluation({ category: "implementation" });
    const implementation = screen.getByRole("button", { name: /Implementación/ });
    expect(implementation).toHaveTextContent("Implementación parcial");
    expect(implementation).toHaveTextContent("19 / 19 disponibles");
    expect(screen.getByText("Creado")).toBeVisible();
    expect(screen.getByText("Pendiente")).toBeVisible();
    expect(screen.getByText("Pendientes")).toBeVisible();
  });

  it("updates partial Implementation when environment is prepared", () => {
    const base = workflowEvaluation();
    useWorkflowEvaluationStore.setState({
      evaluation: workflowEvaluation({
        evaluation_status: "partial",
        implementation: {
          ...base.implementation,
          score: 22,
          percentage: 88,
          evaluation_state: "partial",
          available_points: 25,
          positive_signals: [{
            code: "ENVIRONMENT_PREPARED",
            category: "implementation",
            message: "Entorno preparado.",
            related_task_id: null,
            related_event_id: null,
            related_file: null,
          }],
        },
      }),
    });
    renderEvaluation({ category: "implementation" });
    expect(screen.getByText("Preparado")).toBeVisible();
    expect(screen.getByText("Pendientes")).toBeVisible();
    expect(screen.getByRole("button", { name: /Implementación/ }))
      .toHaveTextContent("22 / 25 disponibles");
  });

  it("shows evaluated Implementation only in the completed state", () => {
    renderEvaluation();
    const implementation = screen.getByRole("button", { name: /Implementación/ });
    expect(implementation).toHaveTextContent("Implementación evaluada");
    expect(implementation).toHaveTextContent("23 / 25");
    expect(screen.getByText("Excelente")).toBeVisible();
  });

  it("navigates from IMPLEMENTATION_PENDING to task and approval", async () => {
    useWorkflowEvaluationStore.setState({
      evaluation: workflowEvaluation({
        findings: [{
          code: "IMPLEMENTATION_PENDING",
          category: "implementation",
          title: "Implementación pendiente",
          description: "La creación del proyecto todavía no fue ejecutada.",
          severity: "info",
          recommendation: null,
          related_task_id: "task-implementation",
          related_event_id: "event-create-approval",
          related_file: null,
        }],
      }),
    });
    const result = renderEvaluation({ finding: "IMPLEMENTATION_PENDING" });
    await userEvent.click(screen.getByRole("button", { name: "Ver tarea" }));
    await userEvent.click(screen.getByRole("button", { name: "Ver en Timeline" }));
    expect(result.onOpenTask).toHaveBeenCalledWith("task-implementation");
    expect(result.onOpenEvent).toHaveBeenCalledWith("event-create-approval");
  });

  it("opens main.py from a validated Implementation finding", async () => {
    useWorkflowEvaluationStore.setState({
      evaluation: workflowEvaluation({
        findings: [{
          code: "IMPLEMENTATION_VALID",
          category: "implementation",
          title: "Implementación válida",
          description: "Los artefactos cumplen el contrato.",
          severity: "info",
          recommendation: null,
          related_task_id: "task-implementation",
          related_event_id: "event-implementation-completed",
          related_file: "app/main.py",
        }],
      }),
    });
    const result = renderEvaluation({ finding: "IMPLEMENTATION_VALID" });
    await userEvent.click(screen.getByRole("button", { name: "Ver archivo" }));
    expect(result.onOpenFile).toHaveBeenCalledWith("app/main.py");
  });

  it("renders positive signals without a zero-point bonus", () => {
    renderEvaluation({ category: "planning" });
    expect(screen.getByRole("heading", { name: "Señales positivas" })).toBeVisible();
    expect(screen.getByText("Validación completada.")).toBeVisible();
    expect(screen.queryByText(/\+0/)).not.toBeInTheDocument();
  });

  it("keeps a materialized scoring 1.0 evaluation viewable", () => {
    const legacy = workflowEvaluation({ scoring_version: "1.0" }) as unknown as
      Record<string, unknown>;
    delete legacy.earned_points;
    delete legacy.available_points;
    delete legacy.provisional_percentage;
    delete legacy.final_grade_available;
    useWorkflowEvaluationStore.setState({
      evaluation: legacy as unknown as ReturnType<typeof workflowEvaluation>,
    });
    renderEvaluation();
    expect(screen.getByText("Scoring v1.0")).toBeVisible();
    expect(screen.getByText("92")).toBeVisible();
    expect(screen.getByText("Excelente")).toBeVisible();
  });

  it.each([
    ["Planning", "100 ms"],
    ["Implementation", "45.0 s"],
    ["Testing", "1.3 s"],
    ["Repair", "n/a"],
    ["Finalize", "100 ms"],
  ])("shows the %s duration", (label, value) => {
    renderEvaluation();
    const term = screen
      .getAllByText(label)
      .find((element) => element.tagName === "DT")!;
    expect(term).toBeDefined();
    expect(term.parentElement).toHaveTextContent(value);
  });

  it("separates wall clock, active execution and approval waiting", () => {
    renderEvaluation();
    expect(screen.getByText("Tiempo total").parentElement).toHaveTextContent("48.0 s");
    expect(screen.getByText(/Ejecución activa:/)).toHaveTextContent("46.4 s");
    expect(screen.getByText(/Esperando aprobaciones:/)).toHaveTextContent("1.6 s");
    expect(screen.getByText(/no deben sumarse entre sí/)).toBeVisible();
  });

  it("expands a warning finding and its recommendation", () => {
    renderEvaluation({ finding: "TEST_WARNINGS" });
    expect(screen.getByText("Pytest reportó 2 warnings.")).toBeVisible();
    expect(screen.getAllByText("Revisar y eliminar warnings de pytest.").length)
      .toBeGreaterThan(0);
  });

  it("supports critical findings without relying only on color", () => {
    useWorkflowEvaluationStore.setState({
      evaluation: workflowEvaluation({
        findings: [{
          code: "SUPERVISOR_LOOP",
          category: "reliability",
          title: "Loop del Supervisor",
          description: "No hubo progreso.",
          severity: "critical",
          recommendation: "Revisar handoffs.",
          related_task_id: null,
          related_event_id: null,
          related_file: null,
        }],
      }),
    });
    renderEvaluation({ finding: "SUPERVISOR_LOOP" });
    expect(screen.getByText("Crítico")).toBeVisible();
    expect(screen.getByText("No hubo progreso.")).toBeVisible();
  });

  it("navigates from a finding to task, event and file", async () => {
    const result = renderEvaluation({ finding: "TEST_WARNINGS" });
    await userEvent.click(screen.getByRole("button", { name: "Ver tarea" }));
    await userEvent.click(screen.getByRole("button", { name: "Ver en Timeline" }));
    await userEvent.click(screen.getByRole("button", { name: "Ver archivo" }));
    expect(result.onOpenTask).toHaveBeenCalledWith("task-qa");
    expect(result.onOpenEvent).toHaveBeenCalledWith("event-test");
    expect(result.onOpenFile).toHaveBeenCalledWith("tests/test_health.py");
  });

  it("does not render navigation buttons without relationships", () => {
    useWorkflowEvaluationStore.setState({
      evaluation: workflowEvaluation({
        findings: [{
          code: "PLAN_VALID",
          category: "planning",
          title: "Plan válido",
          description: "Validado.",
          severity: "info",
          recommendation: null,
          related_task_id: null,
          related_event_id: null,
          related_file: null,
        }],
      }),
    });
    renderEvaluation({ finding: "PLAN_VALID" });
    expect(screen.queryByRole("button", { name: "Ver tarea" })).not.toBeInTheDocument();
  });

  it("shows empty findings and recommendations states", () => {
    useWorkflowEvaluationStore.setState({
      evaluation: workflowEvaluation({ findings: [], recommendations: [] }),
    });
    renderEvaluation();
    expect(screen.getByText("No hay hallazgos para este estado.")).toBeVisible();
    expect(screen.getByText("No hay recomendaciones pendientes.")).toBeVisible();
  });

  it("shows loading without stale data", () => {
    vi.mocked(workflows.getWorkflowEvaluation).mockImplementation(
      () => new Promise(() => undefined),
    );
    useWorkflowEvaluationStore.setState({
      evaluation: null,
      isLoading: true,
    });
    renderEvaluation();
    expect(screen.getByRole("status")).toHaveTextContent("Calculando evaluación");
  });

  it("shows the initial error and supports retry", async () => {
    useWorkflowEvaluationStore.setState({
      evaluation: null,
      isLoading: false,
      error: {
        title: "No disponible",
        message: "Falló",
        retryable: true,
        scope: "evaluation",
      },
    });
    renderEvaluation();
    expect(screen.getByText("No disponible")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    await waitFor(() => expect(workflows.getWorkflowEvaluation).toHaveBeenCalled());
  });

  it("preserves stale data when refresh fails", () => {
    useWorkflowEvaluationStore.setState({
      error: {
        title: "No disponible",
        message: "Falló refresh",
        retryable: true,
        scope: "evaluation",
      },
    });
    renderEvaluation();
    expect(screen.getByText("92")).toBeVisible();
    expect(screen.getByText("Falló refresh")).toBeVisible();
  });

  it("deduplicates the initial request under StrictMode", async () => {
    useWorkflowEvaluationStore.setState({
      key: null,
      evaluation: null,
      isLoading: false,
      error: null,
    });
    render(
      <StrictMode>
        <WorkflowEvaluationTab
          threadId="thread-1"
          branchId="original"
          events={[]}
          selectedCategory={null}
          selectedFinding={null}
          onSelectCategory={vi.fn()}
          onSelectFinding={vi.fn()}
          onOpenTask={vi.fn()}
          onOpenEvent={vi.fn()}
          onOpenFile={vi.fn()}
        />
      </StrictMode>,
    );
    await screen.findByText("92");
    expect(workflows.getWorkflowEvaluation).toHaveBeenCalledTimes(1);
  });

  it("debounces a relevant live event", async () => {
    vi.useFakeTimers();
    const view = renderEvaluation();
    await act(async () => vi.runAllTimersAsync());
    vi.mocked(workflows.getWorkflowEvaluation).mockClear();
    view.rerender(
      <WorkflowEvaluationTab
        threadId="thread-1"
        branchId="original"
        events={[workflowEvent(8, { type: "test_run_completed" })]}
        selectedCategory={null}
        selectedFinding={null}
        onSelectCategory={view.onSelectCategory}
        onSelectFinding={view.onSelectFinding}
        onOpenTask={view.onOpenTask}
        onOpenEvent={view.onOpenEvent}
        onOpenFile={view.onOpenFile}
      />,
    );
    expect(workflows.getWorkflowEvaluation).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(300));
    expect(workflows.getWorkflowEvaluation).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it("does not refresh for irrelevant technical events", async () => {
    const view = renderEvaluation();
    vi.mocked(workflows.getWorkflowEvaluation).mockClear();
    view.rerender(
      <WorkflowEvaluationTab
        threadId="thread-1"
        branchId="original"
        events={[workflowEvent(9, { type: "tool_started" })]}
        selectedCategory={null}
        selectedFinding={null}
        onSelectCategory={view.onSelectCategory}
        onSelectFinding={view.onSelectFinding}
        onOpenTask={view.onOpenTask}
        onOpenEvent={view.onOpenEvent}
        onOpenFile={view.onOpenFile}
      />,
    );
    await new Promise((resolve) => window.setTimeout(resolve, 350));
    expect(workflows.getWorkflowEvaluation).not.toHaveBeenCalled();
  });

  it("reloads for a different branch and keeps branch caches separate", async () => {
    const view = renderEvaluation();
    vi.mocked(workflows.getWorkflowEvaluation).mockClear();
    view.rerender(
      <WorkflowEvaluationTab
        threadId="thread-1"
        branchId="fork-1"
        events={[]}
        selectedCategory={null}
        selectedFinding={null}
        onSelectCategory={view.onSelectCategory}
        onSelectFinding={view.onSelectFinding}
        onOpenTask={view.onOpenTask}
        onOpenEvent={view.onOpenEvent}
        onOpenFile={view.onOpenFile}
      />,
    );
    await waitFor(() =>
      expect(workflows.getWorkflowEvaluation).toHaveBeenCalledWith(
        "thread-1",
        "fork-1",
        expect.any(AbortSignal),
      ),
    );
  });

  it("ignores AbortError without replacing existing data", async () => {
    vi.mocked(workflows.getWorkflowEvaluation).mockRejectedValue(
      new DOMException("Aborted", "AbortError"),
    );
    renderEvaluation({ branchId: "fork-1" });
    await waitFor(() => expect(workflows.getWorkflowEvaluation).toHaveBeenCalled());
    expect(useWorkflowEvaluationStore.getState().error).toBeNull();
  });
});
