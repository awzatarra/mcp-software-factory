import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { KnowledgePage } from "./KnowledgePage";

vi.mock("../api/knowledge", () => ({
  getKnowledge: vi.fn(), getKnowledgeCandidates: vi.fn(), getKnowledgeSources: vi.fn(),
  getKnowledgeRetrievals: vi.fn(), getKnowledgeRetrievalDetail: vi.fn(), getKnowledgeDetail: vi.fn(), getKnowledgeChunks: vi.fn(),
  approveKnowledge: vi.fn(), rejectKnowledge: vi.fn(), reindexKnowledge: vi.fn(),
}));
vi.mock("../hooks/useAlertSummary", () => ({ useAlertSummary: () => ({ summary: null }) }));
vi.mock("../hooks/useNotificationSummary", () => ({ useNotificationSummary: () => ({ summary: null }) }));

import {
  approveKnowledge, getKnowledge, getKnowledgeCandidates, getKnowledgeChunks,
  getKnowledgeDetail, getKnowledgeRetrievalDetail, getKnowledgeRetrievals, getKnowledgeSources, rejectKnowledge,
  type KnowledgeRecord, type KnowledgeRetrieval,
} from "../api/knowledge";

const record: KnowledgeRecord = {
  knowledge_id: "knowledge-full-id", project_id: "phase-7-validation",
  workflow_id: "manual-knowledge-validation", agent_name: "Developer",
  requested_type: "workflow_learning", knowledge_type: "workflow_learning",
  classification_confidence: .75, content: "Prepare the environment before integration tests.",
  content_hash: "hash", source_reference: "workflow:manual-knowledge-validation", metadata: {},
  status: "indexed", rejection_reason: null, duplicate_of: null, duplicate_candidate: false,
  version: 1, created_at: "2026-08-09T07:30:41.637897+00:00",
  updated_at: "2026-08-09T07:30:41.637897+00:00", validated_at: "2026-08-09T07:30:41.637897+00:00",
  indexed_at: "2026-08-09T07:30:41.637897+00:00", chunk_count: 1, embedding_count: 1, vector_count: 1,
};

const retrieval: KnowledgeRetrieval = {
  retrieval_id: "retrieval-full-id", project_id: "phase-7-validation",
  operation: "knowledge.search", query_hash: "query-hash",
  query_preview: "How should integration tests handle database migrations?",
  query: "How should integration tests handle database migrations?",
  result_count: 1, latency_ms: 9.4, workflow_id: "manual-knowledge-validation",
  agent_name: "Developer", trace_id: "trace-full-id", span_id: "span-full-id", top_k: 5,
  filters: { knowledge_types: ["workflow_learning"] }, reranker_used: false,
  reranker_model: null, finops_call_ids: ["finops-call-id"], detail_state: "complete",
  metadata: {}, created_at: "2026-08-09T07:30:41Z",
};

describe("KnowledgePage", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders canonical knowledge and index status without raw embeddings", async () => {
    vi.mocked(getKnowledge).mockResolvedValue({ items: [record], count: 1 });
    render(<MemoryRouter initialEntries={["/knowledge"]}><KnowledgePage /></MemoryRouter>);
    expect(await screen.findByRole("link", { name: "knowledg" })).toHaveAttribute("href", "/knowledge/knowledge-full-id");
    expect(screen.getByText("workflow_learning")).toBeInTheDocument();
    expect(screen.getByText("phase-7-validation")).toBeInTheDocument();
    expect(screen.getByText("INDEXED")).toBeInTheDocument();
    expect(screen.getByText("Indexed")).toBeInTheDocument();
    expect(screen.getByText(/2026/)).not.toHaveTextContent("2026-08-09T07:30:41.637897+00:00");
    expect(screen.queryByText(/\[0\.1|embedding vector/i)).not.toBeInTheDocument();
  });

  it("shows candidates and executes explicit approve and reject actions", async () => {
    const candidate = { ...record, status: "candidate" as const, chunk_count: 0, embedding_count: 0, vector_count: 0 };
    vi.mocked(getKnowledgeCandidates).mockResolvedValueOnce({ items: [candidate], count: 1 }).mockResolvedValue({ items: [], count: 0 });
    vi.mocked(approveKnowledge).mockResolvedValue(record);
    render(<MemoryRouter initialEntries={["/knowledge/candidates"]}><KnowledgePage /></MemoryRouter>);
    await userEvent.click(await screen.findByRole("button", { name: "Approve knowledge-full-id" }));
    await waitFor(() => expect(approveKnowledge).toHaveBeenCalledWith("knowledge-full-id"));

    vi.mocked(getKnowledgeCandidates).mockResolvedValue({ items: [candidate], count: 1 });
    vi.spyOn(window,"prompt").mockReturnValue("Not reusable");
    const second=render(<MemoryRouter initialEntries={["/knowledge/candidates"]}><KnowledgePage /></MemoryRouter>);
    await userEvent.click((await screen.findAllByRole("button", { name: "Reject knowledge-full-id" })).at(-1)!);
    await waitFor(() => expect(rejectKnowledge).toHaveBeenCalledWith("knowledge-full-id","Not reusable"));
    second.unmount();
  });

  it("renders sources and durable retrieval audit views", async () => {
    vi.mocked(getKnowledgeSources).mockResolvedValue({ items: [{ project_id: "phase-7-validation", source_reference: "adr:1", knowledge_count: 2, indexed_count: 1, updated_at: "2026-08-09T07:30:41Z" }], count: 1 });
    const sources=render(<MemoryRouter initialEntries={["/knowledge/sources"]}><KnowledgePage /></MemoryRouter>);
    expect(await screen.findByText("adr:1")).toBeInTheDocument(); expect(screen.getByText("2")).toBeInTheDocument();
    sources.unmount();

    vi.mocked(getKnowledgeRetrievals).mockResolvedValue({ items: [{ ...retrieval, query_preview: "prepare environment", latency_ms: 12.4 }], count: 1 });
    render(<MemoryRouter initialEntries={["/knowledge/retrievals"]}><KnowledgePage /></MemoryRouter>);
    expect(await screen.findByText("knowledge.search")).toBeInTheDocument();
    expect(screen.getByText("Developer")).toBeInTheDocument();
    expect(screen.getByTitle("manual-knowledge-validation")).toHaveTextContent("manual-k");
    expect(screen.getByText("prepare environment")).toBeInTheDocument(); expect(screen.getByText("12 ms")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View" })).toHaveAttribute("href", "/knowledge/retrievals/retrieval-full-id");
  });

  it("renders retrieval detail with provenance links and a compact score", async () => {
    vi.mocked(getKnowledgeRetrievalDetail).mockResolvedValue({
      ...retrieval,
      results: [{
        retrieval_id: retrieval.retrieval_id, rank: 1, knowledge_id: record.knowledge_id,
        chunk_id: "chunk-full-id", project_id: retrieval.project_id,
        source_reference: record.source_reference, knowledge_type: "workflow_learning", version: 1,
        workflow_id: record.workflow_id, retrieval_score: 0.50395263,
        semantic_score: null, rerank_score: null, final_score: null,
        created_at: retrieval.created_at,
      }],
    });
    render(<MemoryRouter initialEntries={["/knowledge/retrievals/retrieval-full-id"]}><Routes><Route path="/knowledge/retrievals/:retrievalId" element={<KnowledgePage />} /></Routes></MemoryRouter>);
    expect(await screen.findByRole("heading", { name: "Retrieval detail" })).toBeInTheDocument();
    expect(getKnowledgeRetrievalDetail).toHaveBeenCalledWith("retrieval-full-id");
    expect(screen.getByText("How should integration tests handle database migrations?")).toBeInTheDocument();
    expect(screen.getAllByText("workflow_learning")).toHaveLength(2);
    expect(screen.getByText("0.504")).toHaveAttribute("title", "0.50395263");
    expect(screen.getByRole("link", { name: "knowledg" })).toHaveAttribute("href", "/knowledge/knowledge-full-id");
    expect(screen.getByText("trace-full-id")).toBeInTheDocument();
    expect(screen.queryByText(/vector_json|raw embedding/i)).not.toBeInTheDocument();
  });

  it("renders an empty retrieval as a normal audited miss", async () => {
    vi.mocked(getKnowledgeRetrievalDetail).mockResolvedValue({ ...retrieval, result_count: 0, results: [] });
    render(<MemoryRouter initialEntries={["/knowledge/retrievals/empty-id"]}><Routes><Route path="/knowledge/retrievals/:retrievalId" element={<KnowledgePage />} /></Routes></MemoryRouter>);
    expect(await screen.findByText("No knowledge matched this query.")).toBeInTheDocument();
    expect(screen.queryByText(/error/i)).not.toBeInTheDocument();
  });

  it("renders knowledge detail, provenance fields and chunks without vectors", async () => {
    vi.mocked(getKnowledgeDetail).mockResolvedValue(record);
    vi.mocked(getKnowledgeChunks).mockResolvedValue({ items: [{ chunk_id: "chunk-full-id", knowledge_id: record.knowledge_id, project_id: record.project_id, knowledge_type: record.knowledge_type, source_reference: record.source_reference, chunk_index: 0, content: record.content, content_hash: "chunk-hash", metadata: {}, created_at: record.created_at }], count: 1 });
    render(<MemoryRouter initialEntries={["/knowledge/knowledge-full-id"]}><KnowledgePage /></MemoryRouter>);
    expect(await screen.findByText("Canonical content")).toBeInTheDocument();
    expect(screen.getByText("phase-7-validation")).toBeInTheDocument();
    expect(screen.getByText("workflow:manual-knowledge-validation")).toBeInTheDocument();
    expect(screen.getAllByText("Prepare the environment before integration tests.")).toHaveLength(2);
    expect(screen.getByText("Chunk 1")).toBeInTheDocument();
    expect(screen.queryByText(/vector_json|\[0\./i)).not.toBeInTheDocument();
  });

  it("renders workflow learning provenance as structured fields", async () => {
    vi.mocked(getKnowledgeDetail).mockResolvedValue({
      ...record,
      agent_name: "Repair",
      metadata: {
        origin: "workflow_learning_extractor",
        evidence_refs: [{ kind: "repair_attempt", value: 1 }],
      },
    });
    vi.mocked(getKnowledgeChunks).mockResolvedValue({ items: [], count: 0 });
    render(<MemoryRouter initialEntries={["/knowledge/knowledge-full-id"]}><KnowledgePage /></MemoryRouter>);
    expect(await screen.findByRole("heading", { name: "Provenance" })).toBeInTheDocument();
    expect(screen.getByText("Origin: Workflow learning")).toBeInTheDocument();
    expect(screen.getByText("manual-knowledge-validation")).toBeInTheDocument();
    expect(screen.getByText("Repair")).toBeInTheDocument();
    expect(screen.getByText(/repair_attempt/)).toBeInTheDocument();
  });
});
