import { useEffect, useState } from "react";
import { Activity, BarChart3, Bot, CheckCircle2, Clock3, Copy, Gauge, PauseCircle, Plus, RefreshCw, Scale, ShieldCheck, SlidersHorizontal, XCircle } from "lucide-react";
import { Link, NavLink, useLocation, useParams } from "react-router-dom";

import { AppNavigation } from "../components/AppNavigation";
import { advancePlannerPolicyRollout, applyPlannerPolicyApplication, cancelPlannerPolicyExperiment, completePlannerPolicyExperiment, createEvaluationRubricVersion, createPlannerPolicyProposal, disableEvaluationRubric, evaluatePlannerPolicyExperiment, evaluatePlannerPolicyRollout, getAgentRecommendations, getCIOperationalMetrics, getEvaluationCollection, getEvaluationDashboard, getEvaluationRubric, getEvaluationRun, getEvaluationRuns, getPlannerPolicyApplications, getPlannerPolicyExperimentPortfolio, getPlannerPolicyExperiments, getPlannerPolicyProposals, getPlannerPolicyRollouts, getPlannerRecommendations, pausePlannerPolicyExperiment, pausePlannerPolicyRollout, preparePlannerPolicyApplication, preparePlannerPolicyRollout, readyPlannerPolicyExperiment, resumePlannerPolicyExperiment, resumePlannerPolicyRollout, reviewAgentRecommendation, reviewPlannerRecommendation, rollbackPlannerPolicyApplication, rollbackPlannerPolicyRollout, startPlannerPolicyExperiment, startPlannerPolicyRollout, transitionPlannerPolicyProposal, type AgentRecommendation, type EvaluationBaseline, type EvaluationDashboard, type EvaluationRegression, type EvaluationRubric, type EvaluationRun, type PlannerPolicyApplication, type PlannerPolicyExperiment, type PlannerPolicyExperimentPortfolio, type PlannerPolicyProposal, type PlannerPolicyRollout, type PlannerRecommendation } from "../api/evaluations";
import type { CIOperationalMetrics } from "../api/types";

const tabs = [
  ["/evaluations", "Dashboard", BarChart3], ["/evaluations/runs", "Runs", Activity],
  ["/evaluations/agents", "Agents", Bot], ["/evaluations/metrics", "Metrics", Gauge],
  ["/evaluations/rubrics", "Rubrics", SlidersHorizontal], ["/evaluations/baselines", "Baselines", Scale],
  ["/evaluations/regressions", "Regressions", ShieldCheck], ["/evaluations/recommendations", "Recommendations", CheckCircle2],
  ["/evaluations/experiments", "Experiments", Gauge], ["/evaluations/ci", "CI Operations", Activity],
] as const;

const score = (value: unknown) => typeof value === "number" ? `${Math.round(value * 100)}%` : "n/a";
const number = (value: unknown, digits = 2) => typeof value === "number" ? value.toFixed(digits) : "n/a";
const label = (value: string) => value.replaceAll("_", " ");
const validity = (run: EvaluationRun) => run.validity_status ?? "valid";
const isScoreColumn = (column: string) => column === "score" || column.endsWith("_score");
const compactNumber = (value: number, digits = 3) => new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(value);
const humanDate = (value: string | null) => value ? new Intl.DateTimeFormat("es-PE", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "n/a";
const nextVersion = (version: string) => { const match = /^(\d+)\.(\d+)$/.exec(version); return match ? `${match[1]}.${Number(match[2]) + 1}` : `${version}.1`; };
const lowerIsBetter = (metric: string) => new Set(["duration_ms", "evaluation_cost", "llm_judge_cost", "total_tokens"]).has(metric) || /cost|duration|latency_ms|error|retry|token|failure_count/.test(metric);
const baselineDirection = (metric: string) => lowerIsBetter(metric) ? "lower is better" : "higher is better";
const baselineScore = (metric: string, value: number) => {
  if (metric === "duration_ms" || metric.endsWith("_duration_ms")) return `${compactNumber(value)} ms`;
  if (metric === "evaluation_cost" || metric === "llm_judge_cost" || metric.endsWith("_cost")) return `$${compactNumber(value, 6)}`;
  if (metric === "total_tokens" || metric.endsWith("_tokens")) return `${compactNumber(value, 0)} tokens`;
  if (metric === "overall_score" || metric.endsWith("_score")) return `${compactNumber(value * 100, 2)}%`;
  return compactNumber(value);
};
const secondsText = (value: number | null | undefined) => typeof value === "number" ? `${compactNumber(value,2)}s` : "n/a";
const scopeLabels: Record<string, string> = { workflow_id: "Workflow", workflow: "Workflow", evaluation_type: "Evaluation", evaluation: "Evaluation", model: "Model", rubric_version: "Rubric", rubric: "Rubric", validation: "Validation" };
const scopeOrder = ["workflow_id", "workflow", "evaluation_type", "evaluation", "model", "rubric_version", "rubric", "validation"];
const scopeEntries = (scope: Record<string,string>) => Object.entries(scope).sort(([left],[right]) => {
  const leftIndex=scopeOrder.indexOf(left); const rightIndex=scopeOrder.indexOf(right);
  return (leftIndex<0 ? scopeOrder.length : leftIndex)-(rightIndex<0 ? scopeOrder.length : rightIndex) || left.localeCompare(right);
});
const regressionMetadata = (item: EvaluationRegression) => {
  if (item.metadata && typeof item.metadata === "object") return item.metadata;
  if (item.metadata_json && typeof item.metadata_json === "object") return item.metadata_json;
  if (typeof item.metadata_json === "string") {
    try { const parsed: unknown=JSON.parse(item.metadata_json); return parsed && typeof parsed === "object" ? parsed as Record<string,unknown> : {}; }
    catch { return {}; }
  }
  return {};
};
const metadataText = (metadata: Record<string,unknown>, key: string) => typeof metadata[key] === "string" && metadata[key] ? String(metadata[key]) : null;
const metadataNumber = (metadata: Record<string,unknown>, key: string) => typeof metadata[key] === "number" && Number.isFinite(metadata[key]) ? Number(metadata[key]) : null;
const metricValue = (metric: string | null, value: number | null) => metric && value !== null ? baselineScore(metric,value) : "n/a";
const regressionDirection = (metadata: Record<string,unknown>, metric: string | null) => {
  const direction=metadataText(metadata,"direction");
  return direction ? direction.replaceAll("_"," ") : metric ? baselineDirection(metric) : "n/a";
};
const evidenceSummary = (evidence: Record<string,unknown>) => Object.entries(evidence).slice(0,4).map(([key,value]) => `${label(key)}: ${typeof value === "number" ? compactNumber(value,4) : String(value)}`).join(" · ") || "n/a";
const proposalValue = (value: unknown) => typeof value === "number" ? compactNumber(value,3) : value === null || value === undefined ? "n/a" : typeof value === "object" ? "structured" : String(value);
const simulationSummary = (simulation?: Record<string,unknown> | null) => simulation ? `${Number(simulation.changed_decision_count ?? 0)} changed / ${Number(simulation.sample_size ?? 0)} samples` : "unavailable";
const rolloutMetric = (metrics: Record<string,unknown> | null | undefined, key: string) => typeof metrics?.[key] === "number" ? compactNumber(Number(metrics[key]) * (key.endsWith("_rate") || key.endsWith("_delta") ? 100 : 1),2) + (key.endsWith("_rate") || key.endsWith("_delta") ? "%" : "") : "n/a";
const experimentStatisticalSummary = (experiment: PlannerPolicyExperiment) => {
  const nested = (experiment.metrics as { comparison?: { statistical_summary?: PlannerPolicyExperiment["statistical_summary"] } } | null | undefined)?.comparison?.statistical_summary;
  return experiment.statistical_summary ?? nested ?? null;
};
const experimentStatValue = (metric: string, value: number | null | undefined) => {
  if (typeof value !== "number") return "n/a";
  return metric.endsWith("_rate") || metric.includes("rate") || metric.includes("score") || metric.includes("error")
    ? `${compactNumber(value * 100, 2)}%`
    : compactNumber(value, 3);
};
const experimentCi = (metric: string, bounds?: Array<number | null>) => bounds && bounds.length === 2 ? `${experimentStatValue(metric,bounds[0])}..${experimentStatValue(metric,bounds[1])}` : "n/a";
const experimentComparison = (experiment: PlannerPolicyExperiment, variantId: string) => {
  const summary = experimentStatisticalSummary(experiment);
  const comparison = summary?.comparisons?.[variantId];
  if (!comparison) return null;
  const metric = comparison.metric ?? experiment.primary_metric;
  const significant = comparison.adjusted_significant ? "statistical yes" : "statistical no";
  const practical = comparison.practically_significant ? "practical yes" : "practical no";
  const confidence = typeof comparison.decision_confidence === "number" ? `${comparison.decision_confidence_label ?? "confidence"} ${compactNumber(comparison.decision_confidence * 100, 1)}%` : "confidence n/a";
  return `${confidence} · CI ${experimentCi(metric,comparison.adjusted_delta_ci)} · ${significant} · ${practical} · MDE ${experimentStatValue(metric,comparison.minimum_detectable_effect)}`;
};
const experimentPromotionReadiness = (experiment: PlannerPolicyExperiment) => {
  const nested = (experiment.metrics as { promotion_readiness?: PlannerPolicyExperiment["promotion_readiness"] } | null | undefined)?.promotion_readiness;
  return experiment.promotion_readiness ?? nested ?? null;
};
const readinessCheckSummary = (readiness: NonNullable<PlannerPolicyExperiment["promotion_readiness"]>) => {
  const checks = readiness.checks ?? {};
  const keys = ["sample_sufficient", "statistical_significance", "guardrails_clear", "secondary_metrics_consistent", "temporal_stability_ok", "policy_baseline_current"];
  return keys.map(key => `${label(key)} ${checks[key] ? "yes" : "no"}`).join(" · ");
};
const experimentMetric = (experiment: PlannerPolicyExperiment, group: string) => {
  const metrics = experiment.metrics as { control?: Record<string,unknown>; variants?: Record<string,Record<string,unknown>> } | null | undefined;
  const source = group === "control" ? metrics?.control : metrics?.variants?.[group];
  const key = experiment.primary_metric === "successful_with_repair_rate" ? "repair_rate" : experiment.primary_metric === "outcome_score" ? "average_outcome_score" : experiment.primary_metric;
  return rolloutMetric(source,key);
};

function collectionValue(name: string, item: Record<string, unknown>, column: string) {
  const value = item[column];
  if (name !== "metrics") return typeof value === "number" && isScoreColumn(column) ? score(value) : String(value ?? "n/a");
  if (column === "average_value") {
    if (item.analytics_role !== "score" || typeof value !== "number") return "n/a";
    return item.unit === "score" || item.unit === "ratio" ? `${compactNumber(value * 100, 2)}%` : compactNumber(value);
  }
  if (column === "average_raw_value") {
    if (typeof value !== "number") return "n/a";
    const rawUnit = String(item.raw_unit ?? "");
    if (rawUnit === "x") return `${compactNumber(value, 2)}x`;
    if (rawUnit === "ms") return `${compactNumber(value)} ms`;
    if (rawUnit === "ratio") return `${compactNumber(value * 100, 2)}%`;
    return compactNumber(value);
  }
  if (column === "passed_count" && item.analytics_role !== "score") return "n/a";
  return typeof value === "number" ? compactNumber(value) : String(value ?? "n/a");
}

function Layout({ children }: { children: React.ReactNode }) {
  return <div className="app-shell evaluations-page"><AppNavigation /><main><header className="evaluations-heading"><div><p className="page-eyebrow">Calidad durable</p><h1>Evaluaciones de agentes</h1></div><span title="Analytics exclude invalidated/superseded evaluation runs."><ShieldCheck className="analytics-policy" aria-label="Politica de analytics" /></span></header><nav className="evaluations-tabs" aria-label="Secciones de evaluaciones">{tabs.map(([path, text, Icon]) => <NavLink end={path === "/evaluations"} key={path} to={path}><Icon aria-hidden="true" />{text}</NavLink>)}</nav>{children}</main></div>;
}

function Loading({ error }: { error?: string | null }) { return error ? <p className="evaluation-state error">{error}</p> : <p className="evaluation-state">Cargando datos...</p>; }

function ScoreBars({ items }: { items: Array<{ key: string; score: number }> }) {
  return <div className="verdict-bars">{items.map(item => <div key={item.key}><span>{item.key}</span><i><b style={{ width: `${Math.min(100, item.score * 100)}%` }} /></i><strong>{score(item.score)}</strong></div>)}</div>;
}

function Trend({ values, labels }: { values: number[]; labels: string[] }) {
  const maximum = Math.max(1, ...values);
  return <div className="evaluation-trend">{values.map((value, index) => <i key={`${labels[index]}-${index}`} title={`${labels[index]}: ${number(value, 4)}`} style={{ height: `${Math.max(4, value / maximum * 100)}%` }} />)}</div>;
}

function Dashboard() {
  const [data, setData] = useState<EvaluationDashboard | null>(null); const [error, setError] = useState<string | null>(null);
  useEffect(() => { getEvaluationDashboard().then(setData).catch(caught => setError(caught.message)); }, []);
  if (!data) return <Layout><Loading error={error} /></Layout>;
  const kpis = [["Workflows", data.workflows_evaluated], ["Score workflow", score(data.average_workflow_score)], ["Score agentes", score(data.average_agent_score)], ["Regresiones", data.regression_count], ["Costo promedio", `$${number(data.average_evaluation_cost, 4)}`], ["Duración promedio", `${number(data.average_evaluation_duration_ms, 0)} ms`]];
  return <Layout><section className="evaluation-kpis">{kpis.map(([name, value]) => <article key={name}><span>{name}</span><strong>{value}</strong></article>)}</section><section className="evaluation-grid"><article className="evaluation-panel"><header><h2>Distribución de veredictos</h2></header><div className="verdict-bars">{Object.entries(data.verdicts).map(([name, count]) => <div key={name}><span>{label(name)}</span><i><b style={{ width: `${Math.min(100, count / Math.max(1, data.workflows_evaluated) * 100)}%` }} /></i><strong>{count}</strong></div>)}</div></article><article className="evaluation-panel"><header><h2>Score a lo largo del tiempo</h2></header><Trend values={data.score_over_time.map(item => item.score)} labels={data.score_over_time.map(item => item.created_at)} /></article><article className="evaluation-panel"><header><h2>Score por agente</h2></header><ScoreBars items={data.score_by_agent} /></article><article className="evaluation-panel"><header><h2>Score por modelo</h2></header><ScoreBars items={data.score_by_model} /></article><article className="evaluation-panel"><header><h2>Score frente a costo</h2></header><Trend values={data.score_vs_cost.map(item => item.cost)} labels={data.score_vs_cost.map(item => score(item.score))} /></article><article className="evaluation-panel"><header><h2>Score frente a duración</h2></header><Trend values={data.score_vs_duration.map(item => item.duration_ms)} labels={data.score_vs_duration.map(item => score(item.score))} /></article><article className="evaluation-panel"><header><h2>Regresiones a lo largo del tiempo</h2></header><Trend values={data.regressions_over_time.map(item => item.count)} labels={data.regressions_over_time.map(item => item.created_at)} /></article></section></Layout>;
}

function Runs() {
  const [items, setItems] = useState<EvaluationRun[]>([]); const [error, setError] = useState<string | null>(null); const [refresh, setRefresh] = useState(0);
  useEffect(() => { getEvaluationRuns().then(result => setItems(result.items)).catch(caught => setError(caught.message)); }, [refresh]);
  return <Layout><section className="evaluation-panel"><header><div><h2>Runs recientes</h2><p>{items.length} evaluaciones</p></div><button className="icon-button" title="Actualizar" onClick={() => setRefresh(value => value + 1)}><RefreshCw aria-hidden="true" /></button></header>{error ? <Loading error={error} /> : <div className="evaluation-table"><table><thead><tr><th>Workflow</th><th>Tipo</th><th>Estado</th><th>Validez</th><th>Score</th><th>Veredicto</th><th>Duración</th></tr></thead><tbody>{items.map(item => <tr key={item.evaluation_run_id}><td><Link to={`/evaluations/runs/${item.evaluation_run_id}`}>{item.workflow_id}</Link><small>{item.branch_id}</small></td><td>{label(item.evaluation_type)}</td><td><span className="evaluation-status" data-status={item.status}>{item.status}</span></td><td><span className="evaluation-status" data-status={validity(item)} title={item.invalidation_reason ?? undefined}>{validity(item)}</span></td><td>{score(item.overall_score)}</td><td>{item.verdict ? label(item.verdict) : "n/a"}</td><td>{number(item.duration_ms, 0)} ms</td></tr>)}</tbody></table></div>}</section></Layout>;
}

function RunDetail() {
  const { runId = "" } = useParams(); const [run, setRun] = useState<EvaluationRun | null>(null); const [error, setError] = useState<string | null>(null);
  useEffect(() => { getEvaluationRun(runId).then(setRun).catch(caught => setError(caught.message)); }, [runId]);
  if (!run) return <Layout><Loading error={error} /></Layout>;
  const groups = { deterministic: run.metrics?.filter(item => item.metric_type === "deterministic") ?? [], heuristic: run.metrics?.filter(item => item.metric_type === "heuristic") ?? [], dimensions: run.metrics?.filter(item => item.metric_type === "heuristic_dimension") ?? [], judge: run.metrics?.filter(item => item.metric_type === "llm_judge") ?? [] };
  return <Layout><header className="evaluation-detail-head"><div><Link to="/evaluations/runs">Runs</Link><h2>{run.workflow_id}</h2><code>{run.evaluation_run_id}</code></div><div className="evaluation-score"><strong>{score(run.overall_score)}</strong><span>{run.verdict ? label(run.verdict) : run.status}</span></div></header><section className="agent-score-grid">{run.results?.map(result => <article key={String(result.evaluation_result_id)}><span>{String(result.agent_name)}</span><strong>{score(result.score)}</strong><small>{label(String(result.verdict))}</small></article>)}</section><section className="evaluation-grid">{Object.entries(groups).map(([name, metrics]) => <article className="evaluation-panel" key={name}><header><h2>{label(name)}</h2></header><ul className="metric-list">{metrics.map(item => <li key={String(item.metric_id)}><span>{label(String(item.metric_name))}</span><strong>{score(item.metric_value)}</strong><i data-passed={String(item.passed)} /></li>)}</ul></article>)}</section><section className="evaluation-panel"><header><h2>Evidencia referencial</h2></header><div className="evaluation-table"><table><thead><tr><th>Tipo</th><th>Referencia</th><th>Resumen</th></tr></thead><tbody>{run.evidence?.map(item => <tr key={String(item.evidence_id)}><td>{label(String(item.evidence_type))}</td><td><code>{String(item.reference_id ?? "n/a")}</code></td><td>{String(item.summary)}</td></tr>)}</tbody></table></div></section></Layout>;
}

function RubricsList() {
  const [items, setItems] = useState<EvaluationRubric[]>([]); const [error, setError] = useState<string | null>(null);
  useEffect(() => { getEvaluationCollection<EvaluationRubric>("rubrics").then(result => setItems(result.items)).catch(caught => setError(caught.message)); }, []);
  return <Layout><section className="evaluation-panel"><header><div><h2>Rubrics</h2><p>{items.length} versiones durables</p></div></header>{error ? <Loading error={error} /> : <div className="evaluation-table"><table><thead><tr><th>Name</th><th>Version</th><th>Agent</th><th>Status</th><th>Created</th><th>Updated</th><th>Action</th></tr></thead><tbody>{items.map(item => <tr key={item.rubric_id}><td><Link to={`/evaluations/rubrics/${item.rubric_id}`}>{item.name}</Link></td><td>{item.version}</td><td>{item.agent_name}</td><td><span className="evaluation-status" data-status={item.version_status}>{item.version_status.toUpperCase()}</span></td><td>{humanDate(item.created_at)}</td><td>{humanDate(item.updated_at)}</td><td><Link to={`/evaluations/rubrics/${item.rubric_id}`}>View</Link></td></tr>)}</tbody></table>{!items.length && <p className="evaluation-state">Sin registros.</p>}</div>}</section></Layout>;
}

function RubricDetail() {
  const { rubricId = "" } = useParams(); const [item, setItem] = useState<EvaluationRubric | null>(null); const [error, setError] = useState<string | null>(null); const [editing, setEditing] = useState(false); const [saving, setSaving] = useState(false);
  const [version, setVersion] = useState(""); const [weights, setWeights] = useState<Record<string, number>>({});
  useEffect(() => { getEvaluationRubric(rubricId).then(value => { setItem(value); setVersion(nextVersion(value.version)); setWeights(value.weights); setError(null); }).catch(caught => setError(caught.message)); }, [rubricId]);
  if (!item) return <Layout><Loading error={error} /></Layout>;
  const total=Object.values(weights).reduce((sum,value) => sum + Number(value || 0),0); const validWeights=Math.abs(total-1)<1e-6 && Object.keys(weights).length===Object.keys(item.dimensions).length;
  const createVersion = async () => { if (!validWeights)return; setSaving(true); try { const created=await createEvaluationRubricVersion(item.rubric_id,{ version, dimensions:item.dimensions, weights, thresholds:item.thresholds, enabled:true }); setItem(created); setVersion(nextVersion(created.version)); setWeights(created.weights); setEditing(false); setError(null); } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); } finally { setSaving(false); } };
  const disable = async () => { setSaving(true); try { setItem(await disableEvaluationRubric(item.rubric_id)); setError(null); } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); } finally { setSaving(false); } };
  return <Layout><header className="evaluation-detail-head rubric-detail-head"><div><Link to="/evaluations/rubrics">Rubrics</Link><h2>{item.name}</h2><p>{item.agent_name} · version {item.version}</p></div><div className="rubric-actions"><span className="evaluation-status" data-status={item.version_status}>{item.version_status.toUpperCase()}</span><button type="button" onClick={() => setEditing(value => !value)}><Plus aria-hidden="true" />Create new version</button>{item.version_status === "current" && item.enabled && <button type="button" disabled={saving} onClick={disable}><XCircle aria-hidden="true" />Disable</button>}</div></header>{error && <p className="evaluation-state error">{error}</p>}<section className="rubric-meta"><div><span>Agent</span><strong>{item.agent_name}</strong></div><div><span>Version</span><strong>{item.version}</strong></div><div><span>Created</span><strong>{humanDate(item.created_at)}</strong></div><div><span>Updated</span><strong>{humanDate(item.updated_at)}</strong></div></section><section className="evaluation-grid"><article className="evaluation-panel"><header><h2>Dimensions and weights</h2><strong className={validWeights ? "weight-valid" : "weight-invalid"}>Total {compactNumber(total * 100,2)}%</strong></header><div className="evaluation-table"><table><thead><tr><th>Dimension</th><th>Weight</th></tr></thead><tbody>{Object.keys(item.dimensions).map(name => <tr key={name}><td>{name}</td><td>{editing ? <input aria-label={`${name} weight`} type="number" min="0" max="1" step="0.01" value={weights[name] ?? 0} onChange={event => setWeights(current => ({ ...current,[name]:Number(event.target.value) }))} /> : `${compactNumber(item.weights[name] * 100,2)}%`}</td></tr>)}</tbody></table></div>{!validWeights && <p className="rubric-validation" role="alert">Weights must total 100% and match every dimension.</p>}{editing && <footer className="rubric-editor"><label>New version<input aria-label="New version" value={version} onChange={event => setVersion(event.target.value)} /></label><button type="button" disabled={!validWeights || !version.trim() || saving} onClick={createVersion}><Plus aria-hidden="true" />Create version</button></footer>}</article><article className="evaluation-panel"><header><h2>Verdict thresholds</h2></header><ul className="threshold-list">{Object.entries(item.verdict_thresholds ?? item.thresholds).map(([name,value]) => <li key={name}><span>{label(name)}</span><strong>{compactNumber(value * 100,2)}%</strong></li>)}</ul></article></section><section className="evaluation-panel"><header><div><h2>Version history</h2><p>Historical evaluations retain their original rubric version.</p></div></header><div className="evaluation-table"><table><thead><tr><th>Version</th><th>Status</th><th>Created</th><th>Valid evaluations</th><th>Total evaluations</th></tr></thead><tbody>{item.history?.map(history => <tr key={history.rubric_id}><td><Link to={`/evaluations/rubrics/${history.rubric_id}`}>v{history.version}</Link></td><td><span className="evaluation-status" data-status={history.version_status}>{history.version_status.toUpperCase()}</span></td><td>{humanDate(history.created_at)}</td><td>{history.evaluations_valid}</td><td>{history.evaluations_total}</td></tr>)}</tbody></table></div></section></Layout>;
}

function BaselinesList() {
  const [items, setItems] = useState<EvaluationBaseline[]>([]); const [error, setError] = useState<string | null>(null);
  useEffect(() => { getEvaluationCollection<EvaluationBaseline>("baselines").then(result => setItems(result.items)).catch(caught => setError(caught.message)); }, []);
  return <Layout><section className="evaluation-panel"><header><div><h2>Baselines</h2><p>{items.length} referencias activas</p></div></header>{error ? <Loading error={error} /> : <div className="evaluation-table baseline-table"><table><thead><tr><th>Baseline ID</th><th>Scope</th><th>Metric</th><th>Direction</th><th>Score</th><th>Sample count</th><th>Status</th><th>Created</th></tr></thead><tbody>{items.map(item => <tr key={item.baseline_id}><td><code title={item.baseline_id}>{item.baseline_id.slice(0,8)}</code></td><td><div className="baseline-scope">{scopeEntries(item.scope).length ? scopeEntries(item.scope).map(([key,value]) => <span key={key}><small>{scopeLabels[key] ?? label(key)}</small><strong>{value}</strong></span>) : <span><small>Scope</small><strong>Global</strong></span>}</div></td><td><code>{item.metric}</code></td><td>{baselineDirection(item.metric)}</td><td>{baselineScore(item.metric,item.score)}</td><td>{compactNumber(item.sample_count,0)}</td><td><span className="evaluation-status" data-status="current">ACTIVE</span></td><td>{humanDate(item.created_at)}</td></tr>)}</tbody></table>{!items.length && <p className="evaluation-state">Sin registros.</p>}</div>}</section></Layout>;
}

function RegressionsList() {
  const [items, setItems] = useState<EvaluationRegression[]>([]); const [error, setError] = useState<string | null>(null);
  useEffect(() => { getEvaluationCollection<EvaluationRegression>("regressions").then(result => setItems(result.items)).catch(caught => setError(caught.message)); }, []);
  return <Layout><section className="evaluation-panel"><header><div><h2>Regressions</h2><p>{items.length} detecciones durables</p></div></header>{error ? <Loading error={error} /> : <div className="evaluation-table regression-table"><table><thead><tr><th>Type</th><th>Metric</th><th>Baseline</th><th>Current</th><th>Delta</th><th>Threshold</th><th>Direction</th><th>Status</th><th>Run</th><th>Created</th></tr></thead><tbody>{items.map(item => {
    const metadata=regressionMetadata(item); const metric=metadataText(metadata,"metric");
    const baselineId=metadataText(metadata,"baseline_id") ?? item.reference_id;
    const runId=metadataText(metadata,"evaluation_run_id") ?? item.evaluation_run_id;
    const status=metadataText(metadata,"status") ?? "regression";
    return <tr key={item.evidence_id}><td><code>{metadataText(metadata,"type") ?? "n/a"}</code></td><td><code>{metric ?? "n/a"}</code></td><td>{metricValue(metric,metadataNumber(metadata,"baseline_value"))}<small>{baselineId ? <><code title={baselineId}>{baselineId.slice(0,8)}</code><button className="inline-copy" type="button" title="Copy baseline ID" aria-label={`Copy baseline ${baselineId}`} onClick={() => void navigator.clipboard?.writeText(baselineId)}><Copy aria-hidden="true" /></button></> : "n/a"}</small></td><td>{metricValue(metric,metadataNumber(metadata,"current_value"))}</td><td>{metricValue(metric,metadataNumber(metadata,"delta"))}</td><td>{metricValue(metric,metadataNumber(metadata,"threshold"))}</td><td>{regressionDirection(metadata,metric)}</td><td><span className="evaluation-status" data-status={status}>{status.toUpperCase()}</span></td><td>{runId ? <Link title={runId} to={`/evaluations/runs/${runId}`}>{runId.slice(0,8)}</Link> : "n/a"}</td><td>{humanDate(item.created_at)}</td></tr>;
  })}</tbody></table>{!items.length && <p className="evaluation-state">Sin registros.</p>}</div>}</section></Layout>;
}

function RecommendationsList() {
  const [items, setItems] = useState<PlannerRecommendation[]>([]); const [agentItems, setAgentItems] = useState<AgentRecommendation[]>([]); const [proposals, setProposals] = useState<PlannerPolicyProposal[]>([]); const [applications, setApplications] = useState<PlannerPolicyApplication[]>([]); const [rollouts, setRollouts] = useState<PlannerPolicyRollout[]>([]); const [metrics, setMetrics] = useState<Record<string,number>>({}); const [agentMetrics, setAgentMetrics] = useState<Record<string,number>>({}); const [error, setError] = useState<string | null>(null); const [busy, setBusy] = useState<string | null>(null);
  const load = () => Promise.all([getPlannerRecommendations(), getAgentRecommendations(), getPlannerPolicyProposals(), getPlannerPolicyApplications(), getPlannerPolicyRollouts()]).then(([result, agentResult, proposalResult, applicationResult, rolloutResult]) => { setItems(result.items); setAgentItems(agentResult.items); setMetrics(result.review_metrics ?? {}); setAgentMetrics(agentResult.review_metrics ?? {}); setProposals(proposalResult.items); setApplications(applicationResult.items); setRollouts(rolloutResult.items); setError(null); }).catch(caught => setError(caught.message));
  useEffect(() => { load(); }, []);
  const act = async (item: PlannerRecommendation, action: "start" | "accept" | "reject" | "defer") => {
    const reviewer = "ui-reviewer"; const fingerprint = item.recommendation_fingerprint;
    const payload: Record<string,unknown> = action === "reject" ? { reviewer, fingerprint, reason: "Rejected from UI review." } : action === "defer" ? { reviewer, fingerprint, reason: "Deferred from UI review.", deferred_until: "later" } : action === "accept" ? { reviewer, fingerprint, notes: "Accepted conceptually from UI.", decision_reason: "Human review approved." } : { reviewer, fingerprint, notes: "Review started from UI." };
    setBusy(`${item.recommendation_id}:${action}`);
    try { await reviewPlannerRecommendation(item.recommendation_id, action, payload); await load(); } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); } finally { setBusy(null); }
  };
  const actAgent = async (item: AgentRecommendation, action: "start" | "accept" | "reject" | "defer") => {
    const reviewer = "ui-reviewer"; const fingerprint = item.recommendation_fingerprint;
    const payload: Record<string,unknown> = action === "reject" ? { reviewer, fingerprint, reason: "Rejected from UI review." } : action === "defer" ? { reviewer, fingerprint, reason: "Deferred from UI review.", deferred_until: "later" } : action === "accept" ? { reviewer, fingerprint, notes: "Accepted conceptually from UI.", decision_reason: "Human review approved." } : { reviewer, fingerprint, notes: "Review started from UI." };
    setBusy(`${item.recommendation_id}:${action}`);
    try { await reviewAgentRecommendation(item.recommendation_id, action, payload); await load(); } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); } finally { setBusy(null); }
  };
  const createProposal = async (item: PlannerRecommendation) => { setBusy(`${item.recommendation_id}:proposal`); try { await createPlannerPolicyProposal(item.recommendation_id); await load(); } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); } finally { setBusy(null); } };
  const proposalAction = async (proposal: PlannerPolicyProposal, action: "ready" | "approve" | "reject" | "cancel") => {
    const payload={ actor:"ui-reviewer", proposal_fingerprint:proposal.proposal_fingerprint, notes:`${action} from UI governance.` };
    setBusy(`${proposal.proposal_id}:${action}`); try { await transitionPlannerPolicyProposal(proposal.proposal_id,action,payload); await load(); } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); } finally { setBusy(null); }
  };
  const prepareApplication = async (proposal: PlannerPolicyProposal) => { setBusy(`${proposal.proposal_id}:prepare`); try { await preparePlannerPolicyApplication(proposal.proposal_id,{ actor:"ui-reviewer", notes:"Prepared from UI governance." }); await load(); } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); } finally { setBusy(null); } };
  const applicationAction = async (application: PlannerPolicyApplication, action: "apply" | "rollback") => {
    const payload={ actor:"ui-reviewer", application_fingerprint:application.application_fingerprint, notes:`${action} from UI governance.` };
    setBusy(`${application.application_id}:${action}`); try { if (action==="apply") await applyPlannerPolicyApplication(application.application_id,payload); else await rollbackPlannerPolicyApplication(application.application_id,payload); await load(); } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); } finally { setBusy(null); }
  };
  const prepareRollout = async (application: PlannerPolicyApplication) => { setBusy(`${application.application_id}:rollout`); try { await preparePlannerPolicyRollout(application.application_id,{ actor:"ui-reviewer", notes:"Prepared rollout from UI governance." }); await load(); } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); } finally { setBusy(null); } };
  const rolloutAction = async (rollout: PlannerPolicyRollout, action: "start" | "evaluate" | "advance" | "pause" | "resume" | "rollback") => {
    const payload={ actor:"ui-reviewer", notes:`${action} rollout from UI governance.` };
    setBusy(`${rollout.rollout_id}:${action}`);
    try {
      if (action==="start") await startPlannerPolicyRollout(rollout.rollout_id,payload);
      else if (action==="evaluate") await evaluatePlannerPolicyRollout(rollout.rollout_id,payload);
      else if (action==="advance") await advancePlannerPolicyRollout(rollout.rollout_id,payload);
      else if (action==="pause") await pausePlannerPolicyRollout(rollout.rollout_id,payload);
      else if (action==="resume") await resumePlannerPolicyRollout(rollout.rollout_id,payload);
      else await rollbackPlannerPolicyRollout(rollout.rollout_id,payload);
      await load();
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); } finally { setBusy(null); }
  };
  return <Layout><section className="evaluation-panel"><header><div><h2>Agent Recommendations</h2><p>{agentItems.length} advisory agent recommendations · {agentMetrics.recommendations_accepted ?? 0} accepted · no automatic prompt, model, routing or retry changes</p></div><button className="icon-button" title="Actualizar" onClick={load}><RefreshCw aria-hidden="true" /></button></header>{error ? <p className="evaluation-state error">{error}</p> : <div className="evaluation-table recommendation-table"><table><thead><tr><th>Agent</th><th>Recommendation</th><th>Severity</th><th>Confidence</th><th>Evidence summary</th><th>Trend</th><th>Review status</th><th>Application</th><th>Actions</th></tr></thead><tbody>{agentItems.map(item => {
    const reviewStatus=item.review?.status ?? "recommendation_only"; const disabled=reviewStatus==="accepted" || reviewStatus==="rejected";
    return <tr key={`${item.recommendation_id}-${item.recommendation_fingerprint}`}><td><code title={item.recommendation_id}>{item.agent}</code><small>{item.segment ?? "global"}</small></td><td>{label(item.type)}<small>{item.summary}</small></td><td><span className="evaluation-status" data-status={item.severity}>{item.severity.toUpperCase()}</span></td><td>{compactNumber(item.confidence * 100,2)}%</td><td>{evidenceSummary(item.evidence)}</td><td>{label(item.trend ?? "insufficient_data")}</td><td><span className="evaluation-status" data-status={reviewStatus}>{reviewStatus.toUpperCase()}</span><small>{item.review?.reviewer ?? "unassigned"}</small></td><td>{item.application_status ?? "not_applied"}</td><td><div className="recommendation-actions"><button type="button" disabled={disabled || busy!==null} onClick={() => actAgent(item,"start")} title="Start review"><Clock3 aria-hidden="true" />Review</button><button type="button" disabled={disabled || busy!==null} onClick={() => actAgent(item,"accept")} title="Accept recommendation conceptually"><CheckCircle2 aria-hidden="true" />Accept</button><button type="button" disabled={disabled || busy!==null} onClick={() => actAgent(item,"reject")} title="Reject recommendation"><XCircle aria-hidden="true" />Reject</button><button type="button" disabled={disabled || busy!==null} onClick={() => actAgent(item,"defer")} title="Defer recommendation"><PauseCircle aria-hidden="true" />Defer</button></div></td></tr>;
  })}</tbody></table>{!agentItems.length && <p className="evaluation-state">Sin recomendaciones de agentes.</p>}</div>}</section><section className="evaluation-panel"><header><div><h2>Recommendations</h2><p>{items.length} advisory recommendations · {metrics.recommendations_accepted ?? 0} accepted · {metrics.recommendations_deferred ?? 0} deferred</p></div></header>{error ? <p className="evaluation-state error">{error}</p> : <div className="evaluation-table recommendation-table"><table><thead><tr><th>Policy</th><th>Direction</th><th>Severity</th><th>Confidence</th><th>Reason codes</th><th>Review status</th><th>Application</th><th>Evidence</th><th>Actions</th></tr></thead><tbody>{items.map(item => {
    const reviewStatus=item.review?.status ?? "recommendation_only"; const disabled=reviewStatus==="accepted" || reviewStatus==="rejected";
    return <tr key={`${item.recommendation_id}-${item.recommendation_fingerprint}`}><td><code title={item.recommendation_id}>{item.policy}</code><small>{item.segment ?? "global"}</small></td><td>{label(item.direction)}</td><td><span className="evaluation-status" data-status={item.severity}>{item.severity.toUpperCase()}</span></td><td>{compactNumber(item.confidence * 100,2)}%</td><td><div className="reason-code-list">{item.reason_codes.map(code => <code key={code}>{code}</code>)}</div></td><td><span className="evaluation-status" data-status={reviewStatus}>{reviewStatus.toUpperCase()}</span><small>{item.review?.reviewer ?? "unassigned"}</small></td><td>{item.application_status ?? "not_applicable"}</td><td>{evidenceSummary(item.evidence)}</td><td><div className="recommendation-actions"><button type="button" disabled={disabled || busy!==null} onClick={() => act(item,"start")} title="Start review"><Clock3 aria-hidden="true" />Review</button><button type="button" disabled={disabled || busy!==null} onClick={() => act(item,"accept")} title="Accept recommendation conceptually"><CheckCircle2 aria-hidden="true" />Accept</button><button type="button" disabled={disabled || busy!==null} onClick={() => act(item,"reject")} title="Reject recommendation"><XCircle aria-hidden="true" />Reject</button><button type="button" disabled={disabled || busy!==null} onClick={() => act(item,"defer")} title="Defer recommendation"><PauseCircle aria-hidden="true" />Defer</button>{reviewStatus==="accepted" && <button type="button" disabled={busy!==null} onClick={() => createProposal(item)} title="Create policy proposal"><Plus aria-hidden="true" />Create Policy Proposal</button>}</div></td></tr>;
  })}</tbody></table>{!items.length && <p className="evaluation-state">Sin recomendaciones revisables.</p>}</div>}</section><section className="evaluation-panel"><header><div><h2>Policy proposals</h2><p>{proposals.length} durable proposals · no runtime apply in this phase</p></div></header><div className="evaluation-table policy-proposal-table"><table><thead><tr><th>Policy</th><th>Current → Proposed</th><th>Change</th><th>Risk</th><th>Simulation</th><th>Status</th><th>Application</th><th>Actions</th></tr></thead><tbody>{proposals.map(proposal => {
    const terminal=["approved","rejected","cancelled"].includes(proposal.status);
    const warn=proposal.safety_flags?.reduces_safety || ["high","critical"].includes(proposal.proposal_risk_level);
    return <tr key={proposal.proposal_id}><td><code title={proposal.proposal_id}>{proposal.policy_key}</code><small>{proposal.policy_scope}</small></td><td>{proposalValue(proposal.current_value)} → {proposalValue(proposal.proposed_value)}</td><td>{label(proposal.change_type)}{warn && <small>Safety review required</small>}</td><td><span className="evaluation-status" data-status={proposal.proposal_risk_level}>{proposal.proposal_risk_level.toUpperCase()}</span></td><td>{simulationSummary(proposal.simulation)}</td><td><span className="evaluation-status" data-status={proposal.status}>{proposal.status.toUpperCase()}</span></td><td>{proposal.application_status}</td><td><div className="recommendation-actions"><button type="button" disabled={proposal.status!=="draft" || busy!==null} onClick={() => proposalAction(proposal,"ready")}><Clock3 aria-hidden="true" />Ready for Review</button><button type="button" disabled={proposal.status!=="ready_for_review" || busy!==null} onClick={() => proposalAction(proposal,"approve")}><CheckCircle2 aria-hidden="true" />Approve</button><button type="button" disabled={proposal.status!=="ready_for_review" || busy!==null} onClick={() => proposalAction(proposal,"reject")}><XCircle aria-hidden="true" />Reject</button><button type="button" disabled={terminal || busy!==null} onClick={() => proposalAction(proposal,"cancel")}><PauseCircle aria-hidden="true" />Cancel</button><button type="button" disabled={proposal.status!=="approved" || proposal.application_status!=="not_applied" || proposal.change_type==="review_only" || busy!==null} onClick={() => prepareApplication(proposal)}><Plus aria-hidden="true" />Prepare Application</button></div></td></tr>;
  })}</tbody></table>{!proposals.length && <p className="evaluation-state">Sin propuestas de policy.</p>}</div></section><section className="evaluation-panel"><header><div><h2>Policy applications</h2><p>{applications.length} application attempts · explicit apply and rollback only</p></div></header><div className="evaluation-table policy-application-table"><table><thead><tr><th>Policy</th><th>Previous → Proposed</th><th>Revision</th><th>Verification</th><th>Status</th><th>Error</th><th>Actions</th></tr></thead><tbody>{applications.map(application => <tr key={application.application_id}><td><code title={application.application_id}>{application.policy_key}</code><small>{application.scope}</small></td><td>{proposalValue(application.previous_value)} → {proposalValue(application.proposed_value)}</td><td>{application.baseline_revision} → {application.applied_revision ?? application.rollback_revision ?? "pending"}</td><td>{application.verification_strategy}<small>{application.rollback_supported ? "Rollback available" : "Rollback unavailable"}</small></td><td><span className="evaluation-status" data-status={application.status}>{application.status.toUpperCase()}</span></td><td>{application.error_code ?? "n/a"}</td><td><div className="recommendation-actions"><button type="button" disabled={application.status!=="prepared" || busy!==null} onClick={() => applicationAction(application,"apply")}><CheckCircle2 aria-hidden="true" />Apply</button><button type="button" disabled={application.status!=="applied" || !application.rollback_supported || busy!==null} onClick={() => applicationAction(application,"rollback")}><PauseCircle aria-hidden="true" />Rollback</button><button type="button" disabled={application.status!=="applied" || busy!==null} onClick={() => prepareRollout(application)}><Plus aria-hidden="true" />Prepare Rollout</button></div></td></tr>)}</tbody></table>{!applications.length && <p className="evaluation-state">Sin aplicaciones preparadas.</p>}</div></section><section className="evaluation-panel"><header><div><h2>Policy Rollouts</h2><p>{rollouts.length} staged rollouts · manual canary and impact monitoring</p></div></header><div className="evaluation-table policy-rollout-table"><table><thead><tr><th>Policy</th><th>Previous → Target</th><th>Status</th><th>Current %</th><th>Health</th><th>Sample</th><th>Treatment / Control</th><th>Actions</th></tr></thead><tbody>{rollouts.map(rollout => <tr key={rollout.rollout_id}><td><code title={rollout.rollout_id}>{rollout.policy_key}</code><small>{rollout.scope}</small></td><td>{proposalValue(rollout.previous_value)} → {proposalValue(rollout.target_value)}</td><td><span className="evaluation-status" data-status={rollout.status}>{rollout.status.toUpperCase()}</span><small>{rollout.error_code ?? rollout.strategy}</small></td><td>{rollout.current_percentage}%<small>stages {(rollout.stages ?? []).join(" / ")}</small></td><td><span className="evaluation-status" data-status={rollout.health_status}>{rollout.health_status.toUpperCase()}</span><small>{rollout.health_score}/100</small></td><td>{rolloutMetric(rollout.treatment_metrics,"policy_sample_size")}</td><td><span>failure {rolloutMetric(rollout.treatment_metrics,"failure_rate")} / {rolloutMetric(rollout.control_metrics,"failure_rate")}</span><small>repair delta {rolloutMetric(rollout.delta_metrics,"repair_rate_delta")}</small></td><td><div className="recommendation-actions"><button type="button" disabled={rollout.status!=="prepared" || busy!==null} onClick={() => rolloutAction(rollout,"start")}><CheckCircle2 aria-hidden="true" />Start</button><button type="button" disabled={["completed","rolled_back","failed"].includes(rollout.status) || busy!==null} onClick={() => rolloutAction(rollout,"evaluate")}><Gauge aria-hidden="true" />Evaluate</button><button type="button" disabled={!["canary","expanding"].includes(rollout.status) || rollout.health_status!=="healthy" || busy!==null} onClick={() => rolloutAction(rollout,"advance")}><Plus aria-hidden="true" />Advance</button><button type="button" disabled={!["canary","expanding","observing"].includes(rollout.status) || busy!==null} onClick={() => rolloutAction(rollout,"pause")}><PauseCircle aria-hidden="true" />Pause</button><button type="button" disabled={rollout.status!=="paused" || busy!==null} onClick={() => rolloutAction(rollout,"resume")}><RefreshCw aria-hidden="true" />Resume</button><button type="button" disabled={["completed","rolled_back","failed"].includes(rollout.status) || busy!==null} onClick={() => rolloutAction(rollout,"rollback")}><XCircle aria-hidden="true" />Rollback</button></div></td></tr>)}</tbody></table>{!rollouts.length && <p className="evaluation-state">Sin rollouts preparados.</p>}</div></section></Layout>;
}

function Collection({ name }: { name: string }) {
  const [items, setItems] = useState<Array<Record<string, unknown>>>([]); const [error, setError] = useState<string | null>(null);
  useEffect(() => { getEvaluationCollection<Record<string, unknown>>(name).then(result => setItems(result.items)).catch(caught => setError(caught.message)); }, [name]);
  const columns = name === "metrics" ? ["metric_name", "metric_type", "unit", "samples", "average_value", "average_raw_value", "passed_count"] : Array.from(new Set(items.flatMap(Object.keys))).filter(key => !key.endsWith("_id") && !["dimensions", "weights", "thresholds", "scope"].includes(key)).slice(0, 7);
  return <Layout><section className="evaluation-panel"><header><div><h2>{name[0].toUpperCase() + name.slice(1)}</h2><p>{items.length} registros</p></div></header>{error ? <Loading error={error} /> : <div className="evaluation-table"><table><thead><tr>{columns.map(column => <th key={column}>{label(column)}</th>)}</tr></thead><tbody>{items.map((item, index) => <tr key={String(item.rubric_id || item.baseline_id || item.agent || item.metric_name || index)}>{columns.map(column => <td key={column}>{collectionValue(name, item, column)}</td>)}</tr>)}</tbody></table>{!items.length && <p className="evaluation-state">Sin registros.</p>}</div>}</section></Layout>;
}

function CIOperations() {
  const [data, setData] = useState<CIOperationalMetrics | null>(null); const [error, setError] = useState<string | null>(null);
  useEffect(() => { getCIOperationalMetrics().then(setData).catch(caught => setError(caught.message)); }, []);
  if (!data) return <Layout><Loading error={error} /></Layout>;
  const kpis = [
    ["Total Runs", data.summary.total_runs],
    ["Acceptance Rate", score(data.summary.acceptance_rate)],
    ["Rejection Rate", score(data.summary.rejection_rate)],
    ["Warnings", data.summary.accepted_with_warnings_runs],
    ["Average Duration", secondsText(data.summary.average_pipeline_duration_seconds)],
    ["P95 Duration", secondsText(data.summary.p95_pipeline_duration_seconds)],
    ["Repair Success Rate", score(data.repair.ci_repair_success_rate)],
    ["Repair Exhaustion Rate", score(data.repair.ci_repair_exhaustion_rate)],
    ["Promotion Eligibility Rate", score(data.promotion.promotion_eligibility_rate)],
  ];
  const failureRows = Object.entries(data.failures.by_failure_type);
  const gateRows = Object.entries(data.gates);
  return <Layout><section className="evaluation-kpis">{kpis.map(([name,value]) => <article key={name}><span>{name}</span><strong>{value}</strong></article>)}</section><section className="evaluation-grid"><article className="evaluation-panel"><header><h2>Failure Breakdown</h2></header><div className="evaluation-table"><table><thead><tr><th>Failure Type</th><th>Count</th><th>Rate</th></tr></thead><tbody>{failureRows.map(([type,count]) => <tr key={type}><td>{label(type)}</td><td>{count}</td><td>{score(count / Math.max(1, data.summary.rejected_runs))}</td></tr>)}</tbody></table>{!failureRows.length && <p className="evaluation-state">Sin fallos CI.</p>}</div></article><article className="evaluation-panel"><header><h2>Gate Rates</h2></header><div className="evaluation-table"><table><thead><tr><th>Gate</th><th>Evaluated</th><th>Failure</th><th>Warning</th></tr></thead><tbody>{gateRows.map(([gate,metrics]) => <tr key={gate}><td>{label(gate)}</td><td>{metrics.evaluated_count}</td><td>{score(metrics.failure_rate)}</td><td>{score(metrics.warning_rate)}</td></tr>)}</tbody></table></div></article><article className="evaluation-panel"><header><h2>Step Durations</h2></header><div className="evaluation-table"><table><thead><tr><th>Step</th><th>Runs</th><th>Failures</th><th>Average</th><th>P95</th></tr></thead><tbody>{Object.entries(data.steps).map(([step,metrics]) => <tr key={step}><td>{label(step)}</td><td>{metrics.run_count}</td><td>{score(metrics.failure_rate)}</td><td>{secondsText(metrics.average_duration_seconds)}</td><td>{secondsText(metrics.p95_duration_seconds)}</td></tr>)}</tbody></table></div></article><article className="evaluation-panel"><header><h2>Repair & Promotion</h2></header><dl className="git-facts"><div><dt>Repair required</dt><dd>{data.repair.ci_repair_required_count}</dd></div><div><dt>Recovered</dt><dd>{data.repair.runs_recovered_after_repair}</dd></div><div><dt>Failed after repair</dt><dd>{data.repair.runs_failed_after_repair}</dd></div><div><dt>Max attempts</dt><dd>{data.repair.maximum_ci_repair_attempts_observed}</dd></div><div><dt>Promotion eligible</dt><dd>{data.promotion.promotion_eligible_count}</dd></div><div><dt>Promotion blocked</dt><dd>{data.promotion.promotion_blocked_count}</dd></div></dl></article></section></Layout>;
}

function PolicyExperiments() {
  const [items, setItems] = useState<PlannerPolicyExperiment[]>([]); const [portfolio, setPortfolio] = useState<PlannerPolicyExperimentPortfolio | null>(null); const [error, setError] = useState<string | null>(null); const [busy, setBusy] = useState<string | null>(null);
  const load = () => Promise.all([getPlannerPolicyExperiments(), getPlannerPolicyExperimentPortfolio()]).then(([result, portfolioResult]) => { setItems(result.items); setPortfolio(portfolioResult); setError(null); }).catch(caught => setError(caught.message));
  useEffect(() => { load(); }, []);
  const act = async (experiment: PlannerPolicyExperiment, action: "ready" | "start" | "evaluate" | "pause" | "resume" | "complete" | "cancel") => {
    const payload={ actor:"ui-reviewer", notes:`${action} experiment from UI governance.` };
    setBusy(`${experiment.experiment_id}:${action}`);
    try {
      if (action==="ready") await readyPlannerPolicyExperiment(experiment.experiment_id,payload);
      else if (action==="start") await startPlannerPolicyExperiment(experiment.experiment_id,payload);
      else if (action==="evaluate") await evaluatePlannerPolicyExperiment(experiment.experiment_id,payload);
      else if (action==="pause") await pausePlannerPolicyExperiment(experiment.experiment_id,payload);
      else if (action==="resume") await resumePlannerPolicyExperiment(experiment.experiment_id,payload);
      else if (action==="complete") await completePlannerPolicyExperiment(experiment.experiment_id,payload);
      else await cancelPlannerPolicyExperiment(experiment.experiment_id,payload);
      await load();
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); } finally { setBusy(null); }
  };
  return <Layout><section className="evaluation-panel"><header><div><h2>Policy Experiments</h2><p>{items.length} multi-variant experiments · no auto-promote</p></div><button className="icon-button" title="Actualizar" onClick={load}><RefreshCw aria-hidden="true" /></button></header>{error ? <Loading error={error} /> : <><div className="evaluation-kpi-row" aria-label="Experiment Portfolio"><article><span>Active Experiments</span><strong>{portfolio?.active_experiments.length ?? 0}</strong></article><article><span>Active Rollouts</span><strong>{portfolio?.active_rollouts.length ?? 0}</strong></article><article><span>Blocking Conflicts</span><strong>{portfolio?.blocking_conflict_count ?? 0}</strong></article><article><span>Warnings</span><strong>{portfolio?.warning_count ?? 0}</strong></article><article><span>Isolation Status</span><strong>{(portfolio?.isolation_status ?? "unknown").toUpperCase()}</strong></article></div><div className="evaluation-table policy-experiment-table"><table><thead><tr><th>Policy</th><th>Control</th><th>Variants</th><th>Allocation</th><th>Status</th><th>Primary metric</th><th>Result</th><th>Actions</th></tr></thead><tbody>{items.map(experiment => { const summary=experimentStatisticalSummary(experiment); const readiness=experimentPromotionReadiness(experiment); return <tr key={experiment.experiment_id}><td><code title={experiment.experiment_id}>{experiment.policy_key}</code><small>{experiment.scope}</small></td><td>{proposalValue(experiment.control_value)}<small>{experimentMetric(experiment,"control")}</small></td><td>{experiment.variants.map(variant => <span key={variant.variant_id}>{variant.name}: {proposalValue(variant.value)} <small>{experimentMetric(experiment,variant.variant_id)}{experimentComparison(experiment,variant.variant_id) ? ` · ${experimentComparison(experiment,variant.variant_id)}` : ""}</small></span>)}</td><td>{Object.entries(experiment.allocation).map(([key,value]) => <span key={key}>{key} {value}%</span>)}</td><td><span className="evaluation-status" data-status={experiment.status}>{experiment.status.toUpperCase()}</span></td><td>{label(experiment.primary_metric)}</td><td><span className="evaluation-status" data-status={experiment.result ?? "pending"}>{(experiment.result ?? "pending").toUpperCase()}</span><small>{experiment.winner_variant_id ? `winner ${experiment.winner_variant_id}` : "No winner applied"}</small>{summary && <small>{`confidence ${summary.decision_confidence_label ?? "n/a"} ${typeof summary.decision_confidence === "number" ? compactNumber(summary.decision_confidence * 100,1) + "%" : ""}`}</small>}{readiness && <div className="reason-code-list" aria-label="Promotion Readiness"><span className="evaluation-status" data-status={readiness.status}>{readiness.status.toUpperCase()}</span><code>{`score ${readiness.score}`}</code><code>{`confidence ${compactNumber(readiness.confidence * 100,1)}%`}</code><code>{readiness.recommended_action ?? "n/a"}</code><small>{readinessCheckSummary(readiness)}</small><small>{`secondary ${readiness.secondary_metric_consistency?.status ?? "n/a"} · temporal ${readiness.temporal_stability?.status ?? "n/a"} · risk ${readiness.policy_risk?.risk_level ?? "n/a"}`}</small></div>}</td><td><div className="recommendation-actions"><button type="button" disabled={experiment.status!=="draft" || busy!==null} onClick={() => act(experiment,"ready")}><CheckCircle2 aria-hidden="true" />Ready</button><button type="button" disabled={experiment.status!=="ready" || busy!==null} onClick={() => act(experiment,"start")}><CheckCircle2 aria-hidden="true" />Start</button><button type="button" disabled={["completed","cancelled","failed"].includes(experiment.status) || busy!==null} onClick={() => act(experiment,"evaluate")}><Gauge aria-hidden="true" />Evaluate</button><button type="button" disabled={!["running","observing"].includes(experiment.status) || busy!==null} onClick={() => act(experiment,"pause")}><PauseCircle aria-hidden="true" />Pause</button><button type="button" disabled={experiment.status!=="paused" || busy!==null} onClick={() => act(experiment,"resume")}><RefreshCw aria-hidden="true" />Resume</button><button type="button" disabled={!["running","observing","paused"].includes(experiment.status) || experiment.result==="insufficient_data" || busy!==null} onClick={() => act(experiment,"complete")}><CheckCircle2 aria-hidden="true" />Complete</button><button type="button" disabled={["completed","cancelled","failed"].includes(experiment.status) || busy!==null} onClick={() => act(experiment,"cancel")}><XCircle aria-hidden="true" />Cancel</button></div></td></tr>; })}</tbody></table>{!items.length && <p className="evaluation-state">Sin experimentos preparados.</p>}</div></>}</section></Layout>;
}

export function EvaluationsPage() {
  const path = useLocation().pathname;
  if (path === "/evaluations") return <Dashboard />;
  if (path === "/evaluations/runs") return <Runs />;
  if (path.startsWith("/evaluations/runs/")) return <RunDetail />;
  if (path === "/evaluations/rubrics") return <RubricsList />;
  if (path.startsWith("/evaluations/rubrics/")) return <RubricDetail />;
  if (path === "/evaluations/baselines") return <BaselinesList />;
  if (path === "/evaluations/regressions") return <RegressionsList />;
  if (path === "/evaluations/recommendations") return <RecommendationsList />;
  if (path === "/evaluations/experiments") return <PolicyExperiments />;
  if (path === "/evaluations/ci") return <CIOperations />;
  return <Collection name={path.split("/").pop() || "agents"} />;
}

