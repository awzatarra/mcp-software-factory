import { ArrowRight, LoaderCircle } from "lucide-react";
import { type FormEvent, useState } from "react";

const DEFAULT_REQUIREMENT =
  'Crea un proyecto FastAPI llamado demo-health-api con GET /health que devuelva {"status": "ok"}, agrega pruebas y valida que pasen.';
const MAX_LENGTH = 20_000;

interface WorkflowFormProps {
  onSubmit: (request: string) => Promise<void>;
}

export function WorkflowForm({ onSubmit }: WorkflowFormProps) {
  const [request, setRequest] = useState(DEFAULT_REQUIREMENT);
  const [submitting, setSubmitting] = useState(false);
  const [validation, setValidation] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const value = request.trim();
    if (!value) {
      setValidation("Ingresa un requerimiento antes de iniciar.");
      return;
    }
    if (value.length > MAX_LENGTH || submitting) return;
    setValidation(null);
    setSubmitting(true);
    try {
      await onSubmit(value);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form className="workflow-form" onSubmit={submit} noValidate>
      <label htmlFor="workflow-requirement">Requerimiento</label>
      <textarea
        id="workflow-requirement"
        value={request}
        onChange={(event) => setRequest(event.target.value)}
        maxLength={MAX_LENGTH}
        rows={8}
        aria-describedby="requirement-meta"
        aria-invalid={Boolean(validation)}
      />
      <div id="requirement-meta" className="form-meta">
        <span className={validation ? "validation-message" : ""}>
          {validation ?? "Describe el proyecto, sus endpoints y cómo validarlo."}
        </span>
        <span>{request.length.toLocaleString()} / {MAX_LENGTH.toLocaleString()}</span>
      </div>
      <button
        className="primary-button submit-button"
        type="submit"
        disabled={submitting || !request.trim()}
      >
        {submitting ? (
          <LoaderCircle className="spin" aria-hidden="true" />
        ) : (
          <ArrowRight aria-hidden="true" />
        )}
        {submitting ? "Iniciando…" : "Crear workflow"}
      </button>
    </form>
  );
}
