import type { EvaluationGrade, EvaluationSeverity } from "../api/types";

export const GRADE_LABELS: Record<EvaluationGrade, string> = {
  excellent: "Excelente",
  good: "Bueno",
  acceptable: "Aceptable",
  poor: "Bajo",
  critical: "Crítico",
};

export const SEVERITY_LABELS: Record<EvaluationSeverity, string> = {
  info: "Información",
  warning: "Advertencia",
  error: "Error",
  critical: "Crítico",
};

export function formatEvaluationDuration(value: number | null): string {
  if (value === null) return "n/a";
  if (value < 1) return `${Math.round(value * 1_000)} ms`;
  if (value < 60) return `${value.toFixed(1)} s`;
  const minutes = Math.floor(value / 60);
  return `${minutes} min ${Math.round(value % 60)} s`;
}
