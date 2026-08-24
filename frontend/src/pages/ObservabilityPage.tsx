import { Activity, AlertTriangle, Braces, Clock3, Database, Search } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, NavLink, useLocation, useParams, useSearchParams } from "react-router-dom";
import { AppNavigation } from "../components/AppNavigation";
import { getObservabilityCollection, getObservabilitySummary, getTrace, getTraces, type ObservabilitySummary, type SpanRecord, type TraceDetail, type TraceRecord } from "../api/observability";

const tabs = [["/observability", "Resumen"], ["/observability/traces", "Trazas"], ["/observability/errors", "Errores"], ["/observability/agents", "Agentes"], ["/observability/tools", "Tools"], ["/observability/llm", "LLM"], ["/observability/slow-spans", "Spans lentos"]] as const;
const formatMs = (value: unknown) => typeof value === "number" ? value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(2)} s` : "n/a";
const formatDate = (value: unknown) => typeof value === "string" ? new Date(value).toLocaleString("es-PE") : "n/a";

function Layout({ children }: { children: React.ReactNode }) {
  return <div className="app-shell observability-page"><AppNavigation /><main><div className="observability-heading"><div><p className="page-eyebrow">Telemetría durable</p><h1>Observabilidad</h1><p>Trazas, errores y rendimiento de cada ejecución.</p></div></div><nav className="observability-tabs" aria-label="Secciones de observabilidad">{tabs.map(([path, label]) => <NavLink end={path === "/observability"} key={path} to={path}>{label}</NavLink>)}</nav>{children}</main></div>;
}

function State({ loading, error }: { loading: boolean; error: string | null }) {
  if (loading) return <div className="observability-state"><span className="loading-spinner" />Cargando telemetría...</div>;
  if (error) return <div className="observability-state" role="alert"><AlertTriangle aria-hidden="true" />{error}</div>;
  return null;
}

function SummaryPage() {
  const [summary, setSummary] = useState<ObservabilitySummary | null>(null); const [slow, setSlow] = useState<SpanRecord[]>([]); const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => { try { const [next, spans] = await Promise.all([getObservabilitySummary(), getObservabilityCollection<SpanRecord>("slow-spans")]); setSummary(next); setSlow(spans.items.slice(0, 8)); setError(null); } catch (caught) { setError(caught instanceof Error ? caught.message : "No se pudo cargar observabilidad."); } }, []);
  useEffect(() => { const timer = window.setTimeout(() => void load(), 0); return () => window.clearTimeout(timer); }, [load]);
  return <Layout><State loading={!summary && !error} error={error} />{summary && <><section className="observability-kpis"><article><Activity /><span>Trazas</span><strong>{summary.traces}</strong><small>{summary.completed} completadas</small></article><article><Braces /><span>Spans</span><strong>{summary.spans}</strong><small>{summary.slow_spans} lentos</small></article><article><AlertTriangle /><span>Errores</span><strong>{summary.errors}</strong><small>{summary.failed} workflows fallidos</small></article><article><Clock3 /><span>Duración media</span><strong>{formatMs(summary.average_duration_ms)}</strong><small>Extremo a extremo</small></article></section><section className="observability-grid"><article className="observability-panel"><header><div><h2>Rendimiento reciente</h2><p>Spans más lentos</p></div><Link to="/observability/slow-spans">Ver todos</Link></header><div className="waterfall-list">{slow.map((span) => <div key={span.span_id}><span>{span.name}</span><i style={{ width: `${Math.min(100, Math.max(3, (span.duration_ms ?? 0) / Math.max(...slow.map(item => item.duration_ms ?? 1)) * 100))}%` }} /><strong>{formatMs(span.duration_ms)}</strong></div>)}</div></article><article className="observability-panel"><header><div><h2>Salud operativa</h2><p>Señales agregadas</p></div><Database /></header><dl className="observability-facts"><dt>Tasa de error</dt><dd>{summary.spans ? `${(summary.errors / summary.spans * 100).toFixed(1)}%` : "0%"}</dd><dt>Spans lentos</dt><dd>{summary.slow_spans}</dd><dt>Último cálculo</dt><dd>{formatDate(summary.calculated_at)}</dd></dl></article></section></>}</Layout>;
}

function TraceListPage() {
  const [params, setParams] = useSearchParams(); const [items, setItems] = useState<TraceRecord[]>([]); const [loading, setLoading] = useState(true); const [error, setError] = useState<string | null>(null);
  const filters = useMemo(() => ({ workflow_id: params.get("workflow") || undefined, status: params.get("status") || undefined }), [params]);
  useEffect(() => { const controller = new AbortController(); const timer = window.setTimeout(() => { setLoading(true); getTraces(filters, controller.signal).then(result => { setItems(result.items); setError(null); }).catch(caught => { if (caught.name !== "AbortError") setError(caught.message); }).finally(() => setLoading(false)); }, 0); return () => { window.clearTimeout(timer); controller.abort(); }; }, [filters]);
  const change = (key: string, value: string) => { const next = new URLSearchParams(params); if (value) next.set(key, value); else next.delete(key); setParams(next, { replace: true }); };
  return <Layout><section className="observability-filters"><label><Search />Workflow<input value={params.get("workflow") || ""} onChange={event => change("workflow", event.target.value)} placeholder="thread id" /></label><label>Estado<select value={params.get("status") || ""} onChange={event => change("status", event.target.value)}><option value="">Todos</option><option value="running">running</option><option value="completed">completed</option><option value="failed">failed</option><option value="interrupted">interrupted</option></select></label></section><State loading={loading} error={error} />{!loading && <div className="observability-table-wrap"><table><thead><tr><th>Workflow</th><th>Rama</th><th>Estado</th><th>Inicio</th><th>Duración</th><th>Origen</th></tr></thead><tbody>{items.map(trace => <tr key={trace.trace_id}><td><Link to={`/observability/traces/${trace.trace_id}`}>{trace.workflow_id || trace.name}</Link><small>{trace.trace_id}</small></td><td>{trace.branch_id}</td><td><span className="trace-status" data-status={trace.status}>{trace.status}</span></td><td>{formatDate(trace.started_at)}</td><td>{formatMs(trace.duration_ms)}</td><td>{trace.source}</td></tr>)}</tbody></table>{!items.length && <p className="observability-empty">No hay trazas para estos filtros.</p>}</div>}</Layout>;
}

function CollectionPage({ name }: { name: string }) {
  const [items, setItems] = useState<Array<Record<string, unknown>>>([]); const [error, setError] = useState<string | null>(null); const [loading, setLoading] = useState(true);
  useEffect(() => { getObservabilityCollection<Record<string, unknown>>(name).then(result => setItems(result.items)).catch(caught => setError(caught.message)).finally(() => setLoading(false)); }, [name]);
  const columns = Array.from(new Set(items.flatMap(item => Object.keys(item)))).filter(key => !key.endsWith("attributes") && !key.endsWith("summary")).slice(0, 8);
  return <Layout><State loading={loading} error={error} /><div className="observability-table-wrap"><table><thead><tr>{columns.map(column => <th key={column}>{column.replaceAll("_", " ")}</th>)}</tr></thead><tbody>{items.map((item, index) => <tr key={String(item.error_fingerprint || item.span_id || item.agent || item.tool_name || index)}>{columns.map(column => <td key={column}>{column.includes("duration") ? formatMs(item[column]) : String(item[column] ?? "n/a")}</td>)}</tr>)}</tbody></table>{!loading && !items.length && <p className="observability-empty">No hay datos registrados en esta categoría.</p>}</div></Layout>;
}

function TraceDetailPage() {
  const { traceId = "" } = useParams(); const [trace, setTrace] = useState<TraceDetail | null>(null); const [error, setError] = useState<string | null>(null);
  useEffect(() => { getTrace(traceId).then(result => { setTrace(result); const hierarchy = result.hierarchy_validation; const issues = hierarchy ? [hierarchy.orphan_count > 0 && `${hierarchy.orphan_count} spans huérfanos`, hierarchy.cycle_count > 0 && `${hierarchy.cycle_count} ciclos`, hierarchy.terminal_active_spans > 0 && `${hierarchy.terminal_active_spans} spans activos en una traza terminal`, hierarchy.relationship_violation_count > 0 && `${hierarchy.relationship_violation_count} relaciones inválidas`, hierarchy.hierarchy_degraded && "jerarquía degradada"].filter(Boolean) : ["diagnóstico de jerarquía no disponible"]; setError(issues.length ? `Jerarquía de spans degradada: ${issues.join(", ")}.` : null); }).catch(caught => setError(caught.message)); }, [traceId]);
  const start = trace ? new Date(trace.started_at).getTime() : 0; const total = trace?.duration_ms || Math.max(...(trace?.spans.map(span => span.duration_ms || 0) ?? [1]), 1);
  return <Layout><State loading={!trace && !error} error={error} />{trace && <><header className="trace-detail-header"><div><Link to="/observability/traces">Trazas</Link><h2>{trace.workflow_id || trace.name}</h2><code>{trace.trace_id}</code></div><span className="trace-status" data-status={trace.status}>{trace.status}</span></header><section className="observability-panel trace-waterfall"><header><div><h2>Waterfall</h2><p>{trace.spans.length} spans · {formatMs(trace.duration_ms)}</p></div></header>{trace.spans.map(span => { const offset = (new Date(span.started_at).getTime() - start) / total * 100; const width = Math.max(1, (span.duration_ms || 1) / total * 100); return <div className="waterfall-row" key={span.span_id}><span>{span.name}</span><div><i data-status={span.status} style={{ marginLeft: `${Math.max(0, offset)}%`, width: `${Math.min(width, 100 - Math.max(0, offset))}%` }} /></div><strong>{formatMs(span.duration_ms)}</strong></div>; })}</section><section className="observability-grid"><article className="observability-panel"><header><div><h2>Logs correlacionados</h2><p>{trace.logs.length} registros</p></div></header><ol className="trace-log-list">{trace.logs.map((log, index) => <li key={String(log.log_id || index)}><time>{formatDate(log.timestamp)}</time><span>{String(log.message || "evento")}</span></li>)}</ol></article><article className="observability-panel"><header><div><h2>Artefactos</h2><p>Solo metadatos e integridad</p></div></header>{trace.artifacts.length ? <dl className="observability-facts">{trace.artifacts.map((artifact, index) => <div key={String(artifact.artifact_id || index)}><dt>{String(artifact.artifact_type || "artifact")}</dt><dd>{String(artifact.relative_path || "n/a")}</dd></div>)}</dl> : <p className="observability-empty">Sin artefactos asociados.</p>}</article></section></>}</Layout>;
}

export function ObservabilityPage() {
  const path = useLocation().pathname;
  if (path === "/observability") return <SummaryPage />;
  if (path === "/observability/traces") return <TraceListPage />;
  if (path.startsWith("/observability/traces/")) return <TraceDetailPage />;
  const name = path.split("/").pop() || "errors";
  return <CollectionPage name={name} />;
}
