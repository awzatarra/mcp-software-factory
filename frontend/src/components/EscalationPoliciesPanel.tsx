import { Plus, Trash2, X } from "lucide-react";
import { type FormEvent, useState } from "react";
import {
  createEscalation, deleteEscalation, type EscalationPolicy,
  type NotificationChannel,
} from "../api/notifications";

type DraftStep = {
  delaySeconds: number;
  channelIds: string[];
  severities: string[];
  requireUnacknowledged: boolean;
  repeat: boolean;
  repeatIntervalSeconds: number;
  maxRepeats: number;
};

const severities = ["warning", "error", "critical"];
const newStep = (): DraftStep => ({
  delaySeconds: 0,
  channelIds: [],
  severities: [...severities],
  requireUnacknowledged: true,
  repeat: false,
  repeatIntervalSeconds: 30,
  maxRepeats: 1,
});

export function EscalationPoliciesPanel({
  channels,
  policies,
  onRefresh,
}: {
  channels: NotificationChannel[];
  policies: EscalationPolicy[];
  onRefresh: () => Promise<void>;
}) {
  const [showForm, setShowForm] = useState(false);
  const [steps, setSteps] = useState<DraftStep[]>([newStep()]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const updateStep = (index: number, update: Partial<DraftStep>) => {
    setSteps((current) => current.map((step, selected) => selected === index ? { ...step, ...update } : step));
  };

  const toggleValue = (values: string[], value: string) =>
    values.includes(value) ? values.filter((item) => item !== value) : [...values, value];

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (saving) return;
    const form = new FormData(event.currentTarget);
    setSaving(true);
    setError(null);
    try {
      await createEscalation({
        name: String(form.get("name")),
        description: String(form.get("description") || "") || null,
        enabled: true,
        steps: steps.map((step, index) => ({
          step: index + 1,
          delay_seconds: step.delaySeconds,
          channel_ids: step.channelIds,
          severities: step.severities,
          require_unacknowledged: step.requireUnacknowledged,
          repeat: step.repeat,
          repeat_interval_seconds: step.repeat ? step.repeatIntervalSeconds : null,
          max_repeats: step.repeat ? step.maxRepeats : null,
        })),
        stop_on_acknowledge: form.has("stop_on_acknowledge"),
        stop_on_resolve: form.has("stop_on_resolve"),
      });
      setShowForm(false);
      setSteps([newStep()]);
      await onRefresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "No se pudo crear la política de escalamiento.");
    } finally {
      setSaving(false);
    }
  };

  return <section className="escalation-policy-section" aria-labelledby="escalation-heading">
    <header className="notification-section-heading">
      <div><h2 id="escalation-heading">Políticas de escalamiento</h2><p>Rutas progresivas para alertas sin resolver.</p></div>
      <button onClick={() => { setShowForm((value) => !value); setError(null); }}><Plus aria-hidden="true" />Crear escalamiento</button>
    </header>
    {error && <div className="alerts-state" role="alert">{error}</div>}
    {showForm && <form className="notification-form escalation-form" aria-label="Crear política de escalamiento" onSubmit={(event) => void submit(event)}>
      <label>Nombre<input name="name" required maxLength={120} /></label>
      <label>Descripción<input name="description" maxLength={500} /></label>
      <div className="escalation-steps">
        {steps.map((step, index) => <fieldset key={index}>
          <legend>Paso {index + 1}</legend>
          {steps.length > 1 && <button type="button" className="icon-button" aria-label={`Eliminar paso ${index + 1}`} onClick={() => setSteps((current) => current.filter((_, selected) => selected !== index))}><X aria-hidden="true" /></button>}
          <label>Espera en segundos<input aria-label={`Espera paso ${index + 1}`} type="number" min="0" max="2592000" value={step.delaySeconds} onChange={(event) => updateStep(index, { delaySeconds: Number(event.target.value) })} /></label>
          <div><span>Canales</span>{channels.map((channel) => <label key={channel.channel_id}><input type="checkbox" checked={step.channelIds.includes(channel.channel_id)} onChange={() => updateStep(index, { channelIds: toggleValue(step.channelIds, channel.channel_id) })} />{channel.name}</label>)}</div>
          <div><span>Severidades</span>{severities.map((severity) => <label key={severity}><input type="checkbox" checked={step.severities.includes(severity)} onChange={() => updateStep(index, { severities: toggleValue(step.severities, severity) })} />{severity}</label>)}</div>
          <label><input type="checkbox" checked={step.requireUnacknowledged} onChange={(event) => updateStep(index, { requireUnacknowledged: event.target.checked })} />Requerir alerta sin acknowledge</label>
          <label><input type="checkbox" checked={step.repeat} onChange={(event) => updateStep(index, { repeat: event.target.checked })} />Repetir paso</label>
          {step.repeat && <><label>Intervalo<input aria-label={`Intervalo paso ${index + 1}`} type="number" min="30" value={step.repeatIntervalSeconds} onChange={(event) => updateStep(index, { repeatIntervalSeconds: Number(event.target.value) })} /></label><label>Repeticiones<input aria-label={`Repeticiones paso ${index + 1}`} type="number" min="1" max="20" value={step.maxRepeats} onChange={(event) => updateStep(index, { maxRepeats: Number(event.target.value) })} /></label></>}
        </fieldset>)}
      </div>
      <button type="button" onClick={() => setSteps((current) => [...current, newStep()])}><Plus aria-hidden="true" />Agregar paso</button>
      <label><input defaultChecked type="checkbox" name="stop_on_acknowledge" />Detener al hacer acknowledge</label>
      <label><input defaultChecked type="checkbox" name="stop_on_resolve" />Detener al resolver</label>
      <div><button type="submit" disabled={saving}>{saving ? "Guardando..." : "Guardar"}</button><button type="button" disabled={saving} onClick={() => setShowForm(false)}>Cancelar</button></div>
    </form>}
    <div className="notification-list" aria-label="Políticas de escalamiento existentes">
      {policies.map((policy) => <article className="notification-card" key={policy.escalation_policy_id}>
        <header><div><strong>{policy.name}</strong><span>{policy.enabled ? "Activa" : "Pausada"}</span></div></header>
        {policy.description && <p>{policy.description}</p>}
        <ol>{policy.steps.map((step) => <li key={step.step}>Paso {step.step}: {step.delay_seconds}s · {step.channel_ids.map((id) => channels.find((item) => item.channel_id === id)?.name || id).join(", ")} · {step.require_unacknowledged ? "sin acknowledge" : "siempre"}</li>)}</ol>
        <small>{policy.stop_on_acknowledge ? "Detiene con acknowledge" : "Continúa con acknowledge"} · {policy.stop_on_resolve ? "detiene al resolver" : "continúa al resolver"}</small>
        <footer><button aria-label={`Eliminar ${policy.name}`} onClick={async () => { if (!window.confirm(`¿Eliminar ${policy.name}?`)) return; await deleteEscalation(policy.escalation_policy_id); await onRefresh(); }}><Trash2 aria-hidden="true" /></button></footer>
      </article>)}
    </div>
  </section>;
}
