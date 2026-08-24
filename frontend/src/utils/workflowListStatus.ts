import type { WorkflowListItem, WorkflowListStatus } from "../api/types";

export function workflowDisplayStatus(
  item: WorkflowListItem,
): WorkflowListStatus {
  if (item.interrupted && item.pending_operation) return "waiting";
  if (item.terminal_status === "completed") return "completed";
  if (item.terminal_status === "failed") return "failed";
  if (item.terminal_status === "running") return "running";
  return "pending";
}
