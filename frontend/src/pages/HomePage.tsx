import { Braces, Radio, ShieldCheck } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { ApiClientError } from "../api/client";
import { createWorkflow } from "../api/workflows";
import { ErrorBanner } from "../components/ErrorBanner";
import { WorkflowForm } from "../components/WorkflowForm";
import { useWorkflowStore } from "../stores/workflowStore";
import { AppNavigation } from "../components/AppNavigation";

export function HomePage() {
  const navigate = useNavigate();
  const error = useWorkflowStore((state) => state.error);
  const setError = useWorkflowStore((state) => state.setError);

  const create = async (request: string) => {
    setError(null);
    try {
      const workflow = await createWorkflow(request);
      navigate(`/workflows/${workflow.thread_id}`);
    } catch (caught) {
      setError(
        caught instanceof ApiClientError
          ? caught.toUiError("No se pudo iniciar el workflow")
          : {
              title: "No se pudo iniciar el workflow",
              message: "Ocurrió un error inesperado.",
              retryable: true,
            },
      );
    }
  };

  return (
    <main className="home-page">
      <AppNavigation />
      <section className="creation-workspace">
        <div className="creation-heading">
          <span className="section-kicker">Nuevo workflow</span>
          <h1>Convierte un requerimiento en software validado</h1>
          <p>
            Describe el proyecto. La fábrica planificará, implementará y
            ejecutará pruebas con aprobaciones explícitas.
          </p>
        </div>
        {error ? (
          <ErrorBanner error={error} onDismiss={() => setError(null)} />
        ) : null}
        <WorkflowForm onSubmit={create} />
        <div className="capability-strip" aria-label="Capacidades del workflow">
          <span><Braces aria-hidden="true" />Plan estructurado</span>
          <span><ShieldCheck aria-hidden="true" />Aprobaciones durables</span>
          <span><Radio aria-hidden="true" />Eventos en tiempo real</span>
        </div>
      </section>
    </main>
  );
}
