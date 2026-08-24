import { Ban, Check, LoaderCircle, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { ApiClientError } from "../api/client";
import type { WorkflowEvent, WorkflowSnapshot } from "../api/types";
import {
  approveWorkflow,
  getWorkflow,
  rejectWorkflow,
} from "../api/workflows";
import { useWorkflowStore } from "../stores/workflowStore";

interface ApprovalPanelProps {
  snapshot: WorkflowSnapshot;
  events: WorkflowEvent[];
}

function safePreview(events: WorkflowEvent[]): Record<string, unknown> | null {
  const approval = [...events]
    .reverse()
    .find((event) => event.type === "approval_required");
  const preview = approval?.data.preview;
  return typeof preview === "object" && preview !== null
    ? (preview as Record<string, unknown>)
    : null;
}

export function ApprovalPanel({ snapshot, events }: ApprovalPanelProps) {
  const [reason, setReason] = useState("");
  const submitting = useWorkflowStore((state) => state.approvalSubmitting);
  const approvalLock = useWorkflowStore((state) => state.approvalLock);
  const acquireApprovalLock = useWorkflowStore(
    (state) => state.acquireApprovalLock,
  );
  const clearApprovalLock = useWorkflowStore(
    (state) => state.clearApprovalLock,
  );
  const setSnapshot = useWorkflowStore((state) => state.setSnapshot);
  const setError = useWorkflowStore((state) => state.setError);
  const restartEventStream = useWorkflowStore(
    (state) => state.restartEventStream,
  );
  const preview = useMemo(() => safePreview(events), [events]);

  useEffect(() => {
    if (!approvalLock) return;
    const timeout = window.setTimeout(() => {
      void getWorkflow(approvalLock.threadId)
        .then((latest) => {
          useWorkflowStore.getState().setSnapshot(latest);
          const current = useWorkflowStore.getState();
          if (
            current.approvalLock === approvalLock &&
            latest.interrupted &&
            latest.pending_operation === approvalLock.operation &&
            latest.pending_tool === approvalLock.toolName
          ) {
            current.clearApprovalLock();
          }
        })
        .catch(() => {
          const current = useWorkflowStore.getState();
          if (current.approvalLock === approvalLock) {
            current.clearApprovalLock();
          }
        });
    }, 30_000);
    return () => window.clearTimeout(timeout);
  }, [approvalLock]);

  if (
    !snapshot.interrupted ||
    !snapshot.pending_operation ||
    !snapshot.pending_tool
  ) {
    return null;
  }

  const resolve = async (approved: boolean) => {
    const lock = {
      threadId: snapshot.thread_id,
      operation: snapshot.pending_operation!,
      toolName: snapshot.pending_tool,
      startedAt: Date.now(),
    };
    if (!acquireApprovalLock(lock)) return;
    setError(null);
    try {
      const action = approved ? approveWorkflow : rejectWorkflow;
      await action(snapshot.thread_id, reason);
      restartEventStream();
    } catch (error) {
      const isConflict =
        error instanceof ApiClientError && error.statusCode === 409;
      const apiError =
        error instanceof ApiClientError
          ? {
              ...error.toUiError(
                isConflict
                ? "Operación ya resuelta"
                : "No se pudo enviar la decisión",
              ),
              retryable: !isConflict && error.retryable,
              scope: "approval" as const,
            }
          : {
              title: "No se pudo enviar la decisión",
              message: "Ocurrió un error inesperado.",
              retryable: true,
              scope: "approval" as const,
            };
      clearApprovalLock();
      if (isConflict) {
        try {
          setSnapshot(await getWorkflow(snapshot.thread_id));
        } catch {
          // The conflict remains readable even if reconciliation is unavailable.
        }
      }
      setError(apiError);
    }
  };

  return (
    <section className="approval-panel" aria-labelledby="approval-title">
      <div className="approval-title">
        <ShieldCheck aria-hidden="true" />
        <div>
          <span>Acción sensible</span>
          <h2 id="approval-title">Aprobación requerida</h2>
        </div>
      </div>
      <dl className="approval-facts">
        <div>
          <dt>Operación</dt>
          <dd>{snapshot.pending_operation.replaceAll("_", " ")}</dd>
        </div>
        <div>
          <dt>Tool</dt>
          <dd><code>{snapshot.pending_tool ?? "n/a"}</code></dd>
        </div>
        <div>
          <dt>Proyecto</dt>
          <dd>{snapshot.project_name ?? "n/a"}</dd>
        </div>
      </dl>
      {preview ? (
        <div className="approval-preview">
          <span>Preview sanitizado</span>
          <pre>{JSON.stringify(preview, null, 2)}</pre>
        </div>
      ) : null}
      <label htmlFor="approval-reason">Razón opcional</label>
      <textarea
        id="approval-reason"
        rows={3}
        maxLength={1_000}
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        placeholder="Contexto para la decisión"
      />
      <div className="approval-actions">
        <button
          className="approve-button"
          type="button"
          disabled={submitting}
          onClick={() => void resolve(true)}
        >
          {submitting ? (
            <LoaderCircle className="spin" aria-hidden="true" />
          ) : (
            <Check aria-hidden="true" />
          )}
          Aprobar
        </button>
        <button
          className="reject-button"
          type="button"
          disabled={submitting}
          onClick={() => void resolve(false)}
        >
          <Ban aria-hidden="true" />
          Rechazar
        </button>
      </div>
    </section>
  );
}
