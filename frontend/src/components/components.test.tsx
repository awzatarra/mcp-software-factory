import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as workflows from "../api/workflows";
import { ApiClientError } from "../api/client";
import { useWorkflowStore } from "../stores/workflowStore";
import { workflowEvent, workflowSnapshot } from "../test/fixtures";
import { deriveStageProgress } from "../utils/stageDerivation";
import { ApprovalPanel } from "./ApprovalPanel";
import { ConnectionStatus } from "./ConnectionStatus";
import { EventTimeline } from "./EventTimeline";
import { StageProgress } from "./StageProgress";
import { WorkflowForm } from "./WorkflowForm";
import { WorkflowHeader } from "./WorkflowHeader";
import { WorkflowSummary } from "./WorkflowSummary";

describe("workflow components", () => {
  beforeEach(() => useWorkflowStore.getState().reset());

  it("rejects an empty workflow form", async () => {
    const submit = vi.fn();
    render(<WorkflowForm onSubmit={submit} />);
    const textarea = screen.getByLabelText("Requerimiento");
    await userEvent.clear(textarea);
    fireEvent.submit(textarea.closest("form")!);
    expect(await screen.findByText(/Ingresa un requerimiento/)).toBeVisible();
    expect(submit).not.toHaveBeenCalled();
  });

  it("submits a valid workflow once", async () => {
    const submit = vi.fn().mockResolvedValue(undefined);
    render(<WorkflowForm onSubmit={submit} />);
    await userEvent.click(screen.getByRole("button", { name: "Crear workflow" }));
    expect(submit).toHaveBeenCalledTimes(1);
  });

  it("renders timeline events", () => {
    render(
      <EventTimeline
        events={[
          workflowEvent(1),
          workflowEvent(2, {
            type: "planning_completed",
            status: "completed",
          }),
        ]}
      />,
    );
    expect(screen.getByText("#001")).toBeVisible();
    expect(screen.getByText("Plan validado")).toBeVisible();
  });

  it("shows a selected technical event and activates technical mode", () => {
    const selected = workflowEvent(7, {
      type: "planning_completed",
      source: "langgraph.update",
      stage: "planning",
    });
    render(
      <EventTimeline
        events={[selected]}
        selectedEventId={selected.event_id}
      />,
    );
    expect(screen.getByText("#007")).toBeVisible();
    expect(screen.getByRole("button", { name: "Técnico" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("focuses and scrolls to the selected event", () => {
    const selected = workflowEvent(8, {
      type: "test_run_completed",
      stage: "testing_repair",
      status: "completed",
    });
    render(
      <EventTimeline
        events={[selected]}
        selectedEventId={selected.event_id}
      />,
    );
    const item = document.getElementById(`event-${selected.event_id}`)!;
    expect(item).toHaveFocus();
    expect(item.scrollIntoView).toHaveBeenCalledWith({
      behavior: "smooth",
      block: "center",
    });
  });

  it("highlights the selected event and clears it after timeout", async () => {
    vi.useFakeTimers();
    const selected = workflowEvent(9, {
      type: "test_run_completed",
      stage: "testing_repair",
      status: "completed",
    });
    render(
      <EventTimeline
        events={[selected]}
        selectedEventId={selected.event_id}
      />,
    );
    const item = document.getElementById(`event-${selected.event_id}`)!;
    expect(item).toHaveClass("event-selected");
    await act(async () => vi.advanceTimersByTimeAsync(3_001));
    expect(item).not.toHaveClass("event-selected");
    vi.useRealTimers();
  });

  it("reports a selected event missing from history", () => {
    render(
      <EventTimeline
        events={[workflowEvent(1)]}
        selectedEventId="missing-event"
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "El evento seleccionado ya no está disponible",
    );
  });

  it("keeps summary mode for a selected summary event", () => {
    const selected = workflowEvent(10, {
      type: "test_run_completed",
      stage: "testing_repair",
      status: "completed",
    });
    render(
      <EventTimeline
        events={[selected]}
        selectedEventId={selected.event_id}
      />,
    );
    expect(screen.getByRole("button", { name: "Resumen" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("renders historical starts statically for a terminal workflow", () => {
    render(
      <EventTimeline
        snapshot={workflowSnapshot({ terminal_status: "completed" })}
        events={[
          workflowEvent(1),
          workflowEvent(2, {
            type: "workflow_completed",
            status: "completed",
          }),
        ]}
      />,
    );
    expect(
      screen.queryByLabelText("Evento en ejecución"),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Evento iniciado")).toBeVisible();
    expect(screen.getByLabelText("Evento completado")).toBeVisible();
    expect(screen.getByText("running")).toBeVisible();
  });

  it("updates a started item when its completion arrives without F5", () => {
    const started = workflowEvent(1, {
      type: "test_run_started",
      stage: "testing",
      status: "running",
      data: { framework: "pytest", attempt: 0 },
    });
    const view = render(
      <EventTimeline events={[started]} snapshot={workflowSnapshot()} />,
    );
    expect(screen.getByLabelText("Evento en ejecución")).toBeVisible();
    view.rerender(
      <EventTimeline
        events={[
          started,
          workflowEvent(2, {
            type: "test_run_completed",
            stage: "testing",
            status: "completed",
            data: { framework: "pytest", attempt: 0 },
          }),
        ]}
        snapshot={workflowSnapshot()}
      />,
    );
    expect(
      screen.queryByLabelText("Evento en ejecución"),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Evento iniciado")).toBeVisible();
  });

  it("shows exactly one current approval as waiting", () => {
    const required = workflowEvent(10, {
      type: "approval_required",
      status: "waiting",
      stage: "run_tests",
      data: {
        branch_id: "original",
        lineage: "original",
        operation: "run_tests",
        tool_name: "testing__run_tests",
        attempt: 0,
      },
    });
    render(
      <EventTimeline
        events={[required]}
        snapshot={workflowSnapshot({
          interrupted: true,
          pending_operation: "run_tests",
          pending_tool: "testing__run_tests",
        })}
      />,
    );
    expect(screen.getAllByLabelText("Evento en espera")).toHaveLength(1);
    expect(screen.getByText("waiting")).toBeVisible();
  });

  it("updates a granted approval to resolved without F5", async () => {
    const required = workflowEvent(10, {
      type: "approval_required",
      status: "waiting",
      stage: "run_tests",
      data: {
        branch_id: "original",
        lineage: "original",
        operation: "run_tests",
        tool_name: "testing__run_tests",
        attempt: 0,
      },
    });
    const granted = workflowEvent(11, {
      type: "approval_granted",
      status: "completed",
      stage: "run_tests",
      data: {
        branch_id: "original",
        lineage: "original",
        operation: "run_tests",
        tool_name: "testing__run_tests",
        attempt: 0,
      },
    });
    const view = render(
      <EventTimeline
        events={[required]}
        snapshot={workflowSnapshot({
          interrupted: true,
          pending_operation: "run_tests",
          pending_tool: "testing__run_tests",
        })}
      />,
    );
    expect(screen.getByLabelText("Evento en espera")).toBeVisible();
    view.rerender(
      <EventTimeline
        events={[required, granted]}
        snapshot={workflowSnapshot({ terminal_status: "running" })}
      />,
    );
    expect(
      screen.queryByLabelText("Evento en espera"),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Evento resuelto")).toBeVisible();
    expect(screen.getByText("resolved")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Técnico" }));
    expect(screen.getByText("waiting")).toBeVisible();
  });

  it("updates a rejected approval to rejected without F5", () => {
    const required = workflowEvent(10, {
      type: "approval_required",
      status: "waiting",
      stage: "create_project",
      data: {
        branch_id: "original",
        lineage: "original",
        operation: "create_project",
        tool_name: "filesystem__create_project_structure",
        attempt: 0,
      },
    });
    const rejected = workflowEvent(11, {
      type: "approval_rejected",
      status: "failed",
      stage: "create_project",
      data: {
        branch_id: "original",
        lineage: "original",
        operation: "create_project",
        tool_name: "filesystem__create_project_structure",
        attempt: 0,
      },
    });
    render(
      <EventTimeline
        events={[required, rejected]}
        snapshot={workflowSnapshot({ terminal_status: "user_cancelled" })}
      />,
    );
    expect(screen.getByLabelText("Evento rechazado")).toBeVisible();
    expect(
      screen.queryByLabelText("Evento en espera"),
    ).not.toBeInTheDocument();
    expect(screen.getByText("rejected")).toBeVisible();
  });

  it("shows technical planning synchronization only in technical mode", async () => {
    render(
      <EventTimeline
        events={[
          workflowEvent(10, {
            source: "planning_subgraph",
            type: "planning_completed",
            status: "completed",
          }),
          workflowEvent(11, {
            source: "langgraph.update",
            type: "planning_completed",
            status: "completed",
          }),
        ]}
      />,
    );
    expect(screen.getAllByText("Plan validado")).toHaveLength(1);
    expect(screen.getByText("1 de 2 eventos")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Técnico" }));
    expect(screen.getAllByText("Plan validado")).toHaveLength(2);
    expect(screen.getByText("langgraph.update")).toBeVisible();
    expect(screen.getAllByText("planning_completed")).toHaveLength(2);
  });

  it("keeps original running status in technical mode without animating it", async () => {
    render(
      <EventTimeline
        snapshot={workflowSnapshot({ terminal_status: "completed" })}
        events={[
          workflowEvent(1, {
            type: "stage_started",
            status: "running",
            source: "parent_graph",
            data: { node: "planning", namespace: ["parent", "planning"] },
          }),
        ]}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Técnico" }));
    expect(screen.getByText("running")).toBeVisible();
    expect(screen.getByText("parent_graph")).toBeVisible();
    expect(screen.getByText("stage_started")).toBeVisible();
    expect(
      screen.queryByLabelText("Evento en ejecución"),
    ).not.toBeInTheDocument();
  });

  it("renders connection states", () => {
    const { rerender } = render(<ConnectionStatus status="connecting" />);
    expect(screen.getByText("Conectando…")).toBeVisible();
    rerender(<ConnectionStatus status="connected" />);
    expect(screen.getByText("Eventos en vivo")).toBeVisible();
  });

  it("renders stage progress accessibly", () => {
    render(
      <StageProgress
        stages={deriveStageProgress(
          [workflowEvent(1, { type: "planning_completed", status: "completed" })],
          workflowSnapshot(),
        )}
      />,
    );
    expect(screen.getByRole("list", { name: "Progreso del workflow" })).toBeVisible();
    expect(screen.getByText("Planning")).toBeVisible();
  });

  it("renders a completed F5 snapshot without stage spinners", () => {
    const snapshot = workflowSnapshot({ terminal_status: "completed" });
    const { container } = render(
      <StageProgress
        stages={deriveStageProgress(
          [
            workflowEvent(1, { type: "planning_started" }),
            workflowEvent(2, {
              type: "test_run_started",
              stage: "testing",
            }),
          ],
          snapshot,
        )}
      />,
    );
    expect(screen.getAllByText("completed")).toHaveLength(5);
    expect(container.querySelector(".spin")).toBeNull();
  });

  it("shows the durable test summary after completion", () => {
    render(
      <WorkflowSummary
        snapshot={workflowSnapshot({
          terminal_status: "completed",
          testing: {
            executed: true,
            passed: true,
            final_test_result_summary: "1 passed, 2 warnings",
          },
        })}
      />,
    );
    expect(screen.getByText("1 passed, 2 warnings")).toBeVisible();
  });

  it("hides approval panel without pending operation", () => {
    const { container } = render(
      <ApprovalPanel snapshot={workflowSnapshot()} events={[]} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("does not derive ApprovalPanel from historical approval events", () => {
    const { container } = render(
      <ApprovalPanel
        snapshot={workflowSnapshot({
          interrupted: false,
          terminal_status: "completed",
        })}
        events={[
          workflowEvent(2, {
            type: "approval_required",
            status: "waiting",
            data: {
              operation: "create_project",
              tool_name: "filesystem__create_project_structure",
            },
          }),
        ]}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("shows sanitized approval preview", () => {
    render(
      <ApprovalPanel
        snapshot={workflowSnapshot({
          interrupted: true,
          pending_operation: "create_project",
          pending_tool: "filesystem__create_project_structure",
        })}
        events={[
          workflowEvent(2, {
            type: "approval_required",
            status: "waiting",
            data: { preview: { total_files: 3, files: ["main.py"] } },
          }),
        ]}
      />,
    );
    expect(screen.getByRole("heading", { name: "Aprobación requerida" })).toBeVisible();
    expect(screen.getByText(/total_files/)).toBeVisible();
  });

  it("approves once and disables both buttons while pending", async () => {
    let resolveApproval: () => void = () => undefined;
    vi.spyOn(workflows, "approveWorkflow").mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveApproval = () =>
            resolve({
              thread_id: "thread-1",
              accepted: true,
              operation: "create_project",
              tool_name: "filesystem__create_project_structure",
              status: "running",
            });
        }),
    );
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(workflowSnapshot());
    render(
      <ApprovalPanel
        snapshot={workflowSnapshot({
          interrupted: true,
          pending_operation: "create_project",
          pending_tool: "filesystem__create_project_structure",
        })}
        events={[]}
      />,
    );
    const approve = screen.getByRole("button", { name: "Aprobar" });
    const reject = screen.getByRole("button", { name: "Rechazar" });
    await userEvent.click(approve);
    await userEvent.click(approve);
    expect(workflows.approveWorkflow).toHaveBeenCalledTimes(1);
    expect(approve).toBeDisabled();
    expect(reject).toBeDisabled();
    await act(async () => {
      resolveApproval();
      await Promise.resolve();
    });
    act(() => {
      useWorkflowStore.getState().appendEvent(
        workflowEvent(3, {
          type: "approval_granted",
          status: "completed",
          data: { operation: "create_project" },
        }),
      );
    });
    expect(useWorkflowStore.getState().approvalLock).toBeNull();
  });

  it("calls reject endpoint", async () => {
    vi.spyOn(workflows, "rejectWorkflow").mockResolvedValue({
      thread_id: "thread-1",
      accepted: true,
      operation: "create_project",
      tool_name: "filesystem__create_project_structure",
      status: "running",
    });
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(workflowSnapshot());
    render(
      <ApprovalPanel
        snapshot={workflowSnapshot({
          interrupted: true,
          pending_operation: "create_project",
          pending_tool: "filesystem__create_project_structure",
        })}
        events={[]}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Rechazar" }));
    expect(workflows.rejectWorkflow).toHaveBeenCalledTimes(1);
  });

  it("keeps approval locked after 202 while the operation is still pending", async () => {
    vi.spyOn(workflows, "approveWorkflow").mockResolvedValue({
      thread_id: "thread-1",
      accepted: true,
      operation: "create_project",
      tool_name: "filesystem__create_project_structure",
      status: "running",
    });
    const pending = workflowSnapshot({
      interrupted: true,
      pending_operation: "create_project",
      pending_tool: "filesystem__create_project_structure",
    });
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(pending);
    render(<ApprovalPanel snapshot={pending} events={[]} />);
    await userEvent.click(screen.getByRole("button", { name: "Aprobar" }));
    await waitFor(() => expect(workflows.approveWorkflow).toHaveBeenCalled());
    expect(useWorkflowStore.getState().approvalLock?.operation).toBe(
      "create_project",
    );
    expect(workflows.getWorkflow).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Aprobar" })).toBeDisabled();
  });

  it("refreshes and unlocks after the approval safety timeout", async () => {
    vi.useFakeTimers();
    const pending = workflowSnapshot({
      interrupted: true,
      pending_operation: "create_project",
      pending_tool: "filesystem__create_project_structure",
    });
    vi.spyOn(workflows, "approveWorkflow").mockResolvedValue({
      thread_id: "thread-1",
      accepted: true,
      operation: "create_project",
      tool_name: "filesystem__create_project_structure",
      status: "running",
    });
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(pending);
    render(<ApprovalPanel snapshot={pending} events={[]} />);
    fireEvent.click(screen.getByRole("button", { name: "Aprobar" }));
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(useWorkflowStore.getState().approvalLock).not.toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(workflows.getWorkflow).toHaveBeenCalledTimes(1);
    expect(useWorkflowStore.getState().approvalLock).toBeNull();
    vi.useRealTimers();
  });

  it("treats 409 as a scoped noncritical conflict and refreshes", async () => {
    const pending = workflowSnapshot({
      interrupted: true,
      pending_operation: "create_project",
      pending_tool: "filesystem__create_project_structure",
    });
    vi.spyOn(workflows, "approveWorkflow").mockRejectedValue(
      new ApiClientError({
        title: "Error de API",
        message: "La operación ya fue resuelta o no está pendiente.",
        statusCode: 409,
        retryable: false,
      }),
    );
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(
      workflowSnapshot({ terminal_status: "completed" }),
    );
    render(<ApprovalPanel snapshot={pending} events={[]} />);
    await userEvent.click(screen.getByRole("button", { name: "Aprobar" }));
    await waitFor(() =>
      expect(useWorkflowStore.getState().error?.title).toBe(
        "Operación ya resuelta",
      ),
    );
    expect(useWorkflowStore.getState().error?.scope).toBe("approval");
    expect(useWorkflowStore.getState().error?.retryable).toBe(false);
    expect(workflows.getWorkflow).toHaveBeenCalledTimes(1);
    expect(useWorkflowStore.getState().approvalLock).toBeNull();
  });

  it("copies workflow URL", async () => {
    render(
      <MemoryRouter>
        <WorkflowHeader
          threadId="thread-123456"
          snapshot={workflowSnapshot()}
          connectionStatus="connected"
        />
      </MemoryRouter>,
    );
    await userEvent.click(screen.getByTitle("Copiar enlace del workflow"));
    expect(navigator.clipboard.writeText).toHaveBeenCalled();
  });
});
