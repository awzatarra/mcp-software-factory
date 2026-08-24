import { Activity, BarChart3, Bell, BookOpen, BrainCircuit, CircleDollarSign, Factory, ListChecks, Plus } from "lucide-react";
import { Link, NavLink } from "react-router-dom";
import { useAlertSummary } from "../hooks/useAlertSummary";
import { useNotificationSummary } from "../hooks/useNotificationSummary";

export function AppNavigation() {
  const { summary } = useAlertSummary();
  const { summary: notifications } = useNotificationSummary();
  const activeCount = (summary?.critical_open ?? 0) + (summary?.error_open ?? 0);
  return (
    <header className="app-navigation">
      <Link className="nav-brand" to="/">
        <Factory aria-hidden="true" />
        <span>Software Factory</span>
      </Link>
      <nav aria-label="Navegación principal">
        <NavLink to="/dashboard">
          <BarChart3 aria-hidden="true" />
          Dashboard
        </NavLink>
        <NavLink to="/workflows">
          <ListChecks aria-hidden="true" />
          Workflows
        </NavLink>
        <NavLink to="/observability">
          <Activity aria-hidden="true" />
          Observabilidad
        </NavLink>
        <NavLink to="/llm-costs">
          <CircleDollarSign aria-hidden="true" />
          Costos LLM
        </NavLink>
        <NavLink to="/evaluations">
          <BrainCircuit aria-hidden="true" />
          Evaluaciones
        </NavLink>
        <NavLink to="/knowledge">
          <BookOpen aria-hidden="true" />
          Knowledge
        </NavLink>
        <NavLink to="/alerts">
          <Bell aria-hidden="true" />
          Alertas{activeCount > 0 ? <span className="nav-alert-badge" aria-label={`${activeCount} alertas críticas o de error`}>{activeCount > 99 ? "99+" : activeCount}</span> : null}{(notifications?.unread_internal ?? 0) > 0 ? <span className="nav-inbox-badge" aria-label={`${notifications?.unread_internal} notificaciones sin leer`}>{notifications!.unread_internal > 99 ? "99+" : notifications!.unread_internal}</span> : null}
        </NavLink>
        <NavLink to="/">
          <Plus aria-hidden="true" />
          Nuevo workflow
        </NavLink>
      </nav>
    </header>
  );
}
