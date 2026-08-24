import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import {
  MemoryRouter,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as workflows from "../api/workflows";
import { useWorkflowStore } from "../stores/workflowStore";
import { resetEventStreamsForTests } from "../hooks/useWorkflowEvents";
import { resetWorkflowLoadsForTests } from "../hooks/workflowLoader";
import { resetProjectLoadsForTests } from "../hooks/useWorkflowProject";
import { resetExecutionLoadsForTests } from "../hooks/useWorkflowExecution";
import { resetEvaluationLoadsForTests } from "../hooks/useWorkflowEvaluation";
import { useWorkflowExecutionStore } from "../stores/workflowExecutionStore";
import { useWorkflowEvaluationStore } from "../stores/workflowEvaluationStore";
import {
  workflowEvent,
  workflowExecution,
  workflowEvaluation,
  workflowSnapshot,
} from "../test/fixtures";
import { HomePage } from "./HomePage";
import { WorkflowPage } from "./WorkflowPage";

class QuietEventSource {
  close = vi.fn();
  onmessage = null;
  onopen = null;
  onerror = null;
  addEventListener = vi.fn();
  removeEventListener = vi.fn();
}

function LocationProbe() {
  const location = useLocation();
  return <output aria-label="current-location">{location.search}</output>;
}

describe("pages", () => {
  beforeEach(() => {
    resetEventStreamsForTests();
    resetWorkflowLoadsForTests();
    resetProjectLoadsForTests();
    resetExecutionLoadsForTests();
    resetEvaluationLoadsForTests();
    useWorkflowStore.getState().reset();
    useWorkflowExecutionStore.setState({
      key: null,
      execution: null,
      isLoading: false,
      isRefreshing: false,
      error: null,
    });
    useWorkflowEvaluationStore.setState({
      key: null,
      evaluation: null,
      isLoading: false,
      isRefreshing: false,
      error: null,
    });
    vi.stubGlobal("EventSource", QuietEventSource);
  });

  it("deduplicates initial requests under StrictMode", async () => {
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(workflowSnapshot());
    vi.spyOn(workflows, "getCompleteWorkflowHistory").mockResolvedValue({
      thread_id: "thread-1",
      branch_id: "original",
      events: [],
      last_sequence: 0,
      has_more: false,
    });
    render(
      <StrictMode>
        <MemoryRouter initialEntries={["/workflows/thread-1"]}>
          <Routes>
            <Route path="/workflows/:threadId" element={<WorkflowPage />} />
          </Routes>
        </MemoryRouter>
      </StrictMode>,
    );
    await screen.findByRole("heading", { level: 1, name: "health-api" });
    expect(workflows.getWorkflow).toHaveBeenCalledTimes(1);
    expect(workflows.getCompleteWorkflowHistory).toHaveBeenCalledTimes(1);
  });

  it("creates and navigates to the workflow URL", async () => {
    vi.spyOn(workflows, "createWorkflow").mockResolvedValue({
      thread_id: "created-thread",
      status: "running",
      workflow_url: "/api/workflows/created-thread",
      events_url: "/api/workflows/created-thread/events",
    });
    render(
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route
            path="/workflows/:threadId"
            element={<div>Workflow destination</div>}
          />
        </Routes>
      </MemoryRouter>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Crear workflow" }));
    expect(await screen.findByText("Workflow destination")).toBeVisible();
  });

  it("loads snapshot and durable history", async () => {
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(
      workflowSnapshot({ terminal_status: "completed" }),
    );
    vi.spyOn(workflows, "getCompleteWorkflowHistory").mockResolvedValue({
      thread_id: "thread-1",
      branch_id: "original",
      events: [
        workflowEvent(1),
        workflowEvent(2, {
          type: "approval_required",
          status: "waiting",
          stage: "create_project",
          data: {
            operation: "create_project",
            tool_name: "filesystem__create_project_structure",
          },
        }),
        workflowEvent(3, {
          type: "approval_granted",
          status: "completed",
          stage: "create_project",
          data: {
            operation: "create_project",
            tool_name: "filesystem__create_project_structure",
          },
        }),
        workflowEvent(4, {
          type: "workflow_completed",
          status: "completed",
        }),
      ],
      last_sequence: 4,
      has_more: false,
    });
    render(
      <MemoryRouter initialEntries={["/workflows/thread-1"]}>
        <Routes>
          <Route path="/workflows/:threadId" element={<WorkflowPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(
      await screen.findByRole("heading", { level: 1, name: "health-api" }),
    ).toBeVisible();
    expect(screen.getByText("#004")).toBeVisible();
    expect(
      within(
        screen.getByRole("region", { name: "Timeline de eventos" }),
      ).getByText("Workflow completado"),
    ).toBeVisible();
    expect(
      screen.queryByLabelText("Evento en ejecución"),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Evento iniciado")).toBeVisible();
    expect(
      screen.queryByLabelText("Evento en espera"),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Evento resuelto")).toBeVisible();
  });

  it("does not keep SSE open for a completed workflow", async () => {
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(
      workflowSnapshot({ terminal_status: "completed" }),
    );
    vi.spyOn(workflows, "getCompleteWorkflowHistory").mockResolvedValue({
      thread_id: "thread-1",
      branch_id: "original",
      events: [],
      last_sequence: 0,
      has_more: false,
    });
    render(
      <MemoryRouter initialEntries={["/workflows/thread-1"]}>
        <Routes>
          <Route path="/workflows/:threadId" element={<WorkflowPage />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByRole("heading", { level: 1, name: "health-api" });
    await waitFor(() =>
      expect(useWorkflowStore.getState().snapshot?.terminal_status).toBe(
        "completed",
      ),
    );
    expect(useWorkflowStore.getState().events).toEqual([]);
    expect(useWorkflowStore.getState().connectionStatus).toBe("completed");
  });

  it("shows a readable 404 error", async () => {
    const error = Object.assign(new Error("missing"), {
      statusCode: 404,
      retryable: false,
      toUiError: () => ({
        title: "Workflow no encontrado",
        message: "El workflow solicitado no existe.",
        statusCode: 404,
        retryable: false,
      }),
    });
    vi.spyOn(workflows, "getWorkflow").mockRejectedValue(error);
    vi.spyOn(workflows, "getCompleteWorkflowHistory").mockRejectedValue(error);
    render(
      <MemoryRouter initialEntries={["/workflows/missing"]}>
        <Routes>
          <Route path="/workflows/:threadId" element={<WorkflowPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(
      await screen.findByText("No se pudieron recuperar los datos"),
    ).toBeVisible();
  });

  it("shows the project tab in workflow detail", async () => {
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(workflowSnapshot());
    vi.spyOn(workflows, "getCompleteWorkflowHistory").mockResolvedValue({
      thread_id: "thread-1",
      branch_id: "original",
      events: [],
      last_sequence: 0,
      has_more: false,
    });
    render(
      <MemoryRouter initialEntries={["/workflows/thread-1"]}>
        <Routes>
          <Route path="/workflows/:threadId" element={<WorkflowPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(
      await screen.findByRole("button", { name: "Proyecto" }),
    ).toBeVisible();
  });

  it("shows the Git tab in workflow detail", async () => {
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(workflowSnapshot());
    vi.spyOn(workflows, "getCompleteWorkflowHistory").mockResolvedValue({
      thread_id: "thread-1", branch_id: "original", events: [], last_sequence: 0, has_more: false,
    });
    render(<MemoryRouter initialEntries={["/workflows/thread-1"]}><Routes><Route path="/workflows/:threadId" element={<WorkflowPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByRole("button", { name: "Git" })).toBeVisible();
  });

  it("restores the project tab from the query string", async () => {
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(
      workflowSnapshot({ terminal_status: "completed" }),
    );
    vi.spyOn(workflows, "getCompleteWorkflowHistory").mockResolvedValue({
      thread_id: "thread-1",
      branch_id: "original",
      events: [],
      last_sequence: 0,
      has_more: false,
    });
    vi.spyOn(workflows, "getWorkflowProject").mockResolvedValue({
      thread_id: "thread-1",
      project_name: "health-api",
      project_exists: false,
      relative_project_path: null,
      total_files: 0,
      total_directories: 0,
      total_size_bytes: 0,
      generated_files: [],
      updated_files: [],
      detected_framework: null,
      detected_test_framework: null,
      created_at: null,
      updated_at: null,
    });
    render(
      <MemoryRouter initialEntries={["/workflows/thread-1?tab=project"]}>
        <Routes>
          <Route path="/workflows/:threadId" element={<WorkflowPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(
      await screen.findByText(/El proyecto .* no fue creado\./),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Proyecto" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("shows the execution tab in workflow detail", async () => {
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(workflowSnapshot());
    vi.spyOn(workflows, "getCompleteWorkflowHistory").mockResolvedValue({
      thread_id: "thread-1",
      branch_id: "original",
      events: [],
      last_sequence: 0,
      has_more: false,
    });
    render(
      <MemoryRouter initialEntries={["/workflows/thread-1"]}>
        <Routes>
          <Route path="/workflows/:threadId" element={<WorkflowPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(
      await screen.findByRole("button", { name: "Ejecución" }),
    ).toBeVisible();
  });

  it("shows the evaluation tab in workflow detail", async () => {
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(workflowSnapshot());
    vi.spyOn(workflows, "getCompleteWorkflowHistory").mockResolvedValue({
      thread_id: "thread-1",
      branch_id: "original",
      events: [],
      last_sequence: 0,
      has_more: false,
    });
    render(
      <MemoryRouter initialEntries={["/workflows/thread-1"]}>
        <Routes>
          <Route path="/workflows/:threadId" element={<WorkflowPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(
      await screen.findByRole("button", { name: "Evaluación" }),
    ).toBeVisible();
  });

  it("restores evaluation category and finding after F5", async () => {
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(
      workflowSnapshot({ terminal_status: "completed" }),
    );
    vi.spyOn(workflows, "getCompleteWorkflowHistory").mockResolvedValue({
      thread_id: "thread-1",
      branch_id: "original",
      events: [],
      last_sequence: 0,
      has_more: false,
    });
    vi.spyOn(workflows, "getWorkflowEvaluation").mockResolvedValue(
      workflowEvaluation(),
    );
    render(
      <MemoryRouter
        initialEntries={[
          "/workflows/thread-1?tab=evaluation&category=testing&finding=TEST_WARNINGS",
        ]}
      >
        <Routes>
          <Route path="/workflows/:threadId" element={<WorkflowPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByText("Pytest reportó 2 warnings.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Evaluación" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("button", { name: /Testing/ })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("preserves branch while selecting an evaluation category", async () => {
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(
      workflowSnapshot({ terminal_status: "completed" }),
    );
    vi.spyOn(workflows, "getCompleteWorkflowHistory").mockResolvedValue({
      thread_id: "thread-1",
      branch_id: "fork-1",
      events: [],
      last_sequence: 0,
      has_more: false,
    });
    vi.spyOn(workflows, "getWorkflowEvaluation").mockResolvedValue(
      workflowEvaluation({ branch_id: "fork-1", lineage: "fork" }),
    );
    render(
      <MemoryRouter
        initialEntries={[
          "/workflows/thread-1?tab=evaluation&branch_id=fork-1",
        ]}
      >
        <Routes>
          <Route
            path="/workflows/:threadId"
            element={<><WorkflowPage /><LocationProbe /></>}
          />
        </Routes>
      </MemoryRouter>,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: /Testing/ }),
    );
    expect(screen.getByLabelText("current-location")).toHaveTextContent(
      "?tab=evaluation&branch_id=fork-1&category=testing",
    );
  });

  it("restores execution branch and expanded task from the query string", async () => {
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(
      workflowSnapshot({ terminal_status: "completed" }),
    );
    vi.spyOn(workflows, "getCompleteWorkflowHistory").mockResolvedValue({
      thread_id: "thread-1",
      branch_id: "original",
      events: [],
      last_sequence: 0,
      has_more: false,
    });
    const execution = workflowExecution({
      branch_id: "fork-1",
      lineage: "fork",
    });
    const executionRequest = vi
      .spyOn(workflows, "getWorkflowExecution")
      .mockResolvedValue(execution);
    render(
      <MemoryRouter
        initialEntries={[
          "/workflows/thread-1?tab=execution&branch_id=fork-1&section=tasks&task=task-analysis",
        ]}
      >
        <Routes>
          <Route path="/workflows/:threadId" element={<WorkflowPage />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(
      await screen.findByText("Identificar objetivo y criterios."),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Ejecución" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(executionRequest).toHaveBeenCalledWith(
      "thread-1",
      "fork-1",
      expect.any(AbortSignal),
    );
  });

  it("navigates from QA to Timeline while preserving branch and event", async () => {
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(
      workflowSnapshot({ terminal_status: "completed" }),
    );
    vi.spyOn(workflows, "getCompleteWorkflowHistory").mockResolvedValue({
      thread_id: "thread-1",
      branch_id: "original",
      events: [workflowEvent(6, {
        event_id: "event-6",
        type: "test_run_completed",
        stage: "testing_repair",
        status: "completed",
      })],
      last_sequence: 6,
      has_more: false,
    });
    vi.spyOn(workflows, "getWorkflowExecution").mockResolvedValue(
      workflowExecution({
        branch_id: "fork-1",
        lineage: "fork",
      }),
    );
    render(
      <MemoryRouter
        initialEntries={[
          "/workflows/thread-1?tab=execution&branch_id=fork-1&task=task-testing",
        ]}
      >
        <Routes>
          <Route
            path="/workflows/:threadId"
            element={<><WorkflowPage /><LocationProbe /></>}
          />
        </Routes>
      </MemoryRouter>,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: /Ver en Timeline/ }),
    );
    expect(screen.getByLabelText("current-location")).toHaveTextContent(
      "?tab=timeline&branch_id=fork-1&event=event-6",
    );
  });
});
