import { apiFetch } from "./client";
import type { CIOperationalMetrics } from "./types";

export type EvaluationRun = {
  evaluation_run_id: string; workflow_id: string; trace_id: string | null; branch_id: string;
  evaluation_type: string; status: string; overall_score: number | null; verdict: string | null;
  model: string | null; duration_ms: number | null; created_at: string;
  validity_status?: "valid" | "superseded" | "invalidated"; superseded_by_run_id?: string | null;
  invalidated_at?: string | null; invalidation_reason?: string | null;
  rubrics_used?: Array<{ rubric_id: string; rubric_version: string; agent_name: string; evaluation_type: string }>;
  rubric_binding_status?: string | null; rubric_binding_reason?: string | null;
  results?: Array<Record<string, unknown>>; metrics?: Array<Record<string, unknown>>; evidence?: Array<Record<string, unknown>>;
};
export type EvaluationPage<T> = { items: T[]; total?: number; has_more?: boolean };
export type EvaluationRubric = {
  rubric_id: string; name: string; version: string; agent_name: string;
  dimensions: Record<string, number>; weights: Record<string, number>; thresholds: Record<string, number>;
  verdict_thresholds?: Record<string, number>; enabled: boolean;
  version_status: "current" | "superseded" | "disabled"; usage_count: number;
  evaluations_total: number; evaluations_valid: number;
  created_at: string; updated_at: string | null; history?: EvaluationRubric[];
};
export type EvaluationBaseline = {
  baseline_id: string; scope: Record<string, string>; scope_key?: string;
  metric: string; score: number; sample_count: number; created_at: string;
};
export type EvaluationRegression = {
  evidence_id: string; evaluation_run_id: string; reference_id: string | null;
  summary: string; created_at: string;
  metadata?: Record<string, unknown>;
  metadata_json?: Record<string, unknown> | string;
};
export type PlannerRecommendation = {
  recommendation_id: string; analytics_version: string; recommendation_fingerprint: string; fingerprint?: string;
  policy: string; segment: string | null; direction: string; severity: string; confidence: number;
  reason_codes: string[]; evidence: Record<string, unknown>; current_value: unknown; suggested_value: unknown;
  status: string; application_status?: string;
  review?: { status: string; reviewer?: string | null; reviewed_at?: string | null; decision_reason?: string | null; deferred_until?: string | null };
};
export type AgentRecommendation = PlannerRecommendation & {
  agent: string; type: string; summary: string; recommended_action: string; trend?: string;
  target?: { type: string; agent: string; segment: string | null };
};
export type PlannerPolicyProposal = {
  proposal_id: string; proposal_version: string; source_recommendation_id: string;
  source_recommendation_fingerprint: string; policy_key: string; policy_scope: string; segment: string | null;
  current_value: unknown; proposed_value: unknown; change_type: string; status: string;
  application_status: string; proposal_fingerprint: string; proposal_risk_level: string;
  affected_workflows_scope: string; simulation?: Record<string, unknown> | null;
  safety_flags?: Record<string, boolean>; created_at: string; updated_at: string;
};
export type PlannerPolicyApplication = {
  application_id: string; proposal_id: string; proposal_fingerprint: string; application_fingerprint: string;
  policy_key: string; scope: string; previous_value: unknown; proposed_value: unknown; status: string;
  actor?: string | null; notes?: string | null; error_code?: string | null; error_message?: string | null;
  baseline_revision: number; applied_revision?: number | null; rollback_revision?: number | null;
  rollback_supported: boolean; verification_strategy: string; application_risk_level: string;
  created_at: string; started_at?: string | null; completed_at?: string | null; rolled_back_at?: string | null;
  preview?: { risk?: string; affected_scope?: string; simulation?: Record<string, unknown> | null; rollback_available?: boolean; verification_strategy?: string };
};
export type PlannerPolicyRollout = {
  rollout_id: string; application_id: string; proposal_id: string; policy_key: string; scope: string;
  baseline_revision: number; target_revision: number; previous_value: unknown; target_value: unknown;
  status: string; current_percentage: number; target_percentage: number; strategy: string; stages: number[];
  baseline_metrics: Record<string, unknown>; treatment_metrics?: Record<string, unknown> | null; control_metrics?: Record<string, unknown> | null;
  delta_metrics?: Record<string, unknown> | null; health_status: string; health_score: number;
  rollout_fingerprint: string; application_fingerprint: string; version: string; error_code?: string | null;
  created_at: string; started_at?: string | null; completed_at?: string | null; paused_at?: string | null; rolled_back_at?: string | null;
};
export type PlannerPolicyExperiment = {
  experiment_id: string; experiment_version: string; policy_key: string; scope: string;
  baseline_revision: number; control_value: unknown;
  variants: Array<{ variant_id: string; name: string; value: unknown; allocation_percentage?: number; risk_level?: string; status?: string }>;
  allocation: Record<string, number>; status: string; minimum_sample_size: number;
  observation_window: Record<string, unknown>; primary_metric: string; secondary_metrics: string[];
  guardrails: Record<string, unknown>; metrics?: Record<string, unknown> | null; result?: string | null;
  statistical_summary?: {
    confidence_level?: number; analysis_version?: string; primary_metric?: string;
    decision_confidence?: number; decision_confidence_label?: string;
    comparisons?: Record<string, {
      candidate_variant?: string; metric?: string; metric_type?: string;
      control_ci?: Array<number | null>; candidate_ci?: Array<number | null>;
      adjusted_delta_ci?: Array<number | null>; delta_ci?: Array<number | null>;
      improvement_delta?: number; minimum_detectable_effect?: number | null;
      estimated_required_sample_size?: number | null; statistically_significant?: boolean;
      practically_significant?: boolean; adjusted_significant?: boolean;
      sample_adequacy?: string; decision_confidence?: number; decision_confidence_label?: string;
    }>;
  } | null;
  promotion_readiness?: {
    status: string; score: number; confidence: number; reason_codes?: string[];
    winner_variant_id?: string | null; recommended_action?: string; version?: string;
    checks?: Record<string, boolean>; dimensions?: Record<string, number>;
    secondary_metric_consistency?: { status?: string }; temporal_stability?: { status?: string };
    policy_risk?: { risk_level?: string };
  } | null;
  winner_variant_id?: string | null; experiment_fingerprint: string; created_at: string;
  started_at?: string | null; completed_at?: string | null;
};
export type PlannerPolicyExperimentPortfolio = {
  version: string;
  active_experiments: Array<{ kind: string; id: string; policy_key: string; scope: string; status: string; priority: string }>;
  active_rollouts: Array<{ kind: string; id: string; policy_key: string; scope: string; status: string; priority: string }>;
  conflicts: Array<Record<string, unknown>>;
  warnings: Array<Record<string, unknown>>;
  blocking_conflict_count: number;
  warning_count: number;
  isolation_status: string;
};
export type EvaluationDashboard = {
  workflows_evaluated: number; average_workflow_score: number | null; verdicts: Record<string, number>;
  regression_count: number; average_agent_score: number | null; average_evaluation_cost: number;
  average_evaluation_duration_ms: number;
  score_over_time: Array<{ created_at: string; score: number }>;
  score_by_agent: Array<{ key: string; score: number }>;
  score_by_model: Array<{ key: string; score: number }>;
  score_vs_cost: Array<{ score: number; cost: number }>;
  score_vs_duration: Array<{ score: number; duration_ms: number }>;
  regressions_over_time: Array<{ created_at: string; count: number }>;
};

export const getEvaluationDashboard = () => apiFetch<EvaluationDashboard>("/api/evaluations/dashboard");
export const getCIOperationalMetrics = () => apiFetch<CIOperationalMetrics>("/api/evaluations/ci/metrics");
export const getEvaluationRuns = () => apiFetch<EvaluationPage<EvaluationRun>>("/api/evaluations/runs");
export const getEvaluationRun = (id: string) => apiFetch<EvaluationRun>(`/api/evaluations/runs/${encodeURIComponent(id)}`);
export const getEvaluationRubric = (id: string) => apiFetch<EvaluationRubric>(`/api/evaluations/rubrics/${encodeURIComponent(id)}`);
export const createEvaluationRubricVersion = (id: string, payload: { version: string; dimensions: Record<string, number>; weights: Record<string, number>; thresholds: Record<string, number>; enabled: boolean }) => apiFetch<EvaluationRubric>(`/api/evaluations/rubrics/${encodeURIComponent(id)}/versions`, { method: "POST", body: JSON.stringify(payload) });
export const disableEvaluationRubric = (id: string) => apiFetch<EvaluationRubric>(`/api/evaluations/rubrics/${encodeURIComponent(id)}/disable`, { method: "POST" });
export const getEvaluationCollection = <T>(name: string, includeInvalidated = false) => apiFetch<EvaluationPage<T>>(`/api/evaluations/${name}?include_invalidated=${includeInvalidated}`);
export const createWorkflowEvaluation = (workflowId: string, force = false) => apiFetch<EvaluationRun>(`/api/evaluations/workflows/${encodeURIComponent(workflowId)}/evaluate?force=${force}`, { method: "POST" });
export const createEvaluationBaseline = (payload: { scope: Record<string, string>; metric: string; score: number; sample_count: number }) => apiFetch<Record<string, unknown>>("/api/evaluations/baselines", { method: "POST", body: JSON.stringify(payload) });
export const compareEvaluation = (evaluationRunId: string, baselineId?: string) => apiFetch<Record<string, unknown>>("/api/evaluations/compare", { method: "POST", body: JSON.stringify({ evaluation_run_id: evaluationRunId, baseline_id: baselineId ?? null, regression_threshold: .05 }) });
export const getPlannerRecommendations = () => apiFetch<EvaluationPage<PlannerRecommendation> & { review_metrics?: Record<string, number> }>("/api/evaluations/planner/recommendations");
export const reviewPlannerRecommendation = (id: string, action: "start" | "accept" | "reject" | "defer", payload: Record<string, unknown>) => {
  const suffix = action === "start" ? "review/start" : action;
  return apiFetch<PlannerRecommendation>(`/api/evaluations/planner/recommendations/${encodeURIComponent(id)}/${suffix}`, { method: "POST", body: JSON.stringify(payload) });
};
export const getAgentRecommendations = () => apiFetch<EvaluationPage<AgentRecommendation> & { analysis?: Record<string, unknown>; review_metrics?: Record<string, number> }>("/api/evaluations/agents/recommendations");
export const reviewAgentRecommendation = (id: string, action: "start" | "accept" | "reject" | "defer", payload: Record<string, unknown>) => {
  const suffix = action === "start" ? "review/start" : action;
  return apiFetch<AgentRecommendation>(`/api/evaluations/agents/recommendations/${encodeURIComponent(id)}/${suffix}`, { method: "POST", body: JSON.stringify(payload) });
};
export const createPlannerPolicyProposal = (recommendationId: string) => apiFetch<PlannerPolicyProposal>(`/api/evaluations/planner/recommendations/${encodeURIComponent(recommendationId)}/policy-proposal`, { method: "POST" });
export const getPlannerPolicyProposals = () => apiFetch<EvaluationPage<PlannerPolicyProposal>>("/api/evaluations/planner/policy-proposals");
export const transitionPlannerPolicyProposal = (id: string, action: "ready" | "approve" | "reject" | "cancel", payload: Record<string, unknown>) => apiFetch<PlannerPolicyProposal>(`/api/evaluations/planner/policy-proposals/${encodeURIComponent(id)}/${action}`, { method: "POST", body: JSON.stringify(payload) });
export const getPlannerPolicyApplications = () => apiFetch<EvaluationPage<PlannerPolicyApplication>>("/api/evaluations/planner/policy-applications");
export const preparePlannerPolicyApplication = (proposalId: string, payload: Record<string, unknown>) => apiFetch<PlannerPolicyApplication>(`/api/evaluations/planner/policy-proposals/${encodeURIComponent(proposalId)}/application/prepare`, { method: "POST", body: JSON.stringify(payload) });
export const applyPlannerPolicyApplication = (applicationId: string, payload: Record<string, unknown>) => apiFetch<PlannerPolicyApplication>(`/api/evaluations/planner/policy-applications/${encodeURIComponent(applicationId)}/apply`, { method: "POST", body: JSON.stringify(payload) });
export const rollbackPlannerPolicyApplication = (applicationId: string, payload: Record<string, unknown>) => apiFetch<PlannerPolicyApplication>(`/api/evaluations/planner/policy-applications/${encodeURIComponent(applicationId)}/rollback`, { method: "POST", body: JSON.stringify(payload) });
export const getPlannerPolicyRollouts = () => apiFetch<EvaluationPage<PlannerPolicyRollout>>("/api/evaluations/planner/policy-rollouts");
export const preparePlannerPolicyRollout = (applicationId: string, payload: Record<string, unknown>) => apiFetch<PlannerPolicyRollout>(`/api/evaluations/planner/policy-applications/${encodeURIComponent(applicationId)}/rollout/prepare`, { method: "POST", body: JSON.stringify(payload) });
export const startPlannerPolicyRollout = (rolloutId: string, payload: Record<string, unknown>) => apiFetch<PlannerPolicyRollout>(`/api/evaluations/planner/policy-rollouts/${encodeURIComponent(rolloutId)}/start`, { method: "POST", body: JSON.stringify(payload) });
export const evaluatePlannerPolicyRollout = (rolloutId: string, payload: Record<string, unknown>) => apiFetch<PlannerPolicyRollout>(`/api/evaluations/planner/policy-rollouts/${encodeURIComponent(rolloutId)}/evaluate`, { method: "POST", body: JSON.stringify(payload) });
export const advancePlannerPolicyRollout = (rolloutId: string, payload: Record<string, unknown>) => apiFetch<PlannerPolicyRollout>(`/api/evaluations/planner/policy-rollouts/${encodeURIComponent(rolloutId)}/advance`, { method: "POST", body: JSON.stringify(payload) });
export const pausePlannerPolicyRollout = (rolloutId: string, payload: Record<string, unknown>) => apiFetch<PlannerPolicyRollout>(`/api/evaluations/planner/policy-rollouts/${encodeURIComponent(rolloutId)}/pause`, { method: "POST", body: JSON.stringify(payload) });
export const resumePlannerPolicyRollout = (rolloutId: string, payload: Record<string, unknown>) => apiFetch<PlannerPolicyRollout>(`/api/evaluations/planner/policy-rollouts/${encodeURIComponent(rolloutId)}/resume`, { method: "POST", body: JSON.stringify(payload) });
export const rollbackPlannerPolicyRollout = (rolloutId: string, payload: Record<string, unknown>) => apiFetch<PlannerPolicyRollout>(`/api/evaluations/planner/policy-rollouts/${encodeURIComponent(rolloutId)}/rollback`, { method: "POST", body: JSON.stringify(payload) });
export const getPlannerPolicyExperiments = () => apiFetch<EvaluationPage<PlannerPolicyExperiment>>("/api/evaluations/planner/policy-experiments");
export const getPlannerPolicyExperimentPortfolio = () => apiFetch<PlannerPolicyExperimentPortfolio>("/api/evaluations/planner/policy-experiments/portfolio");
export const readyPlannerPolicyExperiment = (experimentId: string, payload: Record<string, unknown>) => apiFetch<PlannerPolicyExperiment>(`/api/evaluations/planner/policy-experiments/${encodeURIComponent(experimentId)}/ready`, { method: "POST", body: JSON.stringify(payload) });
export const startPlannerPolicyExperiment = (experimentId: string, payload: Record<string, unknown>) => apiFetch<PlannerPolicyExperiment>(`/api/evaluations/planner/policy-experiments/${encodeURIComponent(experimentId)}/start`, { method: "POST", body: JSON.stringify(payload) });
export const evaluatePlannerPolicyExperiment = (experimentId: string, payload: Record<string, unknown>) => apiFetch<PlannerPolicyExperiment>(`/api/evaluations/planner/policy-experiments/${encodeURIComponent(experimentId)}/evaluate`, { method: "POST", body: JSON.stringify(payload) });
export const pausePlannerPolicyExperiment = (experimentId: string, payload: Record<string, unknown>) => apiFetch<PlannerPolicyExperiment>(`/api/evaluations/planner/policy-experiments/${encodeURIComponent(experimentId)}/pause`, { method: "POST", body: JSON.stringify(payload) });
export const resumePlannerPolicyExperiment = (experimentId: string, payload: Record<string, unknown>) => apiFetch<PlannerPolicyExperiment>(`/api/evaluations/planner/policy-experiments/${encodeURIComponent(experimentId)}/resume`, { method: "POST", body: JSON.stringify(payload) });
export const completePlannerPolicyExperiment = (experimentId: string, payload: Record<string, unknown>) => apiFetch<PlannerPolicyExperiment>(`/api/evaluations/planner/policy-experiments/${encodeURIComponent(experimentId)}/complete`, { method: "POST", body: JSON.stringify(payload) });
export const cancelPlannerPolicyExperiment = (experimentId: string, payload: Record<string, unknown>) => apiFetch<PlannerPolicyExperiment>(`/api/evaluations/planner/policy-experiments/${encodeURIComponent(experimentId)}/cancel`, { method: "POST", body: JSON.stringify(payload) });
