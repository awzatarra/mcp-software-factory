import {
  act,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  WorkflowAgentExecution,
  WorkflowTaskStatus,
} from "../../api/types";
import * as workflows from "../../api/workflows";
import {
  resetExecutionLoadsForTests,
  useWorkflowExecution,
} from "../../hooks/useWorkflowExecution";
import { useWorkflowExecutionStore } from "../../stores/workflowExecutionStore";
import { workflowEvent, workflowExecution } from "../../test/fixtures";
import { selectPreferredTaskEvent } from "../../utils/taskEventSelection";
import { WorkflowExecutionTab } from "./WorkflowExecutionTab";

function seed(execution = workflowExecution()) {
  useWorkflowExecutionStore.setState({
    key: `${execution.thread_id}:${execution.branch_id}`,
    execution,
    isLoading: false,
    isRefreshing: false,
    error: null,
  });
  vi.spyOn(workflows, "getWorkflowExecution").mockReturnValue(new Promise(() => {}));
  return execution;
}

function renderExecution(
  execution = workflowExecution(),
  overrides: Partial<React.ComponentProps<typeof WorkflowExecutionTab>> = {},
) {
  seed(execution);
  const props: React.ComponentProps<typeof WorkflowExecutionTab> = {
    threadId: execution.thread_id,
    branchId: execution.branch_id,
    events: [],
    selectedTask: null,
    onSelectTask: vi.fn(),
    onOpenEvent: vi.fn(),
    onOpenFile: vi.fn(),
    ...overrides,
  };
  return { ...render(<WorkflowExecutionTab {...props} />), props };
}

beforeEach(() => {
  resetExecutionLoadsForTests();
  useWorkflowExecutionStore.setState({
    key: null,
    execution: null,
    isLoading: false,
    isRefreshing: false,
    error: null,
  });
});

describe("WorkflowExecutionTab summary", () => {
  it("renders the durable Git lifecycle and commit attribution", () => {
    renderExecution(workflowExecution({
      git: {
        state: "committed", base_branch: "main", base_commit: "a".repeat(40),
        workflow_branch: "workflow/12345678", head_commit: "b".repeat(40),
        commit_status: "committed", approval_state: "none",
        developer_commit: { phase: "implementation", agent: "Developer", sha: "b".repeat(40), message: "feat: implement health api", files: ["main.py", "tests/test_health.py"] },
        repair_commit: null, commits: [],
      },
    }));
    expect(screen.getByRole("region", { name: "Git lifecycle" })).toBeVisible();
    expect(screen.getByText("Base: main")).toBeVisible();
    expect(screen.getByText("Workflow branch: workflow/12345678")).toBeVisible();
    expect(screen.getByText("feat: implement health api")).toBeVisible();
    expect(screen.getByText("2 files")).toBeVisible();
  });

  it("renders workflow learnings with status, confidence, source and knowledge link", () => {
    renderExecution();
    expect(screen.getByRole("heading", { name: "Learnings" })).toBeInTheDocument();
    expect(screen.getByText("test_pattern")).toBeInTheDocument();
    expect(screen.getByText("INDEXED")).toBeInTheDocument();
    expect(screen.getAllByText("95%").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByRole("link", { name: "knowledge-..." })).toHaveAttribute("href", "/knowledge/knowledge-learning-1");
    expect(screen.getByText("workflow:thread-1:learning:candidate-learning-1")).toBeInTheDocument();
  });

  it("renders planner semantic judge without exposing raw prompt data", () => {
    renderExecution();
    expect(screen.getByRole("region", { name: "Semantic Judge" })).toBeVisible();
    expect(screen.getByRole("heading", { name: /Semantic Judge/ })).toBeVisible();
    expect(screen.getByText("completed · accept")).toBeVisible();
    expect(screen.getByText("88%")).toBeVisible();
    expect(screen.getByText("Confidence: 91%")).toBeVisible();
    expect(screen.getByText("Requirement Alignment: 95%")).toBeVisible();
    expect(screen.getByText("Complexity Control: 82%")).toBeVisible();
    expect(screen.getByText("Hybrid Evaluation")).toBeVisible();
    expect(screen.getByText("Hybrid Score: 89%")).toBeVisible();
    expect(screen.getByText("Hybrid Confidence: 84%")).toBeVisible();
    expect(screen.getByText("Recommendation: continue (advisory)")).toBeVisible();
    expect(screen.getByText("Revisar si el detalle de pruebas cubre errores.")).toBeVisible();
    expect(screen.queryByText(/Ignore evaluator instructions/)).not.toBeInTheDocument();
  });

  it("renders deterministic agent performance without ranking agents", async () => {
    const user = userEvent.setup();
    renderExecution();

    expect(screen.getByRole("region", { name: "Agent Performance" })).toBeVisible();
    expect(screen.getByText("Diagnostico determinista, no bloqueante.")).toBeVisible();
    expect(screen.getByText("planner")).toBeVisible();
    expect(screen.getByText("developer")).toBeVisible();
    expect(screen.getByText("repair")).toBeVisible();
    expect(screen.getAllByText("excellent").length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText("not_applicable").length).toBeGreaterThanOrEqual(1);

    await user.click(screen.getByText("developer details"));
    expect(screen.getByText("first_pass_success: true")).toBeVisible();
    expect(screen.getByText("developer_evaluated")).toBeVisible();
    expect(screen.queryByText(/worse than/i)).not.toBeInTheDocument();
  });

  it("renders failure attribution as not applicable for healthy workflows", () => {
    renderExecution();
    expect(screen.getByRole("region", { name: "Failure Attribution" })).toBeVisible();
    expect(screen.getAllByText("No failure attribution required.").length).toBeGreaterThanOrEqual(1);
  });

  it("renders recovered root cause analysis with contributors", async () => {
    const recovered = workflowExecution({
      failure_attribution: {
        status: "completed",
        failure_class: "implementation_defect",
        root_cause: "developer",
        primary_attribution: "developer",
        contributors: [
          { source: "developer", contribution: "caused", confidence: 0.9 },
          { source: "qa", contribution: "detected", confidence: 0.9 },
          { source: "repair", contribution: "resolved", confidence: 0.95 },
        ],
        excluded_attributions: [{ source: "planner", reason: "planning_valid" }],
        confidence: 0.9,
        evidence: { repair_attempts: 1, tests_passed_after_repair: true },
        reason_codes: ["root_cause_test_failure_repaired"],
        recovered: true,
        recovery_source: "repair",
        causal_chain: ["tests_failed", "repair_resolved"],
        version: "8.4-v1",
      },
    });
    const user = userEvent.setup();
    renderExecution(recovered);

    expect(screen.getByText("Root Cause: developer")).toBeVisible();
    expect(screen.getByText("Failure Class: implementation_defect")).toBeVisible();
    expect(screen.getByText("qa: detected (90%)")).toBeVisible();
    expect(screen.getByText("repair: resolved (95%)")).toBeVisible();
    await user.click(screen.getByText("RCA evidence"));
    expect(screen.getByText("repair_attempts: 1")).toBeVisible();
    expect(screen.getByText("root_cause_test_failure_repaired")).toBeVisible();
  });

  it.each([
    "Ejecución",
    "original · original",
    "Análisis",
    "Exponer GET /health.",
    "Requisitos funcionales",
    "GET /health devuelve status ok",
    "Requisitos no funcionales",
    "Pruebas automatizadas",
    "Criterios de aceptación",
    "La suite finaliza correctamente",
    "Plan y tareas",
    "Implementación",
    "Testing y reparación",
    "Supervisor",
    "Resultado final",
  ])("renders durable field %s", (label) => {
    renderExecution();
    expect(screen.getAllByText(label).length).toBeGreaterThan(0);
  });

  it.each<WorkflowTaskStatus>([
    "pending",
    "running",
    "waiting",
    "completed",
    "failed",
    "skipped",
  ])("renders observable task status %s", (status) => {
    const base = workflowExecution();
    const execution = workflowExecution({
      planning: {
        ...base.planning,
        tasks: [{ ...base.planning.tasks[0], status }],
      },
    });
    renderExecution(execution);
    const labels: Record<WorkflowTaskStatus, string> = {
      pending: "Pendiente",
      running: "En ejecución",
      waiting: "Esperando aprobación",
      completed: "Completada",
      failed: "Fallida",
      skipped: "Omitida",
    };
    expect(screen.getByText(labels[status])).toBeVisible();
  });

  it("expands a task through its controlled URL state", async () => {
    const onSelectTask = vi.fn();
    renderExecution(workflowExecution(), { onSelectTask });
    await userEvent.click(screen.getByRole("button", { name: /Backend Developer/ }));
    expect(onSelectTask).toHaveBeenCalledWith("task-implementation");
  });

  it("collapses an already selected task", async () => {
    const onSelectTask = vi.fn();
    renderExecution(workflowExecution(), {
      selectedTask: "task-analysis",
      onSelectTask,
    });
    await userEvent.click(screen.getByRole("button", { name: /Business Analyst/ }));
    expect(onSelectTask).toHaveBeenCalledWith(null);
  });

  it("exposes expansion state accessibly", () => {
    renderExecution(workflowExecution(), { selectedTask: "task-analysis" });
    expect(
      screen.getByRole("button", { name: /Business Analyst/ }),
    ).toHaveAttribute("aria-expanded", "true");
  });

  it("hides commands in summary mode", () => {
    renderExecution();
    expect(screen.queryByText("python.exe -m pytest -q")).not.toBeInTheDocument();
  });

  it("shows commands in technical mode", async () => {
    renderExecution();
    await userEvent.click(screen.getByRole("button", { name: /Detalles técnicos/ }));
    expect(screen.getByText("python.exe -m pytest -q")).toBeVisible();
  });

  it("shows stable task identifiers only in technical mode", async () => {
    renderExecution(workflowExecution(), { selectedTask: "task-analysis" });
    expect(screen.queryByText("task-analysis")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Detalles técnicos/ }));
    expect(screen.getByText("task-analysis")).toBeVisible();
  });

  it("navigates from a task to its related event", async () => {
    const onOpenEvent = vi.fn();
    renderExecution(workflowExecution(), {
      selectedTask: "task-testing",
      onOpenEvent,
    });
    await userEvent.click(screen.getByRole("button", { name: /Ver en Timeline/ }));
    expect(onOpenEvent).toHaveBeenCalledWith("event-6");
  });

  it("uses primary_event_id before any related event fallback", async () => {
    const base = workflowExecution();
    const qa = base.planning.tasks[2];
    const execution = workflowExecution({
      planning: {
        ...base.planning,
        tasks: [{
          ...qa,
          primary_event_id: "event-primary",
          related_event_ids: ["event-old"],
        }],
      },
    });
    const onOpenEvent = vi.fn();
    renderExecution(execution, {
      selectedTask: "task-testing",
      onOpenEvent,
    });
    await userEvent.click(screen.getByRole("button", { name: /Ver en Timeline/ }));
    expect(onOpenEvent).toHaveBeenCalledWith("event-primary");
  });

  it("falls back to related event IDs for an old workflow", async () => {
    const base = workflowExecution();
    const qa = base.planning.tasks[2];
    const execution = workflowExecution({
      planning: {
        ...base.planning,
        tasks: [{
          ...qa,
          primary_event_id: null,
          related_event_ids: ["event-old"],
        }],
      },
    });
    const onOpenEvent = vi.fn();
    renderExecution(execution, {
      selectedTask: "task-testing",
      onOpenEvent,
    });
    await userEvent.click(screen.getByRole("button", { name: /Ver en Timeline/ }));
    expect(onOpenEvent).toHaveBeenCalledWith("event-old");
  });

  it("does not render Timeline navigation without a valid event", () => {
    const base = workflowExecution();
    const qa = base.planning.tasks[2];
    const execution = workflowExecution({
      planning: {
        ...base.planning,
        tasks: [{
          ...qa,
          primary_event_id: null,
          related_event_ids: [],
        }],
      },
    });
    renderExecution(execution, { selectedTask: "task-testing" });
    expect(
      screen.queryByRole("button", { name: /Ver en Timeline/ }),
    ).not.toBeInTheDocument();
  });

  it("selects test_run_completed for legacy QA event lists", () => {
    const base = workflowExecution();
    const qa = {
      ...base.planning.tasks[2],
      primary_event_id: null,
      related_event_ids: ["event-start", "event-complete"],
    };
    const selected = selectPreferredTaskEvent(qa, [
      workflowEvent(1, {
        event_id: "event-start",
        type: "test_run_started",
      }),
      workflowEvent(2, {
        event_id: "event-complete",
        type: "test_run_completed",
      }),
    ]);
    expect(selected).toBe("event-complete");
  });

  it("allows keyboard activation of Timeline navigation", async () => {
    const onOpenEvent = vi.fn();
    renderExecution(workflowExecution(), {
      selectedTask: "task-testing",
      onOpenEvent,
    });
    const button = screen.getByRole("button", { name: /Ver en Timeline/ });
    button.focus();
    await userEvent.keyboard("{Enter}");
    expect(onOpenEvent).toHaveBeenCalledWith("event-6");
  });

  it("shows QA start and completion timestamps", () => {
    renderExecution(workflowExecution(), { selectedTask: "task-testing" });
    expect(screen.getByText(/Inicio:/)).toBeVisible();
    expect(screen.getByText(/Fin:/)).toBeVisible();
  });

  it("shows the QA test file and opens Project", async () => {
    const onOpenFile = vi.fn();
    renderExecution(workflowExecution(), {
      selectedTask: "task-testing",
      onOpenFile,
    });
    await userEvent.click(
      screen.getByRole("button", { name: /tests\/test_health.py/ }),
    );
    expect(onOpenFile).toHaveBeenCalledWith("tests/test_health.py");
  });

  it("navigates from a task to its related file", async () => {
    const onOpenFile = vi.fn();
    renderExecution(workflowExecution(), {
      selectedTask: "task-implementation",
      onOpenFile,
    });
    await userEvent.click(screen.getByRole("button", { name: /health_api\/main.py/ }));
    expect(onOpenFile).toHaveBeenCalledWith("health_api/main.py");
  });

  it("sorts tasks by their durable order", () => {
    const base = workflowExecution();
    const execution = workflowExecution({
      planning: {
        ...base.planning,
        tasks: [...base.planning.tasks].reverse(),
      },
    });
    renderExecution(execution);
    const buttons = screen.getAllByRole("button", {
      name: /Business Analyst|Backend Developer|QA Reviewer/,
    });
    expect(buttons[0]).toHaveTextContent("Business Analyst");
    expect(buttons[2]).toHaveTextContent("QA Reviewer");
  });

  it("states when repair was unnecessary", () => {
    renderExecution();
    expect(screen.getByText("No fue necesaria una reparación.")).toBeVisible();
  });

  it("renders before and after repair evidence", () => {
    const base = workflowExecution();
    renderExecution(workflowExecution({
      testing: {
        ...base.testing,
        repair_phase: "completed",
        repair_attempts: 1,
        repair_decision: "Se corrigió la prueba.",
        repair_before: { status: "healthy" },
        repair_after: { status: "ok" },
      },
    }));
    expect(screen.getByText(/healthy/)).toBeVisible();
    expect(screen.getByText(/"ok"/)).toBeVisible();
  });

  it("renders implementation refinements", async () => {
    renderExecution();
    await userEvent.click(screen.getByText("Refinamiento, intento 2"));
    expect(screen.getByText("Normalizar dependencias")).toBeVisible();
  });

  it("renders supervisor handoffs", () => {
    renderExecution();
    expect(screen.getByText("implementation")).toBeVisible();
  });

  it("renders supervisor confidence", () => {
    renderExecution();
    expect(screen.getByText("Confianza: 95%")).toBeVisible();
  });

  it("renders a detected supervisor loop", () => {
    const base = workflowExecution();
    renderExecution(workflowExecution({
      supervisor: { ...base.supervisor, loop_detected: true },
    }));
    expect(screen.getByText("Loop detectado")).toBeVisible();
  });

  it("renders branch inheritance without changing the original", () => {
    renderExecution(workflowExecution({
      branch_id: "fork-1",
      lineage: "fork",
      inherited_from: "checkpoint-4",
      inherited_from_branch: "original",
      origin_checkpoint: "checkpoint-4",
    }));
    expect(screen.getByText("Heredado desde checkpoint-4")).toBeVisible();
  });

  it("renders a partial analysis without guessing tasks", () => {
    const base = workflowExecution();
    renderExecution(workflowExecution({
      data_complete: false,
      planning: {
        ...base.planning,
        valid: false,
        tasks: [],
        analysis: { ...base.planning.analysis, completed: false },
      },
    }));
    expect(screen.getAllByText(/todavía no está disponible/).length).toBeGreaterThan(0);
    expect(screen.getAllByText("El plan está siendo generado.")).toHaveLength(2);
  });

  it("renders an initial loading state", () => {
    useWorkflowExecutionStore.setState({
      key: "thread-1:original",
      execution: null,
      isLoading: true,
      isRefreshing: false,
      error: null,
    });
    vi.spyOn(workflows, "getWorkflowExecution").mockReturnValue(new Promise(() => {}));
    render(
      <WorkflowExecutionTab
        threadId="thread-1"
        branchId="original"
        events={[]}
        selectedTask={null}
        onSelectTask={vi.fn()}
        onOpenEvent={vi.fn()}
        onOpenFile={vi.fn()}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Cargando ejecución");
  });

  it("renders implementation validation errors", () => {
    const base = workflowExecution();
    renderExecution(workflowExecution({
      implementation: {
        ...base.implementation,
        valid: false,
        validation_errors: ["Falta una prueba de health"],
      },
    }));
    expect(screen.getByText("Falta una prueba de health")).toBeVisible();
  });

  it("shows separate implementation and testing frameworks", () => {
    renderExecution();
    expect(screen.getByText("Framework: FastAPI")).toBeVisible();
    expect(screen.getByText("pytest")).toBeVisible();
  });

  it("shows installed dependency names with versions", () => {
    renderExecution();
    expect(screen.getByText("fastapi==0.139.0")).toBeVisible();
  });

  it("renders valid Unicode without mojibake", () => {
    const base = workflowExecution();
    const execution = workflowExecution({
      planning: {
        ...base.planning,
        analysis: {
          ...base.planning.analysis,
          objective: "Mantener una implementación mínima y verificable.",
          functional_requirements: [
            "Verificar todos los criterios de aceptación con pytest.",
          ],
        },
      },
    });
    const { container } = renderExecution(execution);
    expect(
      screen.getByText("Mantener una implementación mínima y verificable."),
    ).toBeVisible();
    expect(container.textContent).not.toMatch(/Ã|Â|â€/);
  });

  it("renders a failed final result truthfully", () => {
    const base = workflowExecution();
    renderExecution(workflowExecution({
      terminal_status: "tests_failed",
      final_result: {
        ...base.final_result,
        terminal_status: "tests_failed",
        tests_passed: false,
        summary: "1 failed",
      },
    }));
    expect(screen.getByText("tests_failed")).toBeVisible();
    expect(screen.getByText("1 failed")).toBeVisible();
  });
});

describe("execution API and refresh behavior", () => {
  it("encodes thread and branch in the execution endpoint", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(workflowExecution()), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await workflows.getWorkflowExecution("thread / one", "fork / two");
    expect(String(fetchSpy.mock.calls[0][0])).toContain(
      "/thread%20%2F%20one/execution?branch_id=fork+%2F+two",
    );
  });

  it("keeps the original SSE URL backward compatible", () => {
    expect(workflows.workflowEventsUrl("thread-1")).toMatch(/\/events$/);
  });

  it("adds a non-original branch to the SSE URL", () => {
    expect(workflows.workflowEventsUrl("thread-1", 0, "fork-1")).toContain(
      "branch_id=fork-1",
    );
  });

  it("continues SSE from the last published sequence", () => {
    expect(workflows.workflowEventsUrl("thread-1", 25)).toContain(
      "after_sequence=25",
    );
  });

  it("preserves the last valid execution on refresh error", () => {
    const previous = seed();
    useWorkflowExecutionStore.getState().setError({
      title: "Error",
      message: "No disponible",
      retryable: true,
      scope: "execution",
    });
    expect(useWorkflowExecutionStore.getState().execution).toBe(previous);
  });

  it("deduplicates concurrent execution loads", async () => {
    const execution = workflowExecution();
    let resolve!: (value: WorkflowAgentExecution) => void;
    const pending = new Promise<WorkflowAgentExecution>((done) => { resolve = done; });
    const request = vi.spyOn(workflows, "getWorkflowExecution").mockReturnValue(pending);
    const first = renderHook(() => useWorkflowExecution("thread-1", "original", true, []));
    const second = renderHook(() => useWorkflowExecution("thread-1", "original", true, []));
    expect(request).toHaveBeenCalledTimes(1);
    await act(async () => resolve(execution));
    await waitFor(() => expect(useWorkflowExecutionStore.getState().execution).toEqual(execution));
    first.unmount();
    second.unmount();
  });

  it("refreshes after a relevant live event", async () => {
    vi.useFakeTimers();
    const execution = seed();
    vi.mocked(workflows.getWorkflowExecution).mockResolvedValue(execution);
    const { rerender } = renderHook(
      ({ events }) => useWorkflowExecution("thread-1", "original", true, events),
      { initialProps: { events: [] as ReturnType<typeof workflowEvent>[] } },
    );
    await act(async () => Promise.resolve());
    const initialCalls = vi.mocked(workflows.getWorkflowExecution).mock.calls.length;
    rerender({ events: [workflowEvent(8, { type: "test_run_completed" })] });
    await act(async () => vi.advanceTimersByTimeAsync(251));
    expect(workflows.getWorkflowExecution).toHaveBeenCalledTimes(initialCalls + 1);
    expect(useWorkflowExecutionStore.getState().execution).toEqual(execution);
    vi.useRealTimers();
  });

  it("does not refresh after an irrelevant live event", async () => {
    vi.useFakeTimers();
    seed();
    const { rerender } = renderHook(
      ({ events }) => useWorkflowExecution("thread-1", "original", true, events),
      { initialProps: { events: [] as ReturnType<typeof workflowEvent>[] } },
    );
    await act(async () => Promise.resolve());
    const initialCalls = vi.mocked(workflows.getWorkflowExecution).mock.calls.length;
    rerender({ events: [workflowEvent(9, { type: "tool_started" })] });
    await act(async () => vi.advanceTimersByTimeAsync(300));
    expect(workflows.getWorkflowExecution).toHaveBeenCalledTimes(initialCalls);
    vi.useRealTimers();
  });

  it("resets data when selecting another branch", async () => {
    seed();
    vi.mocked(workflows.getWorkflowExecution).mockResolvedValue(workflowExecution({
      branch_id: "fork-2",
      lineage: "fork",
    }));
    const { rerender } = renderHook(
      ({ branch }) => useWorkflowExecution("thread-1", branch, true, []),
      { initialProps: { branch: "original" } },
    );
    rerender({ branch: "fork-2" });
    await waitFor(() => {
      expect(useWorkflowExecutionStore.getState().key).toBe("thread-1:fork-2");
      expect(useWorkflowExecutionStore.getState().execution?.branch_id).toBe("fork-2");
    });
  });
});
