import type { ReactNode } from "react";
import type { Pricing } from "../../api/llmCosts";
import { pricingLabels } from "./finOpsVisualState";
import type { CostVisualState } from "./finOpsVisualState";

const labels: Record<CostVisualState, string> = {
  real: "REAL", estimated: "ESTIMADO", "no-pricing": "SIN PRICING",
  "no-usage": "SIN USAGE", partial: "PARCIAL", invalid: "INVÁLIDO",
};

export function CostStateBadge({ state, technical }: { state: CostVisualState; technical?: string | null }) {
  return <span className={`cost-badge cost-state-${state}`} title={technical ?? labels[state]} aria-label={`Estado de coste: ${labels[state]}`}>{labels[state]}</span>;
}

export function UsageSource({ source }: { source: string }) {
  const friendly = source === "provider_reported" ? "Proveedor" : source === "tokenizer_estimated" ? "Estimado" : source === "unavailable" ? "Sin usage" : source;
  return <span title={source}>{friendly}</span>;
}

export function PricingBadges({ pricing }: { pricing: Pricing }) {
  return <span className="badge-row">{pricingLabels(pricing).map((label) => <span key={label} className={`cost-badge pricing-${label.toLowerCase().replaceAll(" ", "-")}`}>{label}</span>)}</span>;
}

const budgetLabels: Record<string, string> = { within_budget: "HEALTHY", warning: "WARNING", exceeded: "EXCEEDED", blocked: "BLOCKED", disabled: "DISABLED", observe_only: "OBSERVE ONLY", warn: "WARN", soft_limit: "SOFT LIMIT", hard_limit: "HARD LIMIT" };
export function BudgetBadge({ status }: { status: string }) { return <span className={`cost-badge budget-${status}`} aria-label={`Estado de presupuesto: ${budgetLabels[status] ?? status}`}>{budgetLabels[status] ?? status.replaceAll("_", " ").toUpperCase()}</span>; }

export function ProgressBar({ value, label }: { value: number; label: string }) {
  const width = Math.max(0, Math.min(value, 100));
  return <div className="budget-progress" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(width)}><span style={{ width: `${width}%` }} /></div>;
}

export interface ChartDatum { label: string; value: number; formatted: string }
export function BarChart({ title, subtitle, data, empty = "Sin datos para los filtros actuales" }: { title: string; subtitle: string; data: ChartDatum[]; empty?: string }) {
  const max = Math.max(...data.map((item) => item.value), 0);
  return <section className="finops-chart" aria-label={title}><header><div><h2>{title}</h2><p>{subtitle}</p></div></header>{data.length === 0 || max === 0 ? <p className="chart-empty">{empty}</p> : <div className="chart-bars">{data.map((item) => <div key={item.label} title={`${item.label}: ${item.formatted}`}><span>{item.label}</span><i><b style={{ width: `${item.value / max * 100}%` }} /></i><strong>{item.formatted}</strong></div>)}</div>}</section>;
}

export function DetailSection({ title, children }: { title: string; children: ReactNode }) { return <section className="finops-detail-section"><h3>{title}</h3>{children}</section>; }
export function Facts({ entries }: { entries: Array<[string, ReactNode]> }) { return <dl className="finops-facts">{entries.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value ?? "n/a"}</dd></div>)}</dl>; }
