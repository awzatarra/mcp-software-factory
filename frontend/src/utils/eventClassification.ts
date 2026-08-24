import type { WorkflowEvent } from "../api/types";

export type WorkflowEventClassification = "functional" | "technical";

export function classifyWorkflowEvent(
  event: WorkflowEvent,
): WorkflowEventClassification {
  if (
    event.source === "langgraph.update" &&
    event.type === "planning_completed"
  ) {
    return "technical";
  }
  return "functional";
}

export function shouldShowInSummary(event: WorkflowEvent): boolean {
  return classifyWorkflowEvent(event) === "functional";
}
