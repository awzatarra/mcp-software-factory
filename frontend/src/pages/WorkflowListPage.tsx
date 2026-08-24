import {
  ArrowLeft,
  ArrowRight,
  Check,
  Copy,
  FolderOpen,
  RefreshCw,
  Search,
} from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import type { WorkflowListItem } from "../api/types";
import { AppNavigation } from "../components/AppNavigation";
import { ErrorBanner } from "../components/ErrorBanner";
import {
  WorkflowStatusBadge,
} from "../components/WorkflowStatusBadge";
import { useWorkflowList } from "../hooks/useWorkflowList";
import { useWorkflowListStore } from "../stores/workflowListStore";
import { workflowDisplayStatus } from "../utils/workflowListStatus";

function relativeDate(value: string | null, now: number): string {
  if (!value) return "Sin actualizar";
  const difference = Math.max(0, now - new Date(value).getTime());
  const minutes = Math.floor(difference / 60_000);
  if (minutes < 1) return "ahora";
  if (minutes < 60) return `hace ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `hace ${hours} h`;
  if (hours < 48) return "ayer";
  return `hace ${Math.floor(hours / 24)} días`;
}

function actionLabel(item: WorkflowListItem): string {
  const status = workflowDisplayStatus(item);
  if (status === "waiting") return "Continuar";
  if (status === "failed") return "Revisar";
  return "Abrir";
}

function WorkflowRow({ item, now }: { item: WorkflowListItem; now: number }) {
  const [copied, setCopied] = useState(false);
  const status = workflowDisplayStatus(item);
  const copy = async () => {
    await navigator.clipboard.writeText(item.thread_id);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1_500);
  };
  const updated = item.updated_at ?? item.created_at;
  return (
    <li className="workflow-list-item">
      <div className="workflow-item-main">
        <div className="workflow-item-title">
          <h2>{item.project_name ?? "Proyecto por definir"}</h2>
          <WorkflowStatusBadge status={status} />
        </div>
        <p className="workflow-intent">{item.workflow_intent ?? "Sin intent registrado"}</p>
        <div className="workflow-item-meta">
          <span title={new Date(updated).toLocaleString()}>
            Actualizado: {relativeDate(updated, now)}
          </span>
          <span title={new Date(item.created_at).toLocaleString()}>
            Creado: {relativeDate(item.created_at, now)}
          </span>
        </div>
      </div>
      <div className="workflow-item-facts">
        {item.test_summary ? <span>Tests: {item.test_summary}</span> : null}
        {item.pending_operation ? <span>Operación: {item.pending_operation}</span> : null}
        <span>
          Intentos: plan {item.planning_attempts} · implementación {item.implementation_attempts}
          {item.repair_attempts ? ` · reparación ${item.repair_attempts}` : ""}
        </span>
        <span className="thread-reference">
          Thread: <code>{item.thread_id.slice(0, 8)}…</code>
          <button type="button" className="icon-button" onClick={() => void copy()} title="Copiar thread ID">
            {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
            <span className="sr-only">{copied ? "Thread copiado" : "Copiar thread ID"}</span>
          </button>
        </span>
      </div>
      <Link className="open-workflow" to={`/workflows/${item.thread_id}`}>
        <FolderOpen aria-hidden="true" />
        {actionLabel(item)}
      </Link>
    </li>
  );
}

export function WorkflowListPage() {
  const items = useWorkflowListStore((state) => state.items);
  const total = useWorkflowListStore((state) => state.total);
  const hasMore = useWorkflowListStore((state) => state.hasMore);
  const loading = useWorkflowListStore((state) => state.isLoading);
  const refreshing = useWorkflowListStore((state) => state.isRefreshing);
  const error = useWorkflowListStore((state) => state.error);
  const setError = useWorkflowListStore((state) => state.setError);
  const controls = useWorkflowList();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const handle = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(handle);
  }, []);
  const filtered = Boolean(controls.searchInput.trim() || controls.status);

  return (
    <main className="workflow-list-page">
      <AppNavigation />
      <section className="workflow-list-shell">
        <div className="list-heading">
          <div>
            <span className="section-kicker">Historial durable</span>
            <h1>Workflows</h1>
          </div>
          {refreshing ? (
            <span className="refresh-indicator" role="status">
              <RefreshCw className="spin" aria-hidden="true" /> Actualizando…
            </span>
          ) : null}
        </div>
        <div className="workflow-filters">
          <label>
            Buscar
            <span className="search-control">
              <Search aria-hidden="true" />
              <input
                type="search"
                value={controls.searchInput}
                onChange={(event) => controls.setSearchInput(event.target.value)}
                placeholder="Proyecto, thread o intent"
              />
            </span>
          </label>
          <label>
            Estado
            <select value={controls.status} onChange={(event) => controls.setStatus(event.target.value)}>
              <option value="">Todos</option>
              <option value="waiting">Esperando aprobación</option>
              <option value="running">En ejecución</option>
              <option value="pending">Pendiente</option>
              <option value="completed">Completado</option>
              <option value="failed">Fallido</option>
            </select>
          </label>
          <label>
            Orden
            <select
              value={`${controls.sortBy}:${controls.sortOrder}`}
              onChange={(event) => controls.setSort(event.target.value)}
            >
              <option value="updated_at:desc">Más recientes</option>
              <option value="created_at:asc">Más antiguos</option>
              <option value="project_name:asc">Proyecto A-Z</option>
              <option value="project_name:desc">Proyecto Z-A</option>
            </select>
          </label>
        </div>
        <div aria-live="polite" className="sr-only">
          {loading ? "Cargando workflows" : `${total} workflows encontrados`}
        </div>
        {error ? (
          <ErrorBanner
            error={error}
            onRetry={error.retryable ? controls.refetch : undefined}
            onDismiss={() => setError(null)}
          />
        ) : null}
        {loading ? (
          <div className="list-loading" role="status">Cargando workflows…</div>
        ) : items.length ? (
          <ul className="workflow-list" aria-label="Workflows recientes">
            {items.map((item) => <WorkflowRow key={item.thread_id} item={item} now={now} />)}
          </ul>
        ) : (
          <div className="workflow-empty">
            <FolderOpen aria-hidden="true" />
            <h2>{filtered ? "No encontramos workflows con esos filtros." : "Todavía no hay workflows."}</h2>
            <p>{filtered ? "Prueba con otra búsqueda o estado." : "Crea el primero para comenzar."}</p>
            {filtered ? (
              <button type="button" className="secondary-button" onClick={controls.clearFilters}>Limpiar filtros</button>
            ) : (
              <Link className="primary-link" to="/">Nuevo workflow</Link>
            )}
          </div>
        )}
        {items.length ? (
          <nav className="pagination" aria-label="Paginación">
            <button type="button" onClick={controls.previousPage} disabled={controls.offset === 0}>
              <ArrowLeft aria-hidden="true" /> Anterior
            </button>
            <span>{controls.offset + 1}–{Math.min(controls.offset + items.length, total)} de {total}</span>
            <button type="button" onClick={controls.nextPage} disabled={!hasMore}>
              Siguiente <ArrowRight aria-hidden="true" />
            </button>
          </nav>
        ) : null}
      </section>
    </main>
  );
}
