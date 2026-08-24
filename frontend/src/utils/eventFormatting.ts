import type { WorkflowEvent } from "../api/types";

const EVENT_LABELS: Partial<Record<WorkflowEvent["type"], string>> = {
  workflow_started: "Workflow iniciado",
  workflow_resumed: "Workflow reanudado",
  workflow_forked: "Rama alternativa iniciada",
  planning_started: "Análisis y planificación iniciados",
  planning_completed: "Plan validado",
  planning_failed: "Planificación fallida",
  workspace_inspection_started: "Inspección del workspace iniciada",
  workspace_inspection_completed: "Workspace inspeccionado",
  implementation_started: "Implementación iniciada",
  implementation_validation_completed: "Implementación validada",
  implementation_completed: "Implementación completada",
  implementation_failed: "Implementación fallida",
  approval_required: "Aprobación requerida",
  approval_granted: "Operación aprobada",
  approval_rejected: "Operación rechazada",
  tool_started: "Tool iniciada",
  tool_completed: "Tool completada",
  tool_failed: "Tool fallida",
  test_run_started: "Pruebas iniciadas",
  test_run_completed: "Pruebas completadas",
  test_run_failed: "Pruebas fallidas",
  repair_started: "Reparación iniciada",
  repair_completed: "Reparación completada",
  repair_failed: "Reparación fallida",
  workflow_completed: "Workflow completado",
  workflow_failed: "Workflow fallido",
  supervisor_loop_detected: "Loop del supervisor detectado",
};

function textValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

export interface FormattedWorkflowEvent {
  title: string;
  detail: string | null;
  time: string;
}

export function formatWorkflowEvent(
  event: WorkflowEvent,
): FormattedWorkflowEvent {
  const base =
    EVENT_LABELS[event.type] ??
    event.type.replaceAll("_", " ").replace(/^\w/, (value) => value.toUpperCase());
  const operation = textValue(event.data.operation);
  const tool = textValue(event.data.tool) ?? textValue(event.data.tool_name);
  const target = textValue(event.data.executed_target);
  const summary = textValue(event.data.summary);
  let title = base;
  if (event.type === "approval_required" && operation) {
    title = `${base}: ${operation.replaceAll("_", " ")}`;
  } else if (event.type.startsWith("tool_") && tool) {
    title = `${base}: ${tool}`;
  }
  return {
    title,
    detail: summary ?? target ?? operation ?? event.message,
    time: new Intl.DateTimeFormat("es", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(new Date(event.timestamp)),
  };
}

export function formatStageName(stage: string | null): string {
  if (!stage) return "Workflow";
  return stage
    .replace("testing_repair", "Testing")
    .replace("inspect_workspace", "Workspace")
    .replaceAll("_", " ")
    .replace(/^\w/, (value) => value.toUpperCase());
}
