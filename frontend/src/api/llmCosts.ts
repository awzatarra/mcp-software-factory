import { apiFetch } from "./client";

export type CostState = "available" | "partial" | "unavailable";
export interface LlmCostSummary {
  state: CostState; currency: string; calls: number; calls_with_usage: number;
  calls_without_usage: number; calls_with_cost: number; calls_without_pricing: number;
  real_cost: string; estimated_cost: string; input_cost: string; cached_input_cost: string;
  output_cost: string; reasoning_cost: string; cache_savings: string; retry_cost: string;
  failed_call_cost: string; total_tokens: number; cached_tokens: number;
  budget_status: string; budget_limit: string | null; budget_consumed: string | null;
  budget_reserved: string | null; budget_remaining: string | null; p50: string | null; p90: string | null; p95: string | null;
  average_cost_per_call: string | null; average_cost_per_completed_workflow: string | null;
  active_budgets: number; global_budget_applicable: boolean; blocked_calls: number; completed_workflows: number;
  calculated_at: string;
}
export interface LlmCall {
  call_id: string; trace_id: string; span_id: string; workflow_id: string | null;
  branch_id: string; agent: string | null; node: string | null; subgraph: string | null;
  provider: string; model: string; operation: string; status: string; timestamp: string;
  duration_ms: number | null; input_tokens: number | null; output_tokens: number | null;
  total_tokens: number | null; cached_tokens: number | null; reasoning_tokens: number | null;
  usage_source: string; usage_available: boolean; cost_source: string | null;
  usage_invalid?: boolean; cost_status?: string | null; warnings?: string[] | string;
  retry_attempt?: number; input_cost?: string | null; cached_input_cost?: string | null;
  output_cost?: string | null; reasoning_cost?: string | null; pricing_id?: string | null;
  total_cost: string | null; estimated_total_cost: string | null; cache_savings: string | null;
}
export interface Pricing {
  pricing_id: string; provider: string; model_pattern: string; model_canonical_name: string | null;
  currency: string; input_price_per_million: string | null; cached_input_price_per_million: string | null;
  output_price_per_million: string | null; reasoning_price_per_million: string | null;
  effective_from: string; effective_to: string | null; source_type: string; source_reference: string | null;
  source_verified_at: string | null; reasoning_in_completion: boolean;
  enabled: boolean; priority: number;
}
export interface Budget {
  budget_id: string; name: string; description: string | null; scope_type: string;
  scope_value: string | null; currency: string; limit_amount: string; warning_percent: string;
  enforcement_mode: string; enabled: boolean; period_type?: string | null;
  period_start?: string | null; period_end?: string | null; reset_timezone?: string;
  include_estimated?: boolean; include_failed_calls?: boolean; include_retries?: boolean;
}
export interface CostAggregate { agent?: string; model?: string; provider?: string; operation?: string; calls: number; tokens: number; calls_with_usage: number; calls_without_usage: number; calls_with_pricing: number; calls_without_pricing: number; real_cost: string; estimated_cost: string; failed_call_cost: string; failures: number; retries: number; average_cost_per_call: string | null; percentage_of_total_cost: string | null }
export interface CostTimeseries { period: string; calls: number; tokens: number; real_cost: string; estimated_cost: string }
export interface BudgetUsage { budget: Budget; consumed: string; reserved: string; remaining: string; percent: string }
export interface BudgetEvent { budget_event_id: string; budget_id?: string; event_type: string; decision: string; amount: string | null; remaining_amount: string | null; workflow_id: string | null; reason_code: string; created_at: string }
export interface BudgetReservation { reservation_id: string; workflow_id: string | null; branch_id: string; llm_call_id: string | null; agent_name: string | null; estimated_amount: string; currency: string; status: string; created_at: string; expires_at: string; released_at: string | null; consumed_amount: string | null }
export interface ListResponse<T> { items: T[]; total?: number; limit?: number; offset?: number }

const query = (values: Record<string, string | undefined>) => {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => value && params.set(key, value));
  return params.size ? `?${params}` : "";
};
export const getCostSummary = (filters: Record<string, string | undefined> = {}) => apiFetch<LlmCostSummary>(`/api/llm-costs/summary${query(filters)}`);
export const getCalls = (filters: Record<string, string | undefined> = {}) => apiFetch<ListResponse<LlmCall>>(`/api/llm-costs/calls${query(filters)}`);
export const getCall = (id: string) => apiFetch<{ call: LlmCall; calculation: Record<string, unknown> | null; budget_events: Record<string, unknown>[] }>(`/api/llm-costs/calls/${encodeURIComponent(id)}`);
export const getWorkflowCosts = (id: string, branch = "original") => apiFetch<{ summary: LlmCostSummary; items: LlmCall[] }>(`/api/llm-costs/workflows/${encodeURIComponent(id)}?branch_id=${encodeURIComponent(branch)}`);
export const getCostCollection = <T>(name: string, filters: Record<string, string | undefined> = {}) => apiFetch<ListResponse<T>>(`/api/llm-costs/${name}${query(filters)}`);
export const getTimeseries = (filters: Record<string, string | undefined> = {}) => apiFetch<ListResponse<CostTimeseries>>(`/api/llm-costs/timeseries${query(filters)}`);
export const getPricing = () => apiFetch<ListResponse<Pricing>>("/api/llm-costs/pricing");
export const createPricing = (body: Record<string, unknown>) => apiFetch<Pricing>("/api/llm-costs/pricing", { method: "POST", body: JSON.stringify(body) });
export const disablePricing = (id: string) => apiFetch<Pricing>(`/api/llm-costs/pricing/${encodeURIComponent(id)}/disable`, { method: "POST" });
export const getBudgets = () => apiFetch<ListResponse<Budget>>("/api/llm-costs/budgets");
export const createBudget = (body: Record<string, unknown>) => apiFetch<Budget>("/api/llm-costs/budgets", { method: "POST", body: JSON.stringify(body) });
export const disableBudget = (id: string) => apiFetch<Budget>(`/api/llm-costs/budgets/${encodeURIComponent(id)}`, { method: "DELETE" });
export const getBudget = (id: string) => apiFetch<Budget>(`/api/llm-costs/budgets/${encodeURIComponent(id)}`);
export const getBudgetUsage = (id: string) => apiFetch<BudgetUsage>(`/api/llm-costs/budgets/${encodeURIComponent(id)}/usage`);
export const getBudgetEvents = (id: string) => apiFetch<ListResponse<BudgetEvent>>(`/api/llm-costs/budgets/${encodeURIComponent(id)}/events`);
export const getBudgetReservations = (id: string) => apiFetch<ListResponse<BudgetReservation>>(`/api/llm-costs/budgets/${encodeURIComponent(id)}/reservations`);
export const updateBudget = (id: string, body: Record<string, unknown>) => apiFetch<Budget>(`/api/llm-costs/budgets/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(body) });
export const resetBudget = (id: string, body: { actor: string; reason: string }) => apiFetch<Budget>(`/api/llm-costs/budgets/${encodeURIComponent(id)}/reset`, { method: "POST", body: JSON.stringify(body) });
