import { useCallback, useEffect, useState } from "react";
import { Archive, BookOpen, Check, Database, FileClock, RefreshCw, Search, X } from "lucide-react";
import { Link, NavLink, useLocation, useParams } from "react-router-dom";

import { AppNavigation } from "../components/AppNavigation";
import {
  approveKnowledge, getKnowledge, getKnowledgeCandidates, getKnowledgeChunks,
  getKnowledgeDetail, getKnowledgeRetrievalDetail, getKnowledgeRetrievals, getKnowledgeSources, rejectKnowledge,
  reindexKnowledge, type KnowledgeChunk, type KnowledgeRecord, type KnowledgeRetrieval,
  type KnowledgeRetrievalDetail as KnowledgeRetrievalDetailRecord, type KnowledgeSource,
} from "../api/knowledge";

const tabs = [
  ["/knowledge", "Knowledge", Database], ["/knowledge/candidates", "Candidates", FileClock],
  ["/knowledge/sources", "Sources", Archive], ["/knowledge/retrievals", "Retrievals", Search],
] as const;
const humanDate = (value: string | null | undefined) => value ? new Intl.DateTimeFormat("es-PE", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "n/a";
const shortId = (value: string) => value.slice(0, 8);
const indexStatus = (item: KnowledgeRecord) => item.status === "indexed" && (item.vector_count ?? 0) > 0 ? "Indexed" : (item.vector_count ?? 0) > 0 ? "Partial" : "Not indexed";
const scoreText = (value: number) => value.toFixed(3);
const filterText = (value: unknown) => Array.isArray(value) ? (value.length ? value.join(", ") : "Any") : typeof value === "object" && value !== null ? JSON.stringify(value) : String(value ?? "n/a");

function Layout({ children }: { children: React.ReactNode }) {
  return <div className="app-shell knowledge-page"><AppNavigation /><main><header className="knowledge-heading"><div><p className="page-eyebrow">Knowledge MCP &amp; RAG</p><h1>Conocimiento</h1></div><BookOpen aria-hidden="true" /></header><nav className="knowledge-tabs" aria-label="Secciones de conocimiento">{tabs.map(([path,label,Icon]) => <NavLink end={path === "/knowledge"} key={path} to={path}><Icon aria-hidden="true" />{label}</NavLink>)}</nav>{children}</main></div>;
}

function Status({ value }: { value: string }) {
  return <span className="knowledge-status" data-status={value}>{value.toUpperCase()}</span>;
}

function Empty({ error }: { error?: string | null }) {
  return <p className={`knowledge-state${error ? " error" : ""}`}>{error ?? "Sin registros."}</p>;
}

function KnowledgeList() {
  const [items,setItems]=useState<KnowledgeRecord[]>([]); const [error,setError]=useState<string | null>(null);
  useEffect(() => { getKnowledge().then(value => setItems(value.items)).catch(caught => setError(caught.message)); },[]);
  return <Layout><section className="knowledge-panel"><header><div><h2>Knowledge</h2><p>{items.length} registros canónicos</p></div></header>{error ? <Empty error={error} /> : <div className="knowledge-table"><table><thead><tr><th>ID</th><th>Type</th><th>Project</th><th>Source</th><th>Status</th><th>Version</th><th>Chunks</th><th>Index</th><th>Created</th></tr></thead><tbody>{items.map(item => <tr key={item.knowledge_id}><td><Link title={item.knowledge_id} to={`/knowledge/${item.knowledge_id}`}>{shortId(item.knowledge_id)}</Link></td><td>{item.knowledge_type}</td><td>{item.project_id}</td><td title={item.source_reference}>{item.source_reference}</td><td><Status value={item.status} /></td><td>{item.version}</td><td>{item.chunk_count ?? 0}</td><td>{indexStatus(item)}</td><td>{humanDate(item.created_at)}</td></tr>)}</tbody></table>{!items.length && <Empty />}</div>}</section></Layout>;
}

function Candidates() {
  const [items,setItems]=useState<KnowledgeRecord[]>([]); const [error,setError]=useState<string | null>(null); const [saving,setSaving]=useState<string | null>(null);
  const load=useCallback(() => getKnowledgeCandidates().then(value => { setItems(value.items); setError(null); }).catch(caught => setError(caught.message)),[]);
  useEffect(() => { void load(); },[load]);
  const approve=async (id:string) => { setSaving(id); try { await approveKnowledge(id); await load(); } catch(caught) { setError(caught instanceof Error ? caught.message : String(caught)); } finally { setSaving(null); } };
  const reject=async (id:string) => { const reason=window.prompt("Rejection reason"); if (!reason?.trim())return; setSaving(id); try { await rejectKnowledge(id,reason.trim()); await load(); } catch(caught) { setError(caught instanceof Error ? caught.message : String(caught)); } finally { setSaving(null); } };
  return <Layout><section className="knowledge-panel"><header><div><h2>Candidates</h2><p>{items.length} pendientes de política</p></div></header>{error && <Empty error={error} />}<div className="knowledge-table"><table><thead><tr><th>ID</th><th>Type</th><th>Project</th><th>Agent</th><th>Source</th><th>Created</th><th>Actions</th></tr></thead><tbody>{items.map(item => <tr key={item.knowledge_id}><td><Link to={`/knowledge/${item.knowledge_id}`}>{shortId(item.knowledge_id)}</Link></td><td>{item.knowledge_type}</td><td>{item.project_id}</td><td>{item.agent_name}</td><td>{item.source_reference}</td><td>{humanDate(item.created_at)}</td><td><div className="knowledge-actions"><button title="Approve" aria-label={`Approve ${item.knowledge_id}`} disabled={saving===item.knowledge_id} onClick={() => void approve(item.knowledge_id)}><Check aria-hidden="true" /></button><button title="Reject" aria-label={`Reject ${item.knowledge_id}`} disabled={saving===item.knowledge_id} onClick={() => void reject(item.knowledge_id)}><X aria-hidden="true" /></button></div></td></tr>)}</tbody></table>{!items.length && !error && <Empty />}</div></section></Layout>;
}

function Sources() {
  const [items,setItems]=useState<KnowledgeSource[]>([]); const [error,setError]=useState<string | null>(null);
  useEffect(() => { getKnowledgeSources().then(value => setItems(value.items)).catch(caught => setError(caught.message)); },[]);
  return <Layout><section className="knowledge-panel"><header><div><h2>Sources</h2><p>{items.length} fuentes durables</p></div></header>{error ? <Empty error={error} /> : <div className="knowledge-table"><table><thead><tr><th>Project</th><th>Source</th><th>Records</th><th>Indexed</th><th>Updated</th></tr></thead><tbody>{items.map(item => <tr key={`${item.project_id}:${item.source_reference}`}><td>{item.project_id}</td><td>{item.source_reference}</td><td>{item.knowledge_count}</td><td>{item.indexed_count}</td><td>{humanDate(item.updated_at)}</td></tr>)}</tbody></table>{!items.length && <Empty />}</div>}</section></Layout>;
}

function Retrievals() {
  const [items,setItems]=useState<KnowledgeRetrieval[]>([]); const [error,setError]=useState<string | null>(null);
  useEffect(() => { getKnowledgeRetrievals().then(value => setItems(value.items)).catch(caught => setError(caught.message)); },[]);
  return <Layout><section className="knowledge-panel"><header><div><h2>Retrievals</h2><p>{items.length} consultas auditadas</p></div></header>{error ? <Empty error={error} /> : <div className="knowledge-table"><table><thead><tr><th>Project</th><th>Workflow</th><th>Agent</th><th>Operation</th><th>Query</th><th>Results</th><th>Latency</th><th>Created</th><th>Action</th></tr></thead><tbody>{items.map(item => <tr key={item.retrieval_id}><td>{item.project_id}</td><td><code title={item.workflow_id ?? undefined}>{item.workflow_id ? shortId(item.workflow_id) : "n/a"}</code></td><td>{item.agent_name ?? "n/a"}</td><td>{item.operation}</td><td>{item.query_preview ?? item.query ?? "n/a"}</td><td>{item.result_count}</td><td>{Math.round(item.latency_ms)} ms</td><td>{humanDate(item.created_at)}</td><td><Link to={`/knowledge/retrievals/${item.retrieval_id}`}>View</Link></td></tr>)}</tbody></table>{!items.length && <Empty />}</div>}</section></Layout>;
}

function RetrievalDetail() {
  const { retrievalId="" }=useParams(); const [item,setItem]=useState<KnowledgeRetrievalDetailRecord | null>(null); const [error,setError]=useState<string | null>(null);
  useEffect(() => { getKnowledgeRetrievalDetail(retrievalId).then(value => setItem(value)).catch(caught => setError(caught.message)); },[retrievalId]);
  if (!item)return <Layout><Empty error={error ?? "Cargando retrieval..."} /></Layout>;
  const filters=Object.entries(item.filters ?? {});
  return <Layout><header className="knowledge-detail-head"><div><Link to="/knowledge/retrievals">Retrievals</Link><h2>Retrieval detail</h2><code>{item.retrieval_id}</code></div><span className="knowledge-status" data-status={item.detail_state === "complete" ? "indexed" : "candidate"}>{item.detail_state.replace("_", " ").toUpperCase()}</span></header>{error && <Empty error={error} />}<section className="knowledge-meta knowledge-retrieval-meta"><div><span>Query</span><strong>{item.query ?? item.query_preview ?? "n/a"}</strong></div><div><span>Project</span><strong>{item.project_id}</strong></div><div><span>Operation</span><strong>{item.operation}</strong></div><div><span>Result count</span><strong>{item.result_count}</strong></div><div><span>Latency</span><strong>{item.latency_ms.toFixed(1)} ms</strong></div><div><span>Created</span><strong>{humanDate(item.created_at)}</strong></div></section><section className="knowledge-panel"><header><div><h2>Request context</h2><p>Durable filters and execution correlation.</p></div></header><dl className="knowledge-retrieval-context"><div><dt>Top K</dt><dd>{item.top_k ?? "n/a"}</dd></div>{filters.map(([key,value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{filterText(value)}</dd></div>)}<div><dt>Workflow</dt><dd>{item.workflow_id ?? "n/a"}</dd></div><div><dt>Agent</dt><dd>{item.agent_name ?? "n/a"}</dd></div><div><dt>Trace</dt><dd title={item.trace_id ?? undefined}>{item.trace_id ?? "n/a"}</dd></div><div><dt>Span</dt><dd title={item.span_id ?? undefined}>{item.span_id ?? "n/a"}</dd></div></dl></section><section className="knowledge-panel"><header><div><h2>Retrieved context</h2><p>Ranked provenance captured at retrieval time.</p></div></header>{item.detail_state === "summary_only" && <p className="knowledge-state">Detailed provenance is unavailable for this historical retrieval.</p>}{item.results.length ? <div className="knowledge-table"><table><thead><tr><th>Rank</th><th>Knowledge</th><th>Chunk</th><th>Type</th><th>Source</th><th>Score</th><th>Version</th></tr></thead><tbody>{item.results.map(result => <tr key={`${result.retrieval_id}:${result.rank}`}><td>{result.rank}</td><td><Link title={result.knowledge_id} to={`/knowledge/${result.knowledge_id}`}>{shortId(result.knowledge_id)}</Link></td><td><code title={result.chunk_id}>{shortId(result.chunk_id)}</code></td><td>{result.knowledge_type}</td><td>{result.source_reference}</td><td><span className="knowledge-score" title={String(result.retrieval_score)}>{scoreText(result.retrieval_score)}</span></td><td>{result.version}</td></tr>)}</tbody></table></div> : item.detail_state === "complete" && <p className="knowledge-state">No knowledge matched this query.</p>}</section></Layout>;
}

function Detail() {
  const { knowledgeId="" }=useParams(); const [item,setItem]=useState<KnowledgeRecord | null>(null); const [chunks,setChunks]=useState<KnowledgeChunk[]>([]); const [error,setError]=useState<string | null>(null); const [saving,setSaving]=useState(false);
  const load=useCallback(() => Promise.all([getKnowledgeDetail(knowledgeId),getKnowledgeChunks(knowledgeId)]).then(([record,result]) => { setItem(record); setChunks(result.items); setError(null); }).catch(caught => setError(caught.message)),[knowledgeId]);
  useEffect(() => { void load(); },[load]);
  const reindex=async () => { setSaving(true); try { await reindexKnowledge(knowledgeId); await load(); } catch(caught) { setError(caught instanceof Error ? caught.message : String(caught)); } finally { setSaving(false); } };
  if (!item)return <Layout><Empty error={error ?? "Cargando conocimiento..."} /></Layout>;
  const workflowOrigin=item.metadata.origin === "workflow_learning_extractor";
  const evidence=Array.isArray(item.metadata.evidence_refs) ? item.metadata.evidence_refs : [];
  return <Layout><header className="knowledge-detail-head"><div><Link to="/knowledge">Knowledge</Link><h2>{item.knowledge_type}</h2><code>{item.knowledge_id}</code></div><div><Status value={item.status} />{["indexed","validated"].includes(item.status) && <button disabled={saving} onClick={() => void reindex()}><RefreshCw aria-hidden="true" />Reindex</button>}</div></header>{error && <Empty error={error} />}<section className="knowledge-meta"><div><span>Project</span><strong>{item.project_id}</strong></div><div><span>Source</span><strong>{item.source_reference}</strong></div><div><span>Version</span><strong>{item.version}</strong></div><div><span>Chunks</span><strong>{item.chunk_count ?? chunks.length}</strong></div><div><span>Index status</span><strong>{indexStatus(item)}</strong></div><div><span>Created</span><strong>{humanDate(item.created_at)}</strong></div></section>{workflowOrigin && <section className="knowledge-panel"><header><div><h2>Provenance</h2><p>Origin: Workflow learning</p></div></header><dl className="knowledge-retrieval-context"><div><dt>Workflow</dt><dd>{item.workflow_id ?? "n/a"}</dd></div><div><dt>Agent</dt><dd>{item.agent_name}</dd></div><div><dt>Evidence</dt><dd>{evidence.length ? evidence.map(value => typeof value === "object" ? JSON.stringify(value) : String(value)).join(" · ") : "n/a"}</dd></div></dl></section>}<section className="knowledge-panel"><header><div><h2>Canonical content</h2><p>Source of truth preserved independently from the vector index.</p></div></header><p className="knowledge-content">{item.content}</p></section><section className="knowledge-panel"><header><div><h2>Chunks</h2><p>{chunks.length} indexable representations</p></div></header><ol className="knowledge-chunks">{chunks.map(chunk => <li key={chunk.chunk_id}><header><span>Chunk {chunk.chunk_index+1}</span><code title={chunk.chunk_id}>{shortId(chunk.chunk_id)}</code></header><p>{chunk.content}</p></li>)}</ol>{!chunks.length && <Empty />}</section></Layout>;
}

export function KnowledgePage() {
  const path=useLocation().pathname;
  if (path==="/knowledge")return <KnowledgeList />;
  if (path==="/knowledge/candidates")return <Candidates />;
  if (path==="/knowledge/sources")return <Sources />;
  if (path==="/knowledge/retrievals")return <Retrievals />;
  if (path.startsWith("/knowledge/retrievals/"))return <RetrievalDetail />;
  return <Detail />;
}
