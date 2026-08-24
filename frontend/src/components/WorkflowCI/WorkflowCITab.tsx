import { CheckCircle2, LoaderCircle, Play, RefreshCw, ShieldCheck, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { getWorkflowCI, getWorkflowCIAudit, getWorkflowCIRuns, prepareWorkflowCI, runWorkflowCI } from "../../api/workflows";
import type { CIAuditTrail, CIPipelinePreview, CIPipelineRun, CIWorkflowStatus } from "../../api/types";

const statusClass = (status: string | null | undefined) =>
  status === "passed" || status === "accepted" ? "success" : status === "warning" || status === "accepted_with_warnings" || status === "running" || status === "pending" ? "warning" : status ? "danger" : "";
const duration = (seconds: number | null) => seconds == null ? "n/a" : `${seconds.toFixed(seconds < 10 ? 2 : 1)}s`;
const short = (value: string | null) => value ? value.slice(0, 12) : "n/a";

interface CIData {
  status: CIWorkflowStatus;
  preview: CIPipelinePreview | null;
  runs: CIPipelineRun[];
  audit: CIAuditTrail | null;
}

export function WorkflowCITab({ threadId }: { threadId: string }) {
  const [data, setData] = useState<CIData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([
      getWorkflowCI(threadId, controller.signal),
      getWorkflowCIRuns(threadId, controller.signal).catch(() => ({ workflow_id: threadId, runs: [], total: 0 })),
      getWorkflowCIAudit(threadId, controller.signal).catch(() => null),
    ]).then(([status, runs, audit]) => {
      setData({ status, preview: null, runs: runs.runs, audit });
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "No se pudo consultar CI.");
    });
    return () => controller.abort();
  }, [threadId]);

  const prepare = async () => {
    setBusy(true); setError(null);
    try {
      const preview = await prepareWorkflowCI(threadId);
      const status = await getWorkflowCI(threadId);
      const runs = await getWorkflowCIRuns(threadId);
      const audit = await getWorkflowCIAudit(threadId).catch(() => data?.audit ?? null);
      setData({ status, preview, runs: runs.runs, audit });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "No se pudo preparar CI.");
    } finally { setBusy(false); }
  };
  const run = async () => {
    const fingerprint = data?.preview?.pipeline_fingerprint ?? data?.status.pipeline_fingerprint;
    if (!fingerprint) return;
    setBusy(true); setError(null);
    try {
      const result = await runWorkflowCI(threadId, fingerprint);
      const status = await getWorkflowCI(threadId);
      const runs = await getWorkflowCIRuns(threadId);
      const audit = await getWorkflowCIAudit(threadId).catch(() => data?.audit ?? null);
      setData({ status: { ...status, latest_run: result }, preview: data?.preview ?? null, runs: runs.runs, audit });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "No se pudo ejecutar CI.");
    } finally { setBusy(false); }
  };

  if (error) return <div className="git-empty" role="alert">{error}</div>;
  if (!data) return <div className="git-empty" role="status"><LoaderCircle className="spin" aria-hidden="true" />Consultando CI...</div>;
  if (data.status.state === "project_unavailable") return <section className="git-empty" aria-label="CI"><div><h2>CI</h2><p>El proyecto no esta disponible.</p></div></section>;

  const pipeline = data.preview?.pipeline ?? data.status.pipeline;
  const source = data.preview?.source ?? data.status.source;
  const latest = data.status.latest_run ?? data.runs[0] ?? null;
  const repair = data.status.repair;
  const showRepair = latest?.decision === "rejected" || Boolean(repair && repair.state !== "not_required");
  const gates = latest?.gates ?? data.preview?.expected_gates.map((gate) => ({
    gate_id: gate.gate_id,
    type: gate.type,
    status: gate.required ? "skipped" : "not_applicable",
    required: gate.required,
    blocking: gate.blocking,
    reason: gate.required ? "pending_run" : "no_source_steps",
    source_steps: [],
    failure_type: null,
  })) ?? [];
  const steps = latest?.steps ?? pipeline?.steps.map((step) => ({
    ...step,
    status: "pending",
    started_at: null,
    completed_at: null,
    duration_seconds: null,
    exit_code: null,
    stdout_summary: null,
    stderr_summary: null,
    output_truncated: false,
    failure_type: null,
    failure_message: null,
  })) ?? [];

  return <div className="git-view ci-view" aria-label="CI del workflow">
    <section className="git-repository">
      <div className="section-heading"><ShieldCheck aria-hidden="true" /><div><h2>CI Pipeline</h2><p>Ejecucion local durable, sin deployment</p></div></div>
      <dl className="git-facts">
        <div><dt>Status</dt><dd><span className={`status-badge ${statusClass(latest?.status)}`}>{latest?.status ?? data.status.state}</span></dd></div>
        <div><dt>CI Decision</dt><dd><span className={`status-badge ${statusClass(latest?.decision)}`}>{latest?.decision ?? "pending"}</span></dd></div>
        <div><dt>Framework</dt><dd>{pipeline?.framework ?? "n/a"}</dd></div>
        <div><dt>Version</dt><dd>{pipeline?.version ?? "n/a"}</dd></div>
        <div><dt>Source mode</dt><dd>{source?.source_mode ?? "n/a"}</dd></div>
        <div><dt>Branch</dt><dd>{source?.source_branch ?? "n/a"}</dd></div>
        <div><dt>Commit</dt><dd><code title={source?.source_commit ?? undefined}>{short(source?.source_commit ?? null)}</code></dd></div>
        <div><dt>Validated commit</dt><dd><code title={latest?.ci_validated_commit ?? undefined}>{short(latest?.ci_validated_commit ?? null)}</code></dd></div>
        <div><dt>Promotion eligible</dt><dd><span className={`status-badge ${statusClass(data.status.promotion_eligible === true ? "passed" : data.status.promotion_eligible === false ? "failed" : "pending")}`}>{data.status.promotion_eligible === true ? "YES" : data.status.promotion_eligible === false ? "NO" : "n/a"}</span></dd></div>
        <div><dt>Promotion reason</dt><dd>{data.status.promotion_eligibility?.reason ?? "n/a"}</dd></div>
        <div><dt>Duration</dt><dd>{duration(latest?.duration_seconds ?? null)}</dd></div>
        <div><dt>Fingerprint</dt><dd><code title={data.preview?.pipeline_fingerprint ?? data.status.pipeline_fingerprint ?? undefined}>{short(data.preview?.pipeline_fingerprint ?? data.status.pipeline_fingerprint ?? null)}</code></dd></div>
      </dl>
      <div className="approval-actions">
        <button type="button" className="approve-button" disabled={busy} onClick={() => void prepare()}><RefreshCw aria-hidden="true" />Prepare</button>
        <button type="button" className="approve-button" disabled={busy || !pipeline} onClick={() => void run()}><Play aria-hidden="true" />Run</button>
      </div>
    </section>
    <section className="git-changes">
      <h2>Gates</h2>
      <div className="table-scroll"><table><thead><tr><th>Gate</th><th>Required</th><th>Blocking</th><th>Status</th><th>Reason</th></tr></thead><tbody>
        {gates.map((gate) => <tr key={gate.gate_id}>
          <td>{gate.type}</td><td>{gate.required ? "yes" : "no"}</td><td>{gate.blocking ? "yes" : "no"}</td>
          <td><span className={`status-badge ${statusClass(gate.status)}`}>{gate.status}</span></td><td>{gate.reason}</td>
        </tr>)}
      </tbody></table></div>
    </section>
    {showRepair ? <section className="git-changes">
      <h2>Failure classification</h2>
      <dl className="git-facts">
        <div><dt>Repairable</dt><dd>{(latest?.repairability ?? repair?.repairability)?.repairable ? "yes" : "no"}</dd></div>
        <div><dt>Category</dt><dd>{latest?.repairability?.category ?? repair?.category ?? "n/a"}</dd></div>
        <div><dt>Repair state</dt><dd>{repair?.state ?? "n/a"}</dd></div>
        <div><dt>Attempts</dt><dd>{repair ? `${repair.attempts} / ${repair.max_attempts}` : "n/a"}</dd></div>
        <div><dt>Source run</dt><dd><code title={repair?.source_run_id ?? undefined}>{short(repair?.source_run_id ?? null)}</code></dd></div>
        <div><dt>Source commit</dt><dd><code title={repair?.source_commit ?? undefined}>{short(repair?.source_commit ?? null)}</code></dd></div>
        <div><dt>Target commit</dt><dd><code title={repair?.target_commit ?? undefined}>{short(repair?.target_commit ?? null)}</code></dd></div>
      </dl>
      {repair?.reason_codes?.length ? <p>{repair.reason_codes.join(", ")}</p> : null}
      {repair?.lineage?.length ? <div className="table-scroll"><table><thead><tr><th>Attempt</th><th>Failed run</th><th>Source</th><th>Repair commit</th><th>Result run</th><th>Decision</th></tr></thead><tbody>
        {repair.lineage.map((item, index) => <tr key={`repair-${index}`}>
          <td>{String(item.attempt ?? index + 1)}</td>
          <td><code title={String(item.source_run_id ?? "")}>{short(String(item.source_run_id ?? ""))}</code></td>
          <td><code title={String(item.source_commit ?? "")}>{short(String(item.source_commit ?? ""))}</code></td>
          <td><code title={String(item.repair_commit ?? "")}>{short(String(item.repair_commit ?? ""))}</code></td>
          <td><code title={String(item.result_run_id ?? "")}>{short(String(item.result_run_id ?? ""))}</code></td>
          <td>{String(item.result_decision ?? "n/a")}</td>
        </tr>)}
      </tbody></table></div> : null}
    </section> : null}
    <section className="git-changes">
      <h2>CI Audit Trail</h2>
      <div className="table-scroll"><table><thead><tr><th>Event</th><th>Commit</th><th>Run</th><th>Gate/Step</th><th>Decision</th><th>Failure</th></tr></thead><tbody>
        {(data.audit?.entries ?? []).map((entry, index) => <tr key={`${entry.event_type}-${entry.ci_run_id ?? index}-${index}`}>
          <td>{entry.event_type.replaceAll("_", " ")}</td>
          <td><code title={entry.commit ?? entry.repair_commit ?? undefined}>{short(entry.commit ?? entry.repair_commit ?? null)}</code></td>
          <td><code title={entry.ci_run_id ?? undefined}>{short(entry.ci_run_id ?? null)}</code></td>
          <td>{entry.gate ?? entry.step_id ?? "n/a"}</td>
          <td>{entry.decision ?? (entry.promotion_eligible === null ? "n/a" : entry.promotion_eligible ? "promotion eligible" : "promotion blocked")}</td>
          <td>{entry.failure_type ?? "n/a"}</td>
        </tr>)}
      </tbody></table>{!data.audit?.entries.length ? <p className="git-empty">Sin eventos de auditoria CI.</p> : null}</div>
    </section>
    <section className="git-changes">
      <h2>Steps</h2>
      <div className="table-scroll"><table><thead><tr><th>Name</th><th>Type</th><th>Status</th><th>Duration</th><th>Exit code</th></tr></thead><tbody>
        {steps.map((step) => <tr key={step.step_id}>
          <td>{step.name}</td><td>{step.type}</td>
          <td><span className={`status-badge ${statusClass(step.status)}`}>{step.status}</span></td>
          <td>{duration(step.duration_seconds)}</td><td>{step.exit_code ?? "n/a"}</td>
        </tr>)}
      </tbody></table></div>
    </section>
    {latest?.failed_step || latest?.blocking_gate ? <section className="git-commit-preview" role="alert"><h2>Failure summary</h2><dl className="git-facts"><div><dt>Failed step</dt><dd>{latest.failed_step ?? "n/a"}</dd></div><div><dt>Blocking gate</dt><dd>{latest.blocking_gate ?? "n/a"}</dd></div><div><dt>Failure type</dt><dd>{latest.failure_type ?? "n/a"}</dd></div><div><dt>Message</dt><dd>{latest.failure_message ?? "n/a"}</dd></div></dl></section> : null}
    {latest ? <section className="git-history"><h2>Output summaries</h2>{latest.steps.map((step) => <details key={`out-${step.step_id}`}><summary>{step.status === "passed" ? <CheckCircle2 aria-hidden="true" /> : <XCircle aria-hidden="true" />} {step.name}</summary><pre>{step.stdout_summary || step.stderr_summary || "n/a"}</pre>{step.output_truncated ? <p className="git-truncated">Output truncado por el limite configurado.</p> : null}</details>)}</section> : null}
  </div>;
}
