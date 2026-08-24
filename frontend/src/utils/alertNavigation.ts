import type { WorkflowAlert } from "../api/alerts";

export function workflowAlertUrl(alert: WorkflowAlert, tab?: "evaluation" | "timeline" | "execution" | "project") {
  const params = new URLSearchParams();
  if (tab) params.set("tab", tab);
  if (alert.branch_id !== "original") params.set("branch_id", alert.branch_id);
  if (tab === "timeline" && alert.primary_event_id) params.set("event", alert.primary_event_id);
  if (tab === "execution" && alert.related_task_id) params.set("task", alert.related_task_id);
  if (tab === "project" && alert.related_file) params.set("file", alert.related_file);
  const query = params.toString();
  return `/workflows/${encodeURIComponent(alert.thread_id)}${query ? `?${query}` : ""}`;
}
