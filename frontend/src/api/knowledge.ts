import { apiFetch } from "./client";

export type KnowledgeRecord = {
  knowledge_id: string; project_id: string; workflow_id: string | null; agent_name: string;
  requested_type: string; knowledge_type: string; classification_confidence: number;
  content: string; content_hash: string; source_reference: string; metadata: Record<string, unknown>;
  status: "candidate" | "validated" | "rejected" | "indexed" | "superseded" | "archived";
  rejection_reason: string | null; duplicate_of: string | null; duplicate_candidate: boolean;
  version: number; created_at: string; updated_at: string; validated_at: string | null;
  indexed_at: string | null; chunk_count?: number; embedding_count?: number; vector_count?: number;
};

export type KnowledgeChunk = {
  chunk_id: string; knowledge_id: string; project_id: string; knowledge_type: string;
  source_reference: string; chunk_index: number; content: string; content_hash: string;
  metadata: Record<string, unknown>; created_at: string;
};

export type KnowledgeSource = {
  project_id: string; source_reference: string; knowledge_count: number;
  indexed_count: number; updated_at: string;
};

export type KnowledgeRetrieval = {
  retrieval_id: string; project_id: string; operation: string; query_hash: string;
  query_preview: string | null; query: string | null; result_count: number; latency_ms: number;
  workflow_id: string | null; agent_name: string | null; trace_id: string | null;
  span_id: string | null; top_k: number | null; filters: Record<string, unknown>;
  reranker_used: boolean; reranker_model: string | null; finops_call_ids: string[];
  detail_state: "complete" | "summary_only"; metadata: Record<string, unknown>; created_at: string;
};

export type KnowledgeRetrievalResult = {
  retrieval_id: string; rank: number; knowledge_id: string; chunk_id: string;
  project_id: string; source_reference: string; knowledge_type: string; version: number;
  workflow_id: string | null; retrieval_score: number; semantic_score: number | null;
  rerank_score: number | null; final_score: number | null; created_at: string;
};

export type KnowledgeRetrievalDetail = KnowledgeRetrieval & { results: KnowledgeRetrievalResult[] };

type Collection<T> = { items: T[]; count: number };

export const getKnowledge = () => apiFetch<Collection<KnowledgeRecord>>("/api/knowledge");
export const getKnowledgeCandidates = () => apiFetch<Collection<KnowledgeRecord>>("/api/knowledge/candidates");
export const getKnowledgeSources = () => apiFetch<Collection<KnowledgeSource>>("/api/knowledge/sources");
export const getKnowledgeRetrievals = () => apiFetch<Collection<KnowledgeRetrieval>>("/api/knowledge/retrievals");
export const getKnowledgeRetrievalDetail = (id: string) => apiFetch<KnowledgeRetrievalDetail>(`/api/knowledge/retrievals/${encodeURIComponent(id)}`);
export const getKnowledgeDetail = (id: string) => apiFetch<KnowledgeRecord>(`/api/knowledge/${encodeURIComponent(id)}`);
export const getKnowledgeChunks = (id: string) => apiFetch<Collection<KnowledgeChunk>>(`/api/knowledge/${encodeURIComponent(id)}/chunks`);
export const approveKnowledge = (id: string) => apiFetch<KnowledgeRecord>(`/api/knowledge/${encodeURIComponent(id)}/approve`, { method: "POST" });
export const rejectKnowledge = (id: string, reason: string) => apiFetch<KnowledgeRecord>(`/api/knowledge/${encodeURIComponent(id)}/reject`, { method: "POST", body: JSON.stringify({ reason }) });
export const reindexKnowledge = (id: string) => apiFetch<KnowledgeRecord>(`/api/knowledge/${encodeURIComponent(id)}/reindex`, { method: "POST" });
