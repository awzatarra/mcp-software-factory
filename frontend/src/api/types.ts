export const WORKFLOW_EVENT_TYPES = [
  "workflow_started",
  "workflow_resumed",
  "workflow_forked",
  "workflow_completed",
  "workflow_failed",
  "stage_started",
  "stage_completed",
  "stage_failed",
  "supervisor_decision_started",
  "supervisor_decision_completed",
  "supervisor_fallback_used",
  "supervisor_loop_detected",
  "handoff_completed",
  "planning_started",
  "planning_completed",
  "planning_failed",
  "workspace_inspection_started",
  "workspace_inspection_completed",
  "implementation_started",
  "implementation_validation_completed",
  "implementation_completed",
  "implementation_failed",
  "testing_started",
  "testing_completed",
  "testing_failed",
  "repair_started",
  "repair_completed",
  "repair_failed",
  "approval_required",
  "approval_granted",
  "approval_rejected",
  "tool_started",
  "tool_completed",
  "tool_failed",
  "test_run_started",
  "test_run_completed",
  "test_run_failed",
] as const;

export type WorkflowEventType = (typeof WORKFLOW_EVENT_TYPES)[number];
export type EventStatus =
  | "pending"
  | "running"
  | "waiting"
  | "completed"
  | "failed";

export interface WorkflowEvent {
  event_id: string;
  thread_id: string;
  sequence: number;
  type: WorkflowEventType;
  timestamp: string;
  source: string;
  stage: string | null;
  status: EventStatus;
  message: string | null;
  data: Record<string, unknown>;
}

export interface WorkflowSnapshot {
  thread_id: string;
  checkpoint_id: string | null;
  project_name: string | null;
  workflow_intent: string | null;
  terminal_status: string;
  interrupted: boolean;
  pending_operation: string | null;
  pending_tool: string | null;
  planning: Record<string, unknown>;
  implementation: Record<string, unknown>;
  testing: Record<string, unknown>;
  agent_performance?: Record<string, unknown>;
  failure_attribution?: Record<string, unknown>;
  supervisor: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
  observability_summary?: Record<string, unknown>;
  git?: GitWorkflowSummary;
}

export interface GitWorkflowSummary {
  state: "available" | "not_repository" | "project_unavailable";
  repository: boolean;
  branch: string | null;
  head_commit: string | null;
  clean: boolean | null;
  effective_clean?: boolean | null;
  changed_files_count: number;
}

export interface GitRepositoryInfo {
  state: "available" | "not_repository";
  is_repository: boolean;
  inherited_parent_repository?: boolean;
  repository_root: string | null;
  current_branch: string | null;
  head_commit: string | null;
  detached_head: boolean;
  clean: boolean | null;
  effective_clean?: boolean | null;
  base_branch?: string | null;
  base_commit?: string | null;
  workflow_branch?: string | null;
  commits?: GitWorkflowCommit[];
  developer_commit?: GitWorkflowCommit | null;
  repair_commit?: GitWorkflowCommit | null;
  commit_preview?: GitCommitPreview | null;
  commit_status?: string | null;
  promotion?: GitPromotionStatus | null;
  ci_eligibility?: CIPromotionEligibility | null;
}

export interface GitPromotionPreview { promotion_id: string; approval_id: string | null; state: string; base_branch: string; base_commit_at_branch_creation: string | null; current_base_commit: string | null; base_advanced: boolean; workflow_branch: string; workflow_head: string; commits_ahead: number; commits_behind: number; commits: string[]; files_changed: string[]; additions: number; deletions: number; conflict_state: "clean" | "conflicts"; conflicting_files: string[]; merge_strategy_candidate: "fast_forward" | "merge_commit" | "blocked"; promotion_fingerprint: string; ready: boolean; ci?: CIPromotionEligibility | null; }
export interface GitPromotionResult { promotion_id: string; strategy: "fast_forward" | "merge_commit"; base_branch: string; workflow_branch: string; previous_base_commit: string | null; workflow_head: string; result_commit: string; merged_commits: string[]; files: string[]; completed_at: string; existing: boolean; }
export interface GitPromotionStatus { state: string; promotion_id: string | null; approval_id: string | null; preview: GitPromotionPreview | null; result: GitPromotionResult | null; rejection_reason: string | null; }

export interface GitWorkflowCommit { commit: string; message: string | null; created_at: string | null; phase?: "implementation" | "repair" | null; agent?: string | null; files?: string[]; base_commit?: string | null; parent_commit?: string | null; approval_id?: string | null; diff_fingerprint?: string | null; trace_id?: string | null; }

export interface GitCommitPreview { approval_id: string; branch: string; staged_files: string[]; additions: number; deletions: number; diff_fingerprint: string; proposed_message: string; ready: boolean; status: "awaiting_approval" | "approved" | "rejected" | "committed"; }

export interface GitStatus { branch: string | null; clean: boolean; effective_clean?: boolean | null; staged: string[]; modified: string[]; untracked: string[]; deleted: string[]; }
export interface GitDiffFile { path: string; status: "added" | "modified" | "deleted" | "renamed" | "unknown"; additions: number; deletions: number; patch: string; }
export interface GitDiff { staged: boolean; files: GitDiffFile[]; truncated: boolean; total_bytes: number; }
export interface GitLogEntry { commit: string; short_commit: string; author_name: string; author_email: string; timestamp: string; subject: string; }
export interface GitBranch { name: string; current: boolean; commit: string; }
export interface GitBranches { current: string | null; branches: GitBranch[]; }

export type CIStatus = "pending" | "running" | "passed" | "failed" | "timed_out" | "cancelled" | "interrupted";
export type CIStepStatus = CIStatus | "skipped";
export type CIDecision = "accepted" | "accepted_with_warnings" | "rejected";
export type CIGateStatus = "passed" | "failed" | "warning" | "skipped" | "not_applicable";
export interface CIPipelineStep {
  step_id: string;
  name: string;
  type: "build" | "test" | "lint" | "package" | "custom";
  command: string[];
  working_directory: string | null;
  timeout_seconds: number | null;
  required: boolean;
  continue_on_error: boolean;
}
export interface CIPipelineDefinition {
  pipeline_id: string;
  name: string;
  version: string;
  framework: string;
  steps: CIPipelineStep[];
  fail_fast: boolean;
  timeout_seconds: number;
}
export interface CISourceRevision {
  source_mode: "working_tree" | "commit";
  source_commit: string | null;
  source_branch: string | null;
  workflow_branch?: string | null;
  repository_root?: string | null;
  dirty: boolean | null;
  effective_clean: boolean | null;
}
export interface CIStepRun {
  step_id: string;
  name: string;
  type: CIPipelineStep["type"];
  status: CIStepStatus;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
  exit_code: number | null;
  stdout_summary: string | null;
  stderr_summary: string | null;
  output_truncated: boolean;
  failure_type: string | null;
  failure_message: string | null;
}
export interface CIGateDefinition {
  gate_id: string;
  type: "build" | "test" | "lint" | "package";
  required: boolean;
  blocking: boolean;
  source_step_types: Array<"build" | "test" | "lint" | "package">;
  minimum_success_count: number | null;
  allow_skipped: boolean;
  policy_version: string;
}
export interface CIGateResult {
  gate_id: string;
  type: "build" | "test" | "lint" | "package";
  status: CIGateStatus;
  required: boolean;
  blocking: boolean;
  reason: string;
  source_steps: string[];
  failure_type: string | null;
}
export interface CIPipelineRun {
  ci_run_id: string;
  workflow_id: string;
  project_id: string;
  pipeline: CIPipelineDefinition;
  pipeline_fingerprint: string;
  status: CIStatus;
  source: CISourceRevision;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
  steps: CIStepRun[];
  failed_step: string | null;
  failure_type: string | null;
  failure_message: string | null;
  warnings: string[];
  gate_policy_version: string | null;
  gates: CIGateResult[];
  decision: CIDecision | null;
  failed_gates: string[];
  warning_gates: string[];
  blocking_gate: string | null;
  gate_summary: Record<string, number>;
  ci_validated_commit?: string | null;
  repairability?: CIRepairability | null;
}
export interface CIPipelinePreview {
  workflow_id: string;
  project_id: string;
  state: "available";
  pipeline: CIPipelineDefinition;
  pipeline_fingerprint: string;
  source: CISourceRevision;
  expected_gates: CIGateDefinition[];
  gate_policy_version: string | null;
}
export interface CIWorkflowStatus {
  workflow_id: string;
  project_id: string | null;
  state: "not_started" | "available" | "project_unavailable";
  latest_run: CIPipelineRun | null;
  pipeline: CIPipelineDefinition | null;
  pipeline_fingerprint: string | null;
  source: CISourceRevision | null;
  promotion_eligible?: boolean | null;
  promotion_eligibility?: CIPromotionEligibility | null;
  repair?: CIRepairStatus;
}
export interface CIRepairability {
  repairable: boolean;
  category: string;
  confidence: number;
  reason_codes: string[];
  failed_gate: string | null;
  failed_step: string | null;
  failure_type: string | null;
  summary: string | null;
}
export interface CIRepairStatus {
  state: string;
  attempts: number;
  max_attempts: number;
  source_run_id: string | null;
  source_commit: string | null;
  target_commit: string | null;
  category: string | null;
  reason_codes: string[];
  repairability: CIRepairability | null;
  lineage: Array<Record<string, unknown>>;
}
export interface CIPromotionEligibility {
  required: boolean;
  eligible: boolean;
  reason: string;
  commit_match: boolean;
  source_commit: string | null;
  target_commit: string | null;
  run_id: string | null;
  ci_status: CIStatus | null;
  ci_decision: CIDecision | null;
  blocking_gates: string[];
  warnings: string[];
  gate_policy_version: string | null;
  pipeline_version: string | null;
  pipeline_fingerprint: string | null;
  metadata: Record<string, unknown>;
}
export interface CIRunListResponse {
  workflow_id: string;
  runs: CIPipelineRun[];
  total: number;
}
export interface CIStepMetrics {
  run_count: number;
  passed_count: number;
  failed_count: number;
  timed_out_count: number;
  skipped_count: number;
  average_duration_seconds: number | null;
  p50_duration_seconds: number | null;
  p95_duration_seconds: number | null;
  failure_rate: number | null;
}
export interface CIGateMetrics {
  evaluated_count: number;
  passed_count: number;
  failed_count: number;
  warning_count: number;
  skipped_count: number;
  not_applicable_count: number;
  failure_rate: number | null;
  warning_rate: number | null;
}
export interface CIOperationalMetrics {
  version: string;
  limit: number;
  framework: string | null;
  summary: {
    total_runs: number;
    completed_runs: number;
    runs_with_decision: number;
    accepted_runs: number;
    accepted_with_warnings_runs: number;
    rejected_runs: number;
    acceptance_rate: number | null;
    warning_rate: number | null;
    rejection_rate: number | null;
    average_pipeline_duration_seconds: number | null;
    p50_pipeline_duration_seconds: number | null;
    p95_pipeline_duration_seconds: number | null;
    commit_bound_runs: number;
    working_tree_runs: number;
  };
  steps: Record<string, CIStepMetrics>;
  gates: Record<string, CIGateMetrics>;
  failures: {
    by_failure_type: Record<string, number>;
    by_repair_category: Record<string, number>;
    code_related_failure_count: number;
    infrastructure_failure_count: number;
    configuration_failure_count: number;
    unknown_failure_count: number;
  };
  repair: {
    ci_repair_required_count: number;
    ci_repair_success_count: number;
    ci_repair_failed_count: number;
    ci_repair_exhausted_count: number;
    ci_repair_success_rate: number | null;
    ci_repair_exhaustion_rate: number | null;
    average_ci_repair_attempts: number | null;
    maximum_ci_repair_attempts_observed: number;
    commits_repaired_count: number;
    average_commits_per_repair_chain: number | null;
    runs_recovered_after_repair: number;
    runs_failed_after_repair: number;
  };
  promotion: {
    promotion_eligible_count: number;
    promotion_blocked_count: number;
    promotion_blocked_no_ci_count: number;
    promotion_blocked_rejected_ci_count: number;
    promotion_blocked_commit_mismatch_count: number;
    promotion_blocked_stale_ci_count: number;
    promotion_eligibility_rate: number | null;
  };
}
export interface CIAuditEntry {
  event_type: string;
  timestamp: string | null;
  workflow_id: string;
  ci_run_id: string | null;
  commit: string | null;
  step_id: string | null;
  gate: string | null;
  decision: string | null;
  failure_type: string | null;
  repair_attempt: number | null;
  repair_commit: string | null;
  promotion_eligible: boolean | null;
  metadata: Record<string, unknown>;
}
export interface CIAuditTrail {
  workflow_id: string;
  total: number;
  entries: CIAuditEntry[];
}

export interface CreateWorkflowResponse {
  thread_id: string;
  status: string;
  workflow_url: string;
  events_url: string;
}

export interface ApprovalResponse {
  thread_id: string;
  accepted: boolean;
  operation: string;
  tool_name: string;
  status: string;
}

export interface WorkflowHistoryResponse {
  thread_id: string;
  branch_id: string;
  events: WorkflowEvent[];
  last_sequence: number;
  has_more: boolean;
}

export type WorkflowListStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "waiting";
export type WorkflowSortBy =
  | "created_at"
  | "updated_at"
  | "project_name"
  | "terminal_status";
export type WorkflowSortOrder = "asc" | "desc";

export interface WorkflowListItem {
  thread_id: string;
  project_name: string | null;
  workflow_intent: string | null;
  terminal_status: string;
  interrupted: boolean;
  pending_operation: string | null;
  pending_tool: string | null;
  tests_executed: boolean;
  tests_passed: boolean;
  test_summary: string | null;
  planning_attempts: number;
  implementation_attempts: number;
  repair_phase: string;
  repair_attempts: number;
  supervisor_decision: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface WorkflowListResponse {
  items: WorkflowListItem[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface WorkflowProjectSummary {
  thread_id: string;
  project_name: string | null;
  project_exists: boolean;
  relative_project_path: string | null;
  total_files: number;
  total_directories: number;
  total_size_bytes: number;
  generated_files: string[];
  updated_files: string[];
  detected_framework: string | null;
  detected_test_framework: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export type ProjectContentType = "text" | "binary" | "image" | "unknown";

export interface ProjectFileNode {
  name: string;
  path: string;
  type: "file" | "directory";
  size_bytes: number | null;
  extension: string | null;
  language: string | null;
  content_type: ProjectContentType | null;
  content_available: boolean;
  is_generated: boolean;
  is_updated: boolean;
  children?: ProjectFileNode[] | null;
}

export interface ProjectFileTreeResponse {
  thread_id: string;
  project_name: string;
  root: ProjectFileNode;
  truncated: boolean;
  total_entries: number;
}

export interface ProjectFileContentResponse {
  thread_id: string;
  project_name: string;
  path: string;
  name: string;
  extension: string | null;
  language: string | null;
  content_type: "text";
  encoding: string;
  size_bytes: number;
  content: string;
  truncated: boolean;
  line_count: number;
  is_generated: boolean;
  is_updated: boolean;
}

export type WorkflowTaskStatus =
  | "pending"
  | "running"
  | "waiting"
  | "completed"
  | "failed"
  | "skipped";

export interface WorkflowExecutionAnalysis {
  requirement: string;
  objective: string | null;
  functional_requirements: string[];
  non_functional_requirements: string[];
  acceptance_criteria: string[];
  assumptions: string[];
  constraints: string[];
  risks: string[];
  completed: boolean;
  source: string | null;
  updated_at: string | null;
}

export interface WorkflowExecutionTask {
  task_id: string;
  order: number;
  agent: string;
  title: string | null;
  description: string;
  status: WorkflowTaskStatus;
  attempt: number;
  result_summary: string | null;
  primary_event_id: string | null;
  related_event_ids: string[];
  related_files: string[];
  started_at: string | null;
  completed_at: string | null;
}

export interface WorkflowRefinement {
  sequence: number;
  stage: string;
  reason: string | null;
  before_summary: string | null;
  after_summary: string | null;
  changed_fields: string[];
  attempt: number;
  event_id: string | null;
  created_at: string | null;
}

export interface WorkflowPlannerJudge {
  status: string | null;
  model: string | null;
  version: string | null;
  result: {
    overall_score?: number;
    confidence?: number;
    dimensions?: Record<string, number>;
    issues?: string[];
    strengths?: string[];
    recommendation?: string;
    reason_codes?: string[];
  } | null;
  disagreement: Record<string, unknown> | null;
  plan_fingerprint: string | null;
  evaluated_at: string | null;
}

export interface WorkflowPlannerHybridEvaluation {
  status: string | null;
  source: string | null;
  score: number | null;
  confidence: number | null;
  agreement: string | null;
  dimensions: Record<string, unknown>;
  flags: string[];
  recommendation: string | null;
  version: string | null;
  weights: Record<string, unknown>;
}

export interface WorkflowExecutionPlanning {
  valid: boolean;
  attempts: number;
  project_type: string | null;
  framework: string | null;
  analysis: WorkflowExecutionAnalysis;
  tasks: WorkflowExecutionTask[];
  validation_errors: string[];
  refinements: WorkflowRefinement[];
  judge: WorkflowPlannerJudge;
  hybrid_evaluation: WorkflowPlannerHybridEvaluation;
  started_at: string | null;
  completed_at: string | null;
}

export interface WorkflowExecutionImplementation {
  valid: boolean;
  attempts: number;
  project_name: string | null;
  package_name: string | null;
  framework: string | null;
  project_implementation: Record<string, unknown> | null;
  generated_files: string[];
  updated_files: string[];
  dependency_policy_applied: boolean;
  dependency_normalization_attempts: number;
  environment_prepared: boolean;
  dependencies_installed: boolean;
  installed_dependencies: string[];
  validation_errors: string[];
  refinements: WorkflowRefinement[];
  started_at: string | null;
  completed_at: string | null;
}

export interface WorkflowExecutionTesting {
  executed: boolean;
  passed: boolean;
  framework: string | null;
  expected_command: string[];
  actual_command: string[];
  summary: string | null;
  warnings: number;
  failure_type: string | null;
  failure_stage: string | null;
  failure_message: string | null;
  failing_test_files: string[];
  repair_phase: string;
  repair_attempts: number;
  repair_decision: string | null;
  repair_before: Record<string, unknown> | null;
  repair_after: Record<string, unknown> | null;
  files_read_during_repair: string[];
  files_updated_during_repair: string[];
  started_at: string | null;
  completed_at: string | null;
}

export interface WorkflowAgentPerformanceItem {
  agent: string;
  status: string;
  score: number | null;
  level: string | null;
  confidence: number;
  metrics: Record<string, unknown>;
  strengths: string[];
  issues: string[];
  reason_codes: string[];
  version: string | null;
}

export interface WorkflowFailureAttribution {
  status: string | null;
  failure_class: string | null;
  root_cause: string | null;
  primary_attribution: string | null;
  contributors: Array<Record<string, unknown>>;
  excluded_attributions: Array<Record<string, unknown>>;
  confidence: number | null;
  evidence: Record<string, unknown>;
  reason_codes: string[];
  recovered: boolean;
  recovery_source: string | null;
  causal_chain: string[];
  version: string | null;
}

export interface WorkflowAgentExecution {
  thread_id: string;
  branch_id: string;
  lineage: string;
  inherited_from: string | null;
  inherited_from_branch: string | null;
  origin_checkpoint: string | null;
  data_complete: boolean;
  workflow_intent: string | null;
  project_name: string | null;
  terminal_status: string;
  planning: WorkflowExecutionPlanning;
  implementation: WorkflowExecutionImplementation;
  testing: WorkflowExecutionTesting;
  agent_performance: Record<string, WorkflowAgentPerformanceItem>;
  failure_attribution: WorkflowFailureAttribution;
  git?: {
    state: string;
    base_branch: string | null;
    base_commit: string | null;
    workflow_branch: string | null;
    head_commit: string | null;
    commit_status: string | null;
    approval_state: string | null;
    developer_commit: GitExecutionCommit | null;
    repair_commit: GitExecutionCommit | null;
    commits: GitExecutionCommit[];
  };
  workflow_learning: {
    state: string;
    extracted_count: number;
    submitted_count: number;
    duplicate_count: number;
    rejected_count: number;
    candidates: Array<{
      candidate_id: string;
      knowledge_type: string;
      confidence: number;
      submission_status: string;
      knowledge_id: string | null;
      source_reference: string;
      created_at: string | null;
    }>;
  };
  supervisor: {
    decision: string | null;
    decision_source: string | null;
    confidence: number | null;
    attempts: number;
    errors: string[];
    loop_detected: boolean;
    handoff_history: Array<Record<string, unknown>>;
  };
  final_result: {
    terminal_status: string;
    summary: string | null;
    project_created: boolean;
    tests_passed: boolean;
    failure_type: string | null;
    failure_message: string | null;
  };
  events: WorkflowEvent[] | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface GitExecutionCommit { phase?: string; agent?: string; sha?: string; commit?: string; message?: string | null; files?: string[]; }

export type EvaluationSeverity = "info" | "warning" | "error" | "critical";
export type EvaluationGrade =
  | "excellent"
  | "good"
  | "acceptable"
  | "poor"
  | "critical";

export interface EvaluationPenalty {
  code: string;
  category: string;
  points: number;
  message: string;
  severity: EvaluationSeverity;
  related_task_id: string | null;
  related_event_id: string | null;
  related_file: string | null;
}

export interface EvaluationBonus {
  code: string;
  category: string;
  points: number;
  message: string;
  related_task_id: string | null;
  related_event_id: string | null;
  related_file: string | null;
}

export interface EvaluationPositiveSignal {
  code: string;
  category: string;
  message: string;
  related_task_id: string | null;
  related_event_id: string | null;
  related_file: string | null;
}

export interface EvaluationFinding {
  code: string;
  category: string;
  title: string;
  description: string;
  severity: EvaluationSeverity;
  recommendation: string | null;
  related_task_id: string | null;
  related_event_id: string | null;
  related_file: string | null;
}

export interface RequirementCoverageItem {
  index: number;
  text: string;
  status: "satisfied" | "unsatisfied" | "unknown";
  evidence: string[];
  related_task_ids: string[];
  related_event_ids: string[];
  related_files: string[];
}

export interface RequirementCoverage {
  total: number;
  satisfied: number;
  unsatisfied: number;
  unknown: number;
  coverage_percent: number;
  items: RequirementCoverageItem[];
}

export interface EvaluationCategory {
  score: number;
  max_score: number;
  percentage: number | null;
  evaluation_state: "not_started" | "partial" | "evaluated";
  available_points: number;
  penalties: EvaluationPenalty[];
  bonuses: EvaluationBonus[];
  positive_signals: EvaluationPositiveSignal[];
  findings: EvaluationFinding[];
}

export interface StageDuration {
  elapsed_seconds: number | null;
  active_seconds: number | null;
  waiting_seconds: number | null;
}

export interface WorkflowDurationBreakdown {
  wall_clock_duration_seconds: number | null;
  active_execution_seconds: number | null;
  total_duration_seconds: number | null;
  planning_seconds: number | null;
  implementation_seconds: number | null;
  testing_seconds: number | null;
  repair_seconds: number | null;
  approval_wait_seconds: number | null;
  finalize_seconds: number | null;
  planning: StageDuration;
  implementation: StageDuration;
  testing: StageDuration;
  repair: StageDuration;
  finalize: StageDuration;
}

export interface WorkflowEvaluation {
  thread_id: string;
  branch_id: string;
  lineage: string;
  inherited_from_branch: string | null;
  origin_checkpoint: string | null;
  terminal_status: string;
  data_complete: boolean;
  evaluation_status: "partial" | "final";
  overall_score: number;
  max_score: number;
  percentage: number;
  earned_points: number;
  available_points: number;
  provisional_percentage: number;
  projected_max_score: number;
  is_provisional: boolean;
  final_grade_available: boolean;
  grade: EvaluationGrade | null;
  provisional_grade: EvaluationGrade | null;
  planning: EvaluationCategory;
  implementation: EvaluationCategory;
  testing: EvaluationCategory;
  efficiency: EvaluationCategory;
  reliability: EvaluationCategory;
  requirement_coverage: RequirementCoverage;
  acceptance_coverage: RequirementCoverage;
  duration: WorkflowDurationBreakdown;
  penalties: EvaluationPenalty[];
  bonuses: EvaluationBonus[];
  positive_signals: EvaluationPositiveSignal[];
  findings: EvaluationFinding[];
  recommendations: string[];
  calculated_at: string;
  source_updated_at: string | null;
  scoring_version: string;
  evidence: Record<string, unknown> | null;
}

export interface GetWorkflowsOptions {
  status?: WorkflowListStatus;
  search?: string;
  limit?: number;
  offset?: number;
  sortBy?: WorkflowSortBy;
  sortOrder?: WorkflowSortOrder;
  signal?: AbortSignal;
}

export interface ApiErrorBody {
  detail?: string | Array<{ loc?: unknown[]; msg?: string; type?: string }>;
}

export interface UiError {
  title: string;
  message: string;
  statusCode?: number;
  retryable: boolean;
  scope?: UiErrorScope;
}

export type UiErrorScope =
  | "workflow"
  | "approval"
  | "connection"
  | "history"
  | "list"
  | "project"
  | "execution"
  | "evaluation";

export interface ApprovalLock {
  threadId: string;
  operation: string;
  toolName: string | null;
  startedAt: number;
}
