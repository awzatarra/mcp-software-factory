import type { WorkflowSnapshot } from "../api/types";

function value(
  record: Record<string, unknown>,
  ...keys: string[]
): string {
  for (const key of keys) {
    const selected = record[key];
    if (selected !== undefined && selected !== null) return String(selected);
  }
  return "n/a";
}

export function WorkflowSummary({
  snapshot,
}: {
  snapshot: WorkflowSnapshot;
}) {
  return (
    <section className="summary-panel" aria-labelledby="summary-title">
      <div className="section-heading compact">
        <div>
          <span className="section-kicker">Estado</span>
          <h2 id="summary-title">Resumen</h2>
        </div>
        <span className={`terminal-badge terminal-${snapshot.terminal_status}`}>
          {snapshot.terminal_status}
        </span>
      </div>
      <dl className="summary-list">
        <div><dt>Proyecto</dt><dd>{snapshot.project_name ?? "n/a"}</dd></div>
        <div><dt>Intención</dt><dd>{snapshot.workflow_intent ?? "n/a"}</dd></div>
        <div><dt>Tests</dt><dd>{value(snapshot.testing, "final_test_result_summary", "passed")}</dd></div>
        <div><dt>Planning</dt><dd>{value(snapshot.planning, "attempts")} intentos</dd></div>
        <div><dt>Implementation</dt><dd>{value(snapshot.implementation, "attempts")} intentos</dd></div>
        <div><dt>Repair</dt><dd>{value(snapshot.testing, "repair_phase")}</dd></div>
        <div><dt>Supervisor</dt><dd>{value(snapshot.supervisor, "decision")}</dd></div>
      </dl>
    </section>
  );
}
