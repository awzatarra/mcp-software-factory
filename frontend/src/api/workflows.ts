import { API_BASE_URL, apiFetch } from "./client";
import type {
  ApprovalResponse,
  CreateWorkflowResponse,
  WorkflowHistoryResponse,
  WorkflowSnapshot,
  GetWorkflowsOptions,
  WorkflowListResponse,
  WorkflowProjectSummary,
  ProjectFileTreeResponse,
  ProjectFileContentResponse,
  WorkflowAgentExecution,
  WorkflowEvaluation,
  GitRepositoryInfo,
  GitStatus,
  GitDiff,
  GitLogEntry,
  GitBranches,
  GitPromotionStatus,
  GitPromotionPreview,
  CIWorkflowStatus,
  CIPipelinePreview,
  CIPipelineRun,
  CIRunListResponse,
  CIAuditTrail,
} from "./types";

export function getWorkflowGit(threadId: string, signal?: AbortSignal): Promise<GitRepositoryInfo> {
  return apiFetch(`/api/workflows/${encodeURIComponent(threadId)}/git`, { signal });
}
export function getWorkflowGitStatus(threadId: string, signal?: AbortSignal): Promise<GitStatus> {
  return apiFetch(`/api/workflows/${encodeURIComponent(threadId)}/git/status`, { signal });
}
export function getWorkflowGitDiff(threadId: string, staged = false, signal?: AbortSignal): Promise<GitDiff> {
  const query = new URLSearchParams({ staged: String(staged) });
  return apiFetch(`/api/workflows/${encodeURIComponent(threadId)}/git/diff?${query}`, { signal });
}
export function getWorkflowGitLog(threadId: string, limit = 20, signal?: AbortSignal): Promise<GitLogEntry[]> {
  const query = new URLSearchParams({ limit: String(limit) });
  return apiFetch(`/api/workflows/${encodeURIComponent(threadId)}/git/log?${query}`, { signal });
}
export function getWorkflowGitBranches(threadId: string, signal?: AbortSignal): Promise<GitBranches> {
  return apiFetch(`/api/workflows/${encodeURIComponent(threadId)}/git/branches`, { signal });
}
export function initializeWorkflowGit(threadId: string) {
  return apiFetch(`/api/workflows/${encodeURIComponent(threadId)}/git/init`, { method: "POST" });
}
export function createWorkflowGitBranch(threadId: string) {
  return apiFetch(`/api/workflows/${encodeURIComponent(threadId)}/git/branch`, { method: "POST" });
}
export function stageWorkflowGitFiles(threadId: string, paths: string[], actor: "Developer" | "Repair" | "user" = "Developer") {
  return apiFetch(`/api/workflows/${encodeURIComponent(threadId)}/git/stage`, { method: "POST", body: JSON.stringify({ paths, actor }) });
}
export function prepareWorkflowGitCommit(threadId: string, message: string, actor: "Developer" | "Repair" | "user" = "Developer") {
  return apiFetch(`/api/workflows/${encodeURIComponent(threadId)}/git/commit/prepare`, { method: "POST", body: JSON.stringify({ message, actor }) });
}
export function getWorkflowGitPromotion(threadId: string, signal?: AbortSignal): Promise<GitPromotionStatus> {
  return apiFetch(`/api/workflows/${encodeURIComponent(threadId)}/git/promotion`, { signal });
}
export function prepareWorkflowGitPromotion(threadId: string): Promise<GitPromotionPreview> {
  return apiFetch(`/api/workflows/${encodeURIComponent(threadId)}/git/promotion/prepare`, { method: "POST" });
}

export function getWorkflowCI(threadId: string, signal?: AbortSignal): Promise<CIWorkflowStatus> {
  return apiFetch(`/api/workflows/${encodeURIComponent(threadId)}/ci`, { signal });
}
export function prepareWorkflowCI(threadId: string): Promise<CIPipelinePreview> {
  return apiFetch(`/api/workflows/${encodeURIComponent(threadId)}/ci/prepare`, { method: "POST" });
}
export function runWorkflowCI(threadId: string, pipelineFingerprint: string, ciRunId?: string): Promise<CIPipelineRun> {
  return apiFetch(`/api/workflows/${encodeURIComponent(threadId)}/ci/run`, {
    method: "POST",
    body: JSON.stringify({ pipeline_fingerprint: pipelineFingerprint, ci_run_id: ciRunId ?? null }),
  });
}
export function getWorkflowCIRuns(threadId: string, signal?: AbortSignal): Promise<CIRunListResponse> {
  return apiFetch(`/api/workflows/${encodeURIComponent(threadId)}/ci/runs`, { signal });
}
export function getWorkflowCIAudit(threadId: string, signal?: AbortSignal): Promise<CIAuditTrail> {
  return apiFetch(`/api/workflows/${encodeURIComponent(threadId)}/ci/audit`, { signal });
}

export function getWorkflowExecution(
  threadId: string,
  branchId = "original",
  signal?: AbortSignal,
): Promise<WorkflowAgentExecution> {
  const query = new URLSearchParams({
    branch_id: branchId,
    include_events: "false",
  });
  return apiFetch(
    `/api/workflows/${encodeURIComponent(threadId)}/execution?${query}`,
    { signal },
  );
}

export function getWorkflowEvaluation(
  threadId: string,
  branchId = "original",
  signal?: AbortSignal,
): Promise<WorkflowEvaluation> {
  const query = new URLSearchParams({
    branch_id: branchId,
    include_evidence: "false",
  });
  return apiFetch(
    `/api/workflows/${encodeURIComponent(threadId)}/evaluation?${query}`,
    { signal },
  );
}

export function getWorkflows(
  options: GetWorkflowsOptions = {},
): Promise<WorkflowListResponse> {
  const query = new URLSearchParams();
  if (options.status) query.set("status", options.status);
  if (options.search?.trim()) query.set("search", options.search.trim());
  query.set("limit", String(options.limit ?? 20));
  query.set("offset", String(options.offset ?? 0));
  query.set("sort_by", options.sortBy ?? "updated_at");
  query.set("sort_order", options.sortOrder ?? "desc");
  return apiFetch(`/api/workflows?${query}`, { signal: options.signal });
}

export function getWorkflowProject(
  threadId: string,
  signal?: AbortSignal,
): Promise<WorkflowProjectSummary> {
  return apiFetch(
    `/api/workflows/${encodeURIComponent(threadId)}/project`,
    { signal },
  );
}

export function getProjectFileTree(
  threadId: string,
  signal?: AbortSignal,
): Promise<ProjectFileTreeResponse> {
  const query = new URLSearchParams({ depth: "20" });
  return apiFetch(
    `/api/workflows/${encodeURIComponent(threadId)}/project/files?${query}`,
    { signal },
  );
}

export function getProjectFileContent(
  threadId: string,
  path: string,
  signal?: AbortSignal,
): Promise<ProjectFileContentResponse> {
  const query = new URLSearchParams({ path });
  return apiFetch(
    `/api/workflows/${encodeURIComponent(threadId)}/project/files/content?${query}`,
    { signal },
  );
}

export function projectFileDownloadUrl(threadId: string, path: string): string {
  const query = new URLSearchParams({ path });
  return `${API_BASE_URL}/api/workflows/${encodeURIComponent(threadId)}/project/files/download?${query}`;
}

export function projectArchiveUrl(threadId: string): string {
  return `${API_BASE_URL}/api/workflows/${encodeURIComponent(threadId)}/project/archive`;
}

export function workflowEventsUrl(
  threadId: string,
  afterSequence = 0,
  branchId = "original",
): string {
  const query = new URLSearchParams();
  if (branchId !== "original") query.set("branch_id", branchId);
  if (afterSequence > 0) query.set("after_sequence", String(afterSequence));
  const suffix = query.size ? `?${query}` : "";
  return `${API_BASE_URL}/api/workflows/${encodeURIComponent(threadId)}/events${suffix}`;
}

export function createWorkflow(
  request: string,
): Promise<CreateWorkflowResponse> {
  return apiFetch("/api/workflows", {
    method: "POST",
    body: JSON.stringify({ request }),
  });
}

export function getWorkflow(
  threadId: string,
  signal?: AbortSignal,
): Promise<WorkflowSnapshot> {
  return apiFetch(`/api/workflows/${encodeURIComponent(threadId)}`, { signal });
}

export function getWorkflowHistory(
  threadId: string,
  options: {
    afterSequence?: number;
    limit?: number;
    branchId?: string;
    eventTypes?: string[];
    signal?: AbortSignal;
  } = {},
): Promise<WorkflowHistoryResponse> {
  const query = new URLSearchParams({
    after_sequence: String(options.afterSequence ?? 0),
    limit: String(options.limit ?? 1000),
    branch_id: options.branchId ?? "original",
  });
  for (const eventType of options.eventTypes ?? []) {
    query.append("event_type", eventType);
  }
  return apiFetch(
    `/api/workflows/${encodeURIComponent(threadId)}/events/history?${query}`,
    { signal: options.signal },
  );
}

export async function getCompleteWorkflowHistory(
  threadId: string,
  signal?: AbortSignal,
  branchId = "original",
): Promise<WorkflowHistoryResponse> {
  const events: WorkflowHistoryResponse["events"] = [];
  let afterSequence = 0;
  let page: WorkflowHistoryResponse;
  do {
    page = await getWorkflowHistory(threadId, {
      afterSequence,
      limit: 1000,
      branchId,
      signal,
    });
    events.push(...page.events);
    afterSequence = page.events.at(-1)?.sequence ?? page.last_sequence;
  } while (page.has_more && events.length < 10_000);
  return { ...page, events };
}

function resolveApproval(
  threadId: string,
  action: "approve" | "reject",
  reason?: string,
): Promise<ApprovalResponse> {
  return apiFetch(
    `/api/workflows/${encodeURIComponent(threadId)}/${action}`,
    {
      method: "POST",
      body: JSON.stringify({ reason: reason?.trim() || null }),
    },
  );
}

export function approveWorkflow(
  threadId: string,
  reason?: string,
): Promise<ApprovalResponse> {
  return resolveApproval(threadId, "approve", reason);
}

export function rejectWorkflow(
  threadId: string,
  reason?: string,
): Promise<ApprovalResponse> {
  return resolveApproval(threadId, "reject", reason);
}
