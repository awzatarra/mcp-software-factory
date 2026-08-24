import { apiFetch } from "./client";

export interface TraceRecord {
  trace_id: string; workflow_id: string | null; branch_id: string; name: string;
  status: string; source: string; started_at: string; ended_at: string | null;
  duration_ms: number | null; attributes: Record<string, unknown>;
}
export interface SpanRecord {
  span_id: string; trace_id: string; parent_span_id: string | null; name: string;
  category: string; kind: string; status: string; agent: string | null;
  operation: string | null; started_at: string; ended_at: string | null;
  duration_ms: number | null; is_slow: boolean; error_message: string | null;
  attributes: Record<string, unknown>;
}
export interface ObservabilitySummary {
  traces: number; completed: number; failed: number; spans: number;
  slow_spans: number; errors: number; average_duration_ms: number | null; calculated_at: string;
}
export interface TraceDetail extends TraceRecord {
  spans: SpanRecord[]; events: Array<Record<string, unknown>>;
  logs: Array<Record<string, unknown>>; artifacts: Array<Record<string, unknown>>;
  hierarchy_validation?: {
    valid: boolean; root_count: number; orphan_count: number; cycle_count: number;
    max_depth: number; terminal_active_spans: number; relationship_violation_count: number;
    precision: string; hierarchy_degraded: boolean;
  };
}
export interface ListResponse<T> { items: T[]; total?: number; limit?: number; offset?: number }

const query = (values: Record<string, string | undefined>) => {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => value && params.set(key, value));
  const encoded = params.toString(); return encoded ? `?${encoded}` : "";
};

export const getObservabilitySummary = (signal?: AbortSignal) => apiFetch<ObservabilitySummary>("/api/observability/summary", { signal });
export const getTraces = (filters: Record<string, string | undefined>, signal?: AbortSignal) => apiFetch<ListResponse<TraceRecord>>(`/api/observability/traces${query(filters)}`, { signal });
export const getTrace = (traceId: string, signal?: AbortSignal) => apiFetch<TraceDetail>(`/api/observability/traces/${encodeURIComponent(traceId)}`, { signal });
export const getObservabilityCollection = <T>(name: string, filters: Record<string, string | undefined> = {}, signal?: AbortSignal) => apiFetch<ListResponse<T>>(`/api/observability/${name}${query(filters)}`, { signal });
