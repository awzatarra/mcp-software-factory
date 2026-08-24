import { apiFetch } from "./client";

export type DashboardCountItem = { key: string; label: string; count: number; percentage: number | null };
export type DashboardFilters = { dateFrom?: string; dateTo?: string; status?: string; projectName?: string; workflowIntent?: string; framework?: string; grade?: string; branchScope?: "original" | "all"; scoringVersion?: string; timezone?: string };
export type DashboardSummary = {
  date_from: string; date_to: string; timezone: string; branch_scope: "original" | "all";
  workflow_counts: { total: number; completed: number; failed: number; running: number; waiting: number; pending: number; cancelled: number; success_rate_percent: number | null; failure_rate_percent: number | null };
  scores: { evaluated_workflows: number; unevaluated_workflows: number; average_score: number | null; median_score: number | null; min_score: number | null; max_score: number | null; excellent: number; good: number; acceptable: number; poor: number; critical: number; provisional: number; scoring_versions: string[] };
  durations: { workflows_with_duration: number; discarded_workflows: number; average_wall_clock_seconds: number | null; median_wall_clock_seconds: number | null; p50_wall_clock_seconds: number | null; p90_wall_clock_seconds: number | null; p95_wall_clock_seconds: number | null; average_active_seconds: number | null; average_approval_wait_seconds: number | null; approval_wait_percent: number | null; average_planning_seconds: number | null; average_implementation_seconds: number | null; average_testing_seconds: number | null; average_repair_seconds: number | null };
  testing: { executed: number; passed: number; failed: number; not_executed: number; pass_rate_percent: number | null; workflows_with_warnings: number; total_warnings: number; average_warnings: number | null; repair_required: number; repair_successful: number; repair_failed: number; repair_success_rate_percent: number | null; average_repair_attempts: number | null };
  approvals: { total_approvals_requested: number; total_approvals_granted: number; total_approvals_rejected: number; workflows_with_pending_approval: number; average_approval_wait_seconds: number | null; longest_approval_wait_seconds: number | null; most_requested_operations: DashboardCountItem[] };
  frameworks: DashboardCountItem[]; intents: DashboardCountItem[]; top_findings: DashboardCountItem[]; top_recommendations: DashboardCountItem[];
  calculated_at: string; source_updated_at: string | null; data_complete: boolean;
};
export interface DashboardTimeSeriesPoint {
  bucket_start: string;
  bucket_end: string;
  value: number | null;
  count: number;
  numerator: number | null;
  denominator: number | null;
}
export type DashboardTimeSeries = { metric: string; interval: string; timezone: string; points: DashboardTimeSeriesPoint[]; source_updated_at: string | null };
export type AgentMetric = { agent: string; total_tasks: number; completed_tasks: number; failed_tasks: number; waiting_tasks: number; skipped_tasks: number; completion_rate_percent: number | null; average_attempts: number | null; average_duration_seconds: number | null; related_workflows: number; related_files: number; findings_count: number; average_score: number | null };
export type AttentionItem = { thread_id: string; branch_id: string; project_name: string | null; terminal_status: string; score: number | null; grade: string | null; severity: "critical" | "error" | "warning"; reasons: string[]; finding_codes: string[]; pending_operation: string | null; age_seconds: number; updated_at: string | null };
export type ActivityItem = { event_id: string; thread_id: string; branch_id: string; project_name: string | null; type: string; status: string; message: string; timestamp: string; related_event_id: string | null };
export type Page<T> = { items: T[]; total: number; limit: number; offset: number; has_more: boolean };

function query(filters: DashboardFilters, extra: Record<string, string> = {}) {
  const params = new URLSearchParams(extra);
  const values: Record<string, string | undefined> = { date_from: filters.dateFrom, date_to: filters.dateTo, status: filters.status, project_name: filters.projectName, workflow_intent: filters.workflowIntent, framework: filters.framework, grade: filters.grade, branch_scope: filters.branchScope, scoring_version: filters.scoringVersion, timezone: filters.timezone };
  Object.entries(values).forEach(([key, value]) => value && params.set(key, value));
  return params.toString();
}

export const getDashboardSummary = (filters: DashboardFilters, signal?: AbortSignal) => apiFetch<DashboardSummary>(`/api/dashboard/summary?${query(filters)}`, { signal });
export function normalizeDashboardTimeSeries(response: DashboardTimeSeries): DashboardTimeSeries {
  const points = response.points.filter((point) => {
    const start = Date.parse(point.bucket_start);
    const end = Date.parse(point.bucket_end);
    const valid = Number.isFinite(start) && Number.isFinite(end) && end > start;
    if (!valid) console.error("Invalid dashboard timeseries bucket", point);
    return valid;
  });
  if (response.points.length > 0 && points.length === 0) {
    throw new Error("La serie temporal contiene fechas incompletas.");
  }
  return { ...response, points };
}

export const getDashboardTimeSeries = async (filters: DashboardFilters, metric: string, interval: string, signal?: AbortSignal) => {
  const response = await apiFetch<DashboardTimeSeries>(`/api/dashboard/timeseries?${query(filters, { metric, interval })}`, { signal });
  return normalizeDashboardTimeSeries(response);
};
export const getDashboardAgents = (filters: DashboardFilters, signal?: AbortSignal) => apiFetch<Page<AgentMetric>>(`/api/dashboard/agents?${query(filters, { limit: "8", sort_by: "total_tasks", sort_order: "desc" })}`, { signal });
export const getDashboardAttention = (filters: DashboardFilters, signal?: AbortSignal) => apiFetch<Page<AttentionItem>>(`/api/dashboard/attention?${query(filters, { limit: "8" })}`, { signal });
export const getDashboardActivity = (filters: DashboardFilters, signal?: AbortSignal) => apiFetch<Page<ActivityItem>>(`/api/dashboard/activity?${query(filters, { limit: "12", include_technical: "false" })}`, { signal });
