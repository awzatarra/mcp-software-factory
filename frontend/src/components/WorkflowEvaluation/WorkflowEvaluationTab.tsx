import {
  AlertTriangle,
  Check,
  ChevronDown,
  CircleAlert,
  Clock3,
  ExternalLink,
  FileCode2,
  LoaderCircle,
  RefreshCw,
  Route,
  Target,
} from "lucide-react";

import type {
  EvaluationCategory,
  EvaluationFinding,
  RequirementCoverage,
  WorkflowEvent,
} from "../../api/types";
import { useWorkflowEvaluation } from "../../hooks/useWorkflowEvaluation";
import { useWorkflowEvaluationStore } from "../../stores/workflowEvaluationStore";
import {
  formatEvaluationDuration,
  GRADE_LABELS,
  SEVERITY_LABELS,
} from "../../utils/evaluationFormatting";
import { ErrorBanner } from "../ErrorBanner";

const CATEGORY_LABELS = {
  planning: "Planning",
  implementation: "Implementación",
  testing: "Testing",
  efficiency: "Efficiency",
  reliability: "Reliability",
} as const;

type CategoryKey = keyof typeof CATEGORY_LABELS;

function CategoryCard({
  categoryKey,
  category,
  expanded,
  onSelect,
}: {
  categoryKey: CategoryKey;
  category: EvaluationCategory;
  expanded: boolean;
  onSelect: (category: string | null) => void;
}) {
  const detailId = `evaluation-category-${categoryKey}`;
  const pending = category.evaluation_state === "not_started";
  const partial = category.evaluation_state === "partial";
  const legacySignals = category.bonuses.filter((bonus) => bonus.points === 0);
  const positiveSignals = [...(category.positive_signals ?? []), ...legacySignals];
  const hasSignal = (code: string) =>
    positiveSignals.some((signal) => signal.code === code);
  const stateLabel = pending
    ? "Pendiente de evaluación"
    : partial && categoryKey === "implementation"
      ? "Implementación parcial"
      : categoryKey === "implementation"
        ? `Implementación evaluada · ${category.percentage?.toFixed(0)}%`
        : `${category.percentage?.toFixed(0)}%`;
  return (
    <article className={`evaluation-category ${expanded ? "is-expanded" : ""}`}>
      <button
        type="button"
        className="evaluation-category-toggle"
        aria-expanded={expanded}
        aria-controls={detailId}
        onClick={() => onSelect(expanded ? null : categoryKey)}
      >
        <span>
          <strong>{CATEGORY_LABELS[categoryKey]}</strong>
          <small>{stateLabel}</small>
        </span>
        <span className="evaluation-category-score">
          {pending
            ? `${category.available_points} puntos disponibles de ${category.max_score}`
            : partial
              ? `${category.score} / ${category.available_points} disponibles`
              : `${category.score} / ${category.max_score}`}
          <ChevronDown aria-hidden="true" />
        </span>
      </button>
      {expanded ? (
        <div id={detailId} className="evaluation-category-details">
          {pending ? (
            <p>
              {categoryKey === "implementation"
                ? "El proyecto todavía no fue creado."
                : "Las pruebas todavía no fueron ejecutadas."}
            </p>
          ) : null}
          {partial && categoryKey === "implementation" ? (
            <dl className="evaluation-evidence-status">
              <div><dt>Proyecto</dt><dd>Creado</dd></div>
              <div>
                <dt>Entorno</dt>
                <dd>{hasSignal("ENVIRONMENT_PREPARED") ? "Preparado" : "Pendiente"}</dd>
              </div>
              <div>
                <dt>Dependencias</dt>
                <dd>{hasSignal("DEPENDENCIES_INSTALLED") ? "Instaladas" : "Pendientes"}</dd>
              </div>
            </dl>
          ) : null}
          {category.penalties.length ? (
            <ul className="evaluation-adjustments">
              {category.penalties.map((penalty) => (
                <li key={penalty.code}>
                  <CircleAlert aria-hidden="true" />
                  <span>{penalty.message}</span>
                  <strong>{penalty.points}</strong>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">Sin penalizaciones.</p>
          )}
          {positiveSignals.length ? (
            <>
              <h4>Señales positivas</h4>
            <ul className="evaluation-adjustments is-positive">
              {positiveSignals.map((signal) => (
                <li key={signal.code}>
                  <Check aria-hidden="true" />
                  <span>{signal.message}</span>
                </li>
              ))}
            </ul>
            </>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

function CoveragePanel({
  title,
  coverage,
}: {
  title: string;
  coverage: RequirementCoverage;
}) {
  return (
    <section className="evaluation-section">
      <div className="evaluation-section-heading">
        <div>
          <span className="section-kicker">Cobertura</span>
          <h3>{title}</h3>
        </div>
        <strong>
          {coverage.satisfied}/{coverage.total} · {coverage.coverage_percent.toFixed(0)}%
        </strong>
      </div>
      {coverage.items.length ? (
        <ul className="coverage-list">
          {coverage.items.map((item) => (
            <li key={item.index}>
              <span className={`coverage-status is-${item.status}`}>
                {item.status === "satisfied"
                  ? "Satisfecho"
                  : item.status === "unsatisfied"
                    ? "No satisfecho"
                    : "Sin evidencia"}
              </span>
              <span>{item.text}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">No se declararon elementos para evaluar.</p>
      )}
    </section>
  );
}

function FindingActions({
  finding,
  onOpenTask,
  onOpenEvent,
  onOpenFile,
}: {
  finding: EvaluationFinding;
  onOpenTask: (id: string) => void;
  onOpenEvent: (id: string) => void;
  onOpenFile: (path: string) => void;
}) {
  return (
    <div className="evaluation-finding-actions">
      {finding.related_task_id ? (
        <button type="button" onClick={() => onOpenTask(finding.related_task_id!)}>
          <Target aria-hidden="true" /> Ver tarea
        </button>
      ) : null}
      {finding.related_event_id ? (
        <button type="button" onClick={() => onOpenEvent(finding.related_event_id!)}>
          <Route aria-hidden="true" /> Ver en Timeline
        </button>
      ) : null}
      {finding.related_file ? (
        <button type="button" onClick={() => onOpenFile(finding.related_file!)}>
          <FileCode2 aria-hidden="true" /> Ver archivo
        </button>
      ) : null}
    </div>
  );
}

export function WorkflowEvaluationTab({
  threadId,
  branchId,
  events,
  selectedCategory,
  selectedFinding,
  onSelectCategory,
  onSelectFinding,
  onOpenTask,
  onOpenEvent,
  onOpenFile,
}: {
  threadId: string;
  branchId: string;
  events: WorkflowEvent[];
  selectedCategory: string | null;
  selectedFinding: string | null;
  onSelectCategory: (category: string | null) => void;
  onSelectFinding: (finding: string | null) => void;
  onOpenTask: (id: string) => void;
  onOpenEvent: (id: string) => void;
  onOpenFile: (path: string) => void;
}) {
  const evaluation = useWorkflowEvaluationStore((state) => state.evaluation);
  const isLoading = useWorkflowEvaluationStore((state) => state.isLoading);
  const isRefreshing = useWorkflowEvaluationStore((state) => state.isRefreshing);
  const error = useWorkflowEvaluationStore((state) => state.error);
  const setError = useWorkflowEvaluationStore((state) => state.setError);
  const { load } = useWorkflowEvaluation(
    threadId,
    branchId,
    true,
    events,
  );

  if (isLoading && !evaluation) {
    return (
      <div className="loading-state" role="status">
        <LoaderCircle className="spin" aria-hidden="true" />
        Calculando evaluación…
      </div>
    );
  }
  if (error && !evaluation) {
    return (
      <div className="page-banner">
        <ErrorBanner
          error={error}
          onRetry={() => void load()}
          onDismiss={() => setError(null)}
        />
      </div>
    );
  }
  if (!evaluation) {
    return (
      <div className="empty-state">
        <Target aria-hidden="true" />
        <p>Todavía no hay datos suficientes para evaluar este workflow.</p>
      </div>
    );
  }

  const categories = Object.entries(CATEGORY_LABELS) as Array<
    [CategoryKey, string]
  >;
  const legacyStage = (
    elapsed: number | null,
  ) => ({ elapsed_seconds: elapsed, active_seconds: elapsed, waiting_seconds: null });
  const durations = [
    ["Planning", evaluation.duration.planning ?? legacyStage(evaluation.duration.planning_seconds)],
    ["Implementation", evaluation.duration.implementation ?? legacyStage(evaluation.duration.implementation_seconds)],
    ["Testing", evaluation.duration.testing ?? legacyStage(evaluation.duration.testing_seconds)],
    ["Repair", evaluation.duration.repair ?? legacyStage(evaluation.duration.repair_seconds)],
    ["Finalize", evaluation.duration.finalize ?? legacyStage(evaluation.duration.finalize_seconds)],
  ] as const;
  const provisional = evaluation.evaluation_status === "partial";
  const earnedPoints = evaluation.earned_points ?? evaluation.overall_score;
  const availablePoints = evaluation.available_points ?? evaluation.max_score;
  const provisionalPercentage = evaluation.provisional_percentage ?? evaluation.percentage;
  const finalGradeAvailable = evaluation.final_grade_available ?? !provisional;

  return (
    <section className="workflow-evaluation" aria-labelledby="evaluation-title">
      {error ? (
        <ErrorBanner
          error={error}
          onRetry={() => void load(true)}
          onDismiss={() => setError(null)}
        />
      ) : null}
      <header className="evaluation-overall">
        <div>
          <span className="section-kicker">Calidad del workflow</span>
          <h2 id="evaluation-title">
            {provisional ? "Evaluación provisional" : "Evaluación general"}
          </h2>
          <div className="evaluation-meta">
            {finalGradeAvailable && evaluation.grade ? (
              <span className={`evaluation-grade is-${evaluation.grade}`}>
                {GRADE_LABELS[evaluation.grade]}
              </span>
            ) : null}
            <span>
              {provisional ? "Testing pendiente" : "Final"}
            </span>
            <span>Scoring v{evaluation.scoring_version}</span>
            {isRefreshing ? (
              <span role="status"><RefreshCw className="spin" aria-hidden="true" /> Actualizando</span>
            ) : null}
          </div>
        </div>
        <div className="evaluation-score-block">
          <strong>{earnedPoints}</strong>
          <span>
            {provisional
              ? `de ${availablePoints} puntos disponibles`
              : `/ ${evaluation.max_score}`}
          </span>
          {provisional ? (
            <>
              <small>{provisionalPercentage.toFixed(0)}% provisional</small>
              <small>
                Faltan {evaluation.projected_max_score - availablePoints} puntos por evaluar.
              </small>
            </>
          ) : null}
        </div>
        <div
          className="evaluation-progress"
          role="progressbar"
          aria-label="Puntuación global"
          aria-valuemin={0}
          aria-valuemax={provisional ? availablePoints : evaluation.max_score}
          aria-valuenow={earnedPoints}
        >
          <span style={{ width: `${provisionalPercentage}%` }} />
        </div>
        <small>
          Actualizada{" "}
          <time dateTime={evaluation.source_updated_at ?? evaluation.calculated_at}>
            {new Intl.DateTimeFormat("es", {
              dateStyle: "short",
              timeStyle: "short",
            }).format(
              new Date(evaluation.source_updated_at ?? evaluation.calculated_at),
            )}
          </time>
        </small>
      </header>

      <div className="evaluation-category-grid">
        {categories.map(([key]) => (
          <CategoryCard
            key={key}
            categoryKey={key}
            category={evaluation[key]}
            expanded={selectedCategory === key}
            onSelect={onSelectCategory}
          />
        ))}
      </div>

      <div className="evaluation-coverage-grid">
        <CoveragePanel
          title="Requisitos funcionales"
          coverage={evaluation.requirement_coverage}
        />
        <CoveragePanel
          title="Criterios de aceptación"
          coverage={evaluation.acceptance_coverage}
        />
      </div>

      <section className="evaluation-section">
        <div className="evaluation-section-heading">
          <div>
            <span className="section-kicker">Evidencia</span>
            <h3>Hallazgos</h3>
          </div>
          <strong>{evaluation.findings.length}</strong>
        </div>
        {evaluation.findings.length ? (
          <div className="evaluation-findings">
            {evaluation.findings.map((finding) => {
              const expanded = selectedFinding === finding.code;
              return (
                <article
                  key={`${finding.category}-${finding.code}`}
                  className={`evaluation-finding is-${finding.severity}`}
                >
                  <button
                    type="button"
                    aria-expanded={expanded}
                    onClick={() => onSelectFinding(expanded ? null : finding.code)}
                  >
                    {finding.severity === "info"
                      ? <Check aria-hidden="true" />
                      : finding.severity === "warning"
                        ? <AlertTriangle aria-hidden="true" />
                        : <CircleAlert aria-hidden="true" />}
                    <span>
                      <strong>{finding.title}</strong>
                      <small>{SEVERITY_LABELS[finding.severity]}</small>
                    </span>
                    <ChevronDown aria-hidden="true" />
                  </button>
                  {expanded ? (
                    <div className="evaluation-finding-detail">
                      <p>{finding.description}</p>
                      {finding.recommendation ? (
                        <p><strong>Recomendación:</strong> {finding.recommendation}</p>
                      ) : null}
                      <FindingActions
                        finding={finding}
                        onOpenTask={onOpenTask}
                        onOpenEvent={onOpenEvent}
                        onOpenFile={onOpenFile}
                      />
                    </div>
                  ) : null}
                </article>
              );
            })}
          </div>
        ) : (
          <p className="muted">No hay hallazgos para este estado.</p>
        )}
      </section>

      <section className="evaluation-section">
        <div className="evaluation-section-heading">
          <div>
            <span className="section-kicker">Rendimiento</span>
            <h3>Duración</h3>
          </div>
          <Clock3 aria-hidden="true" />
        </div>
        <div className="evaluation-duration-summary">
          <div>
            <span>Tiempo total</span>
            <strong>
              {formatEvaluationDuration(
                evaluation.duration.wall_clock_duration_seconds
                  ?? evaluation.duration.total_duration_seconds,
              )}
            </strong>
          </div>
          <div>
            <span>Distribución</span>
            <strong>
              Ejecución activa:{" "}
              {formatEvaluationDuration(
                evaluation.duration.active_execution_seconds
                  ?? evaluation.duration.total_duration_seconds,
              )}
            </strong>
            <small>
              Esperando aprobaciones:{" "}
              {formatEvaluationDuration(evaluation.duration.approval_wait_seconds)}
            </small>
          </div>
        </div>
        <p className="muted">
          Los tiempos por etapa incluyen espera; no deben sumarse entre sí.
        </p>
        <dl className="evaluation-durations">
          {durations.map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>
                {formatEvaluationDuration(value.elapsed_seconds)}
                <small>
                  Activa {formatEvaluationDuration(value.active_seconds)} · Espera{" "}
                  {formatEvaluationDuration(value.waiting_seconds)}
                </small>
              </dd>
            </div>
          ))}
        </dl>
      </section>

      <section className="evaluation-section">
        <div className="evaluation-section-heading">
          <div>
            <span className="section-kicker">Siguientes pasos</span>
            <h3>Recomendaciones</h3>
          </div>
        </div>
        {evaluation.recommendations.length ? (
          <ul className="evaluation-recommendations">
            {evaluation.recommendations.map((recommendation) => (
              <li key={recommendation}>
                <ExternalLink aria-hidden="true" />
                {recommendation}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">No hay recomendaciones pendientes.</p>
        )}
      </section>
    </section>
  );
}
