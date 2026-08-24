import {
  ArrowRight,
  Braces,
  Check,
  ChevronDown,
  CircleAlert,
  Clock3,
  ExternalLink,
  FileCode2,
  Gauge,
  ListFilter,
  LoaderCircle,
  RefreshCw,
  Route,
} from "lucide-react";
import { useMemo, useState } from "react";

import type {
  WorkflowEvent,
  WorkflowExecutionTask,
  WorkflowTaskStatus,
} from "../../api/types";
import { useWorkflowExecution } from "../../hooks/useWorkflowExecution";
import { useWorkflowExecutionStore } from "../../stores/workflowExecutionStore";
import { selectPreferredTaskEvent } from "../../utils/taskEventSelection";
import { ErrorBanner } from "../ErrorBanner";

const STATUS_LABELS: Record<WorkflowTaskStatus, string> = {
  pending: "Pendiente",
  running: "En ejecución",
  waiting: "Esperando aprobación",
  completed: "Completada",
  failed: "Fallida",
  skipped: "Omitida",
};

function formatDuration(start: string | null, end: string | null): string | null {
  if (!start || !end) return null;
  const milliseconds = new Date(end).getTime() - new Date(start).getTime();
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return null;
  return milliseconds < 1_000
    ? `${milliseconds} ms`
    : `${(milliseconds / 1_000).toFixed(1)} s`;
}

function formatTimestamp(value: string | null): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? null
    : new Intl.DateTimeFormat("es", {
        dateStyle: "short",
        timeStyle: "medium",
      }).format(date);
}

function formatFramework(value: string | null): string {
  if (!value) return "n/a";
  return value.toLowerCase() === "fastapi" ? "FastAPI" : value;
}

const JUDGE_DIMENSION_LABELS: Record<string, string> = {
  requirement_alignment: "Requirement Alignment",
  completeness: "Completeness",
  technical_coherence: "Technical Coherence",
  task_clarity: "Task Clarity",
  complexity_control: "Complexity Control",
};

function pct(value: number | undefined | null): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `${Math.round(value)}%`
    : "n/a";
}

function confidencePct(value: number | undefined | null): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `${Math.round(value * 100)}%`
    : "n/a";
}

function scorePct(value: number | undefined | null): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `${Math.round(value)}%`
    : "n/a";
}

function RequirementList({
  title,
  items,
}: {
  title: string;
  items: string[];
}) {
  return (
    <section className="execution-list-section">
      <h3>{title}</h3>
      {items.length ? (
        <ul>{items.map((item) => <li key={item}><Check aria-hidden="true" />{item}</li>)}</ul>
      ) : (
        <p className="muted">Sin datos disponibles.</p>
      )}
    </section>
  );
}

function TaskStatus({ status }: { status: WorkflowTaskStatus }) {
  const Icon =
    status === "completed"
      ? Check
      : status === "failed"
        ? CircleAlert
        : status === "waiting"
          ? Clock3
          : status === "running"
            ? LoaderCircle
            : Clock3;
  return (
    <span className={`execution-task-status status-${status}`}>
      <Icon className={status === "running" ? "spin" : ""} aria-hidden="true" />
      {STATUS_LABELS[status]}
    </span>
  );
}

function AgentTask({
  task,
  expanded,
  onToggle,
  onOpenEvent,
  onOpenFile,
  technical,
  events,
}: {
  task: WorkflowExecutionTask;
  expanded: boolean;
  onToggle: () => void;
  onOpenEvent: (eventId: string) => void;
  onOpenFile: (path: string) => void;
  technical: boolean;
  events: WorkflowEvent[];
}) {
  const panelId = `task-panel-${task.task_id}`;
  const duration = formatDuration(task.started_at, task.completed_at);
  const startedAt = formatTimestamp(task.started_at);
  const completedAt = formatTimestamp(task.completed_at);
  const timelineEventId = selectPreferredTaskEvent(task, events);
  return (
    <li className="execution-task">
      <button
        type="button"
        className="execution-task-summary"
        aria-expanded={expanded}
        aria-controls={panelId}
        onClick={onToggle}
      >
        <span className="task-order">{task.order}</span>
        <span>
          <strong>{task.agent}</strong>
          <small>{task.title ?? task.description}</small>
        </span>
        <TaskStatus status={task.status} />
        <ChevronDown aria-hidden="true" />
      </button>
      {expanded ? (
        <div id={panelId} className="execution-task-detail">
          <p>{task.description}</p>
          <div className="execution-facts">
            <span>Intento: {task.attempt}</span>
            {duration ? <span>Duración: {duration}</span> : null}
            {startedAt ? <span>Inicio: <time dateTime={task.started_at!}>{startedAt}</time></span> : null}
            {completedAt ? <span>Fin: <time dateTime={task.completed_at!}>{completedAt}</time></span> : null}
            {technical ? <code>{task.task_id}</code> : null}
          </div>
          {task.result_summary ? <p><strong>Resultado:</strong> {task.result_summary}</p> : null}
          {task.related_files.length ? (
            <div className="execution-related">
              <strong>Archivos</strong>
              {task.related_files.map((path) => (
                <button type="button" key={path} onClick={() => onOpenFile(path)}>
                  <FileCode2 aria-hidden="true" /> {path}
                </button>
              ))}
            </div>
          ) : null}
          {timelineEventId ? (
            <button
              type="button"
              className="execution-link"
              onClick={() => onOpenEvent(timelineEventId)}
            >
              <ExternalLink aria-hidden="true" /> Ver en Timeline
            </button>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

export function WorkflowExecutionTab({
  threadId,
  branchId,
  events,
  selectedTask,
  onSelectTask,
  onOpenEvent,
  onOpenFile,
}: {
  threadId: string;
  branchId: string;
  events: WorkflowEvent[];
  selectedTask: string | null;
  onSelectTask: (taskId: string | null) => void;
  onOpenEvent: (eventId: string) => void;
  onOpenFile: (path: string) => void;
}) {
  const execution = useWorkflowExecutionStore((state) => state.execution);
  const loading = useWorkflowExecutionStore((state) => state.isLoading);
  const refreshing = useWorkflowExecutionStore((state) => state.isRefreshing);
  const error = useWorkflowExecutionStore((state) => state.error);
  const setError = useWorkflowExecutionStore((state) => state.setError);
  const { load } = useWorkflowExecution(threadId, branchId, true, events);
  const [technical, setTechnical] = useState(false);
  const tasks = useMemo(
    () => [...(execution?.planning.tasks ?? [])].sort((left, right) => left.order - right.order),
    [execution],
  );

  if (loading && !execution) {
    return <div className="execution-empty" role="status"><LoaderCircle className="spin" aria-hidden="true" /> Cargando ejecución...</div>;
  }
  if (error && !execution) {
    return <ErrorBanner error={error} onRetry={() => void load()} onDismiss={() => setError(null)} />;
  }
  if (!execution) {
    return <div className="execution-empty">El análisis todavía no está disponible.</div>;
  }

  const analysis = execution.planning.analysis;
  const judge = execution.planning.judge;
  const hybrid = execution.planning.hybrid_evaluation;
  const judgeResult = judge?.result;
  const judgeDimensions = judgeResult?.dimensions ?? {};
  return (
    <section className="workflow-execution" aria-labelledby="execution-title">
      <header className="execution-header">
        <div>
          <span className="section-kicker">Agentes</span>
          <h2 id="execution-title">Ejecución</h2>
          <p>{execution.lineage} · {execution.branch_id}</p>
        </div>
        <div className="view-switch" aria-label="Vista de ejecución">
          <button type="button" aria-pressed={!technical} onClick={() => setTechnical(false)}>
            <ListFilter aria-hidden="true" /> Resumen
          </button>
          <button type="button" aria-pressed={technical} onClick={() => setTechnical(true)}>
            <Braces aria-hidden="true" /> Detalles técnicos
          </button>
        </div>
      </header>
      {refreshing ? <p className="execution-refresh" aria-live="polite"><RefreshCw className="spin" aria-hidden="true" /> Actualizando ejecución...</p> : null}
      {error ? <ErrorBanner error={error} onRetry={() => void load(true)} onDismiss={() => setError(null)} /> : null}
      {execution.inherited_from ? <p className="execution-inherited">Heredado desde {execution.inherited_from}</p> : null}

      <section className="execution-band">
        <h3>Análisis</h3>
        {analysis.completed ? (
          <>
            <span className="execution-label">Objetivo</span>
            <p className="execution-objective">{analysis.objective ?? analysis.requirement}</p>
            {technical ? <p><strong>Fuente:</strong> {analysis.source ?? "n/a"}</p> : null}
          </>
        ) : (
          <p>El análisis todavía no está disponible.</p>
        )}
      </section>

      <div className="execution-requirements">
        <RequirementList title="Requisitos funcionales" items={analysis.functional_requirements} />
        <RequirementList title="Requisitos no funcionales" items={analysis.non_functional_requirements} />
        <RequirementList title="Criterios de aceptación" items={analysis.acceptance_criteria} />
      </div>

      <section className="execution-band" id="execution-tasks">
        <div className="execution-section-heading">
          <div><h3>Plan y tareas</h3><p>{execution.planning.valid ? "Plan validado" : "El plan está siendo generado."}</p></div>
          <span>{execution.planning.attempts} intentos</span>
        </div>
        {tasks.length ? (
          <ol className="execution-task-list">
            {tasks.map((task) => (
              <AgentTask
                key={task.task_id}
                task={task}
                expanded={selectedTask === task.task_id}
                onToggle={() => onSelectTask(selectedTask === task.task_id ? null : task.task_id)}
                onOpenEvent={onOpenEvent}
                onOpenFile={onOpenFile}
                technical={technical}
                events={events}
              />
            ))}
          </ol>
        ) : <p>El plan está siendo generado.</p>}
        {judge?.status ? (
          <section className="execution-judge" aria-label="Semantic Judge">
            <div className="execution-section-heading">
              <div>
                <h3><Gauge aria-hidden="true" /> Semantic Judge</h3>
                <p>{judge.status}{judgeResult?.recommendation ? ` · ${judgeResult.recommendation}` : ""}</p>
              </div>
              <span>{pct(judgeResult?.overall_score)}</span>
            </div>
            <div className="execution-facts">
              <span>Confidence: {confidencePct(judgeResult?.confidence)}</span>
              <span>Agreement: {String(judge.disagreement?.type ?? "n/a")}</span>
              {technical && judge.model ? <code>{judge.model}</code> : null}
            </div>
            <div className="execution-related">
              <strong>Dimensions</strong>
              {Object.entries(JUDGE_DIMENSION_LABELS).map(([key, label]) => (
                <span key={key}>{label}: {pct(judgeDimensions[key])}</span>
              ))}
            </div>
            {judgeResult?.issues?.length ? (
              <div className="execution-related">
                <strong>Issues</strong>
                {judgeResult.issues.map((issue) => <span key={issue}>{issue}</span>)}
              </div>
            ) : null}
            {hybrid?.status ? (
              <div className="execution-related" aria-label="Hybrid Evaluation">
                <strong>Hybrid Evaluation</strong>
                <span>Hybrid Score: {pct(hybrid.score)}</span>
                <span>Hybrid Confidence: {confidencePct(hybrid.confidence)}</span>
                <span>Agreement: {hybrid.agreement ?? "n/a"}</span>
                <span>Source: {hybrid.source ?? "n/a"}</span>
                <span>Recommendation: {hybrid.recommendation ?? "n/a"} (advisory)</span>
                {hybrid.flags.length ? <span>Flags: {hybrid.flags.join(", ")}</span> : <span>Flags: none</span>}
                {technical ? <code>{hybrid.version ?? "n/a"}</code> : null}
              </div>
            ) : null}
          </section>
        ) : null}
      </section>

      <div className="execution-results">
        <section className="execution-band">
          <h3>Implementación</h3>
          <div className="execution-facts">
            <span>{execution.implementation.valid ? "Validada" : "Pendiente"}</span>
            <span>Framework: {formatFramework(execution.implementation.framework)}</span>
            <span>{execution.implementation.generated_files.length} archivos</span>
            <span>Entorno: {execution.implementation.environment_prepared ? "preparado" : "pendiente"}</span>
          </div>
          {execution.implementation.installed_dependencies.length ? (
            <div className="execution-related">
              <strong>Dependencias instaladas</strong>
              {execution.implementation.installed_dependencies.map((dependency) => (
                <code key={dependency}>{dependency}</code>
              ))}
            </div>
          ) : null}
          {execution.implementation.validation_errors.map((item) => <p className="execution-error" key={item}>{item}</p>)}
          {execution.implementation.refinements.map((refinement) => (
            <details key={`${refinement.sequence}-${refinement.attempt}`}>
              <summary>Refinamiento, intento {refinement.attempt}</summary>
              <p>{refinement.reason ?? "Sin motivo registrado."}</p>
              <p>{refinement.changed_fields.join(", ") || "Sin campos registrados."}</p>
            </details>
          ))}
        </section>

        <section className="execution-band">
          <h3>Testing y reparación</h3>
          {execution.testing.executed ? (
            <>
              <p><strong>{execution.testing.framework ?? "Tests"}</strong> · {execution.testing.summary ?? (execution.testing.passed ? "Pruebas aprobadas" : "Pruebas fallidas")} · {execution.testing.warnings} warnings</p>
              {technical && execution.testing.actual_command.length ? <pre className="execution-command">{execution.testing.actual_command.join(" ")}</pre> : null}
            </>
          ) : <p>Las pruebas todavía no fueron ejecutadas.</p>}
          {execution.testing.repair_phase === "not_started" ? (
            <p>No fue necesaria una reparación.</p>
          ) : (
            <div className="repair-comparison">
              <p>{execution.testing.repair_decision}</p>
              <div><strong>Antes</strong><pre>{JSON.stringify(execution.testing.repair_before, null, 2)}</pre></div>
              <ArrowRight aria-hidden="true" />
              <div><strong>Después</strong><pre>{JSON.stringify(execution.testing.repair_after, null, 2)}</pre></div>
            </div>
          )}
        </section>
      </div>

      <section className="execution-band" aria-label="Agent Performance">
        <div className="execution-section-heading">
          <div><h3>Agent Performance</h3><p>Diagnostico determinista, no bloqueante.</p></div>
        </div>
        <div className="table-scroll">
          <table>
            <thead><tr><th>Agent</th><th>Score</th><th>Level</th><th>Confidence</th><th>Status</th></tr></thead>
            <tbody>
              {(["planner", "developer", "repair", "qa"] as const).map((agent) => {
                const item = execution.agent_performance?.[agent];
                return (
                  <tr key={agent}>
                    <td>{agent}</td>
                    <td>{scorePct(item?.score)}</td>
                    <td>{item?.level ?? "n/a"}</td>
                    <td>{confidencePct(item?.confidence)}</td>
                    <td>{item?.status ?? "insufficient_data"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {Object.entries(execution.agent_performance ?? {}).map(([agent, item]) => (
          <details key={agent}>
            <summary>{agent} details</summary>
            <div className="execution-related">
              <strong>Metrics</strong>
              {Object.entries(item.metrics ?? {}).map(([key, value]) => (
                <span key={key}>{key}: {String(value)}</span>
              ))}
            </div>
            {item.issues.length ? (
              <div className="execution-related">
                <strong>Issues</strong>
                {item.issues.map((issue) => <span key={issue}>{issue}</span>)}
              </div>
            ) : null}
            {item.reason_codes.length ? (
              <div className="execution-related">
                <strong>Reason codes</strong>
                {item.reason_codes.map((code) => <code key={code}>{code}</code>)}
              </div>
            ) : null}
          </details>
        ))}
      </section>

      <section className="execution-band" aria-label="Failure Attribution">
        <div className="execution-section-heading">
          <div><h3>Failure Attribution</h3><p>{execution.failure_attribution?.status === "not_applicable" ? "No failure attribution required." : "Root cause analysis determinista."}</p></div>
          <span>{execution.failure_attribution?.recovered ? "recovered" : execution.failure_attribution?.status ?? "n/a"}</span>
        </div>
        {execution.failure_attribution?.status === "not_applicable" ? (
          <p>No failure attribution required.</p>
        ) : (
          <>
            <div className="execution-facts">
              <span>Root Cause: {execution.failure_attribution?.root_cause ?? "n/a"}</span>
              <span>Failure Class: {execution.failure_attribution?.failure_class ?? "n/a"}</span>
              <span>Confidence: {confidencePct(execution.failure_attribution?.confidence)}</span>
            </div>
            {execution.failure_attribution?.contributors?.length ? (
              <div className="execution-related">
                <strong>Contributors</strong>
                {execution.failure_attribution.contributors.map((item, index) => (
                  <span key={`${String(item.source)}-${index}`}>
                    {String(item.source ?? "n/a")}: {String(item.contribution ?? "n/a")} ({confidencePct(typeof item.confidence === "number" ? item.confidence : null)})
                  </span>
                ))}
              </div>
            ) : null}
            {execution.failure_attribution?.excluded_attributions?.length ? (
              <div className="execution-related">
                <strong>Excluded</strong>
                {execution.failure_attribution.excluded_attributions.map((item, index) => (
                  <span key={`${String(item.source)}-${index}`}>{String(item.source ?? "n/a")}: {String(item.reason ?? "n/a")}</span>
                ))}
              </div>
            ) : null}
            <details>
              <summary>RCA evidence</summary>
              <div className="execution-related">
                <strong>Evidence</strong>
                {Object.entries(execution.failure_attribution?.evidence ?? {}).map(([key, value]) => (
                  <span key={key}>{key}: {String(value)}</span>
                ))}
              </div>
              {execution.failure_attribution?.reason_codes?.length ? (
                <div className="execution-related">
                  <strong>Reason codes</strong>
                  {execution.failure_attribution.reason_codes.map((code) => <code key={code}>{code}</code>)}
                </div>
              ) : null}
            </details>
          </>
        )}
      </section>

      {execution.git && execution.git.state !== "not_initialized" ? <section className="execution-band" aria-label="Git lifecycle">
        <h3><Route aria-hidden="true" /> Git lifecycle</h3>
        <div className="execution-facts">
          <span>Base: {execution.git.base_branch ?? "n/a"}</span>
          <span>Workflow branch: {execution.git.workflow_branch ?? "n/a"}</span>
          <span>Approval: {execution.git.approval_state ?? execution.git.commit_status ?? "n/a"}</span>
        </div>
        {(["Developer", "Repair"] as const).map((label) => {
          const commit = label === "Developer" ? execution.git?.developer_commit : execution.git?.repair_commit;
          return <div className="execution-related" key={label}>
            <strong>{label} commit</strong>
            {commit ? <><code title={commit.sha ?? commit.commit}>{(commit.sha ?? commit.commit)?.slice(0, 12) ?? "n/a"}</code><span>{commit.message ?? "n/a"}</span><span>{commit.files?.length ?? 0} files</span></> : <span>n/a</span>}
          </div>;
        })}
      </section> : null}

      <section className="execution-band">
        <h3><Route aria-hidden="true" /> Supervisor</h3>
        <p>{execution.supervisor.decision ?? "Sin decisión registrada"}{execution.supervisor.decision_source ? ` · ${execution.supervisor.decision_source}` : ""}</p>
        {execution.supervisor.confidence !== null ? <p>Confianza: {Math.round(execution.supervisor.confidence * 100)}%</p> : null}
        {execution.supervisor.loop_detected ? <p className="execution-error">Loop detectado</p> : null}
        {execution.supervisor.handoff_history.length ? (
          <ol className="handoff-list">
            {execution.supervisor.handoff_history.map((handoff, index) => (
              <li key={`${String(handoff.sequence ?? index)}-${index}`}>
                <span>{String(handoff.from ?? "supervisor")}</span>
                <ArrowRight aria-hidden="true" />
                <strong>{String(handoff.executed_to ?? handoff.selected_to ?? handoff.to ?? "n/a")}</strong>
                {technical ? <small>{String(handoff.reason ?? handoff.source ?? "")}</small> : null}
              </li>
            ))}
          </ol>
        ) : null}
      </section>

      <section className="execution-band execution-learnings">
        <div className="execution-section-heading">
          <div><h3>Learnings</h3><p>{execution.workflow_learning.state} · {execution.workflow_learning.extracted_count} extraídos</p></div>
        </div>
        {execution.workflow_learning.candidates.length ? (
          <div className="table-scroll"><table><thead><tr><th>Tipo</th><th>Estado</th><th>Confianza</th><th>Knowledge ID</th><th>Fuente</th><th>Creado</th></tr></thead><tbody>
            {execution.workflow_learning.candidates.map((item) => <tr key={item.candidate_id}>
              <td>{item.knowledge_type}</td><td><span className="knowledge-status" data-status={item.submission_status}>{item.submission_status.toUpperCase()}</span></td>
              <td>{Math.round(item.confidence * 100)}%</td>
              <td>{item.knowledge_id ? <a href={`/knowledge/${encodeURIComponent(item.knowledge_id)}`} title={item.knowledge_id}>{item.knowledge_id.slice(0, 10)}...</a> : "n/a"}</td>
              <td title={item.source_reference}>{item.source_reference}</td>
              <td>{formatTimestamp(item.created_at) ?? "n/a"}</td>
            </tr>)}
          </tbody></table></div>
        ) : <p>No se extrajeron aprendizajes confiables.</p>}
      </section>

      <section className="execution-band execution-final">
        <h3>Resultado final</h3>
        <strong>{execution.final_result.terminal_status}</strong>
        {execution.final_result.summary ? <p>{execution.final_result.summary}</p> : null}
      </section>
    </section>
  );
}
