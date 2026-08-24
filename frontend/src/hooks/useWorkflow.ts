import { useCallback, useEffect } from "react";

import { ApiClientError } from "../api/client";
import { getWorkflow } from "../api/workflows";
import { useWorkflowStore } from "../stores/workflowStore";
import {
  loadWorkflow,
  releaseWorkflowLoad,
} from "./workflowLoader";

function connectionStatusForTerminal(
  terminalStatus: string,
): "completed" | "disconnected" | null {
  if (terminalStatus === "completed") return "completed";
  if (!["pending", "running"].includes(terminalStatus)) return "disconnected";
  return null;
}

export function useWorkflow(threadId: string, branchId = "original") {
  const setSnapshot = useWorkflowStore((state) => state.setSnapshot);
  const setError = useWorkflowStore((state) => state.setError);
  const reset = useWorkflowStore((state) => state.reset);
  const setConnectionStatus = useWorkflowStore(
    (state) => state.setConnectionStatus,
  );

  const refreshSnapshot = useCallback(async () => {
    try {
      const snapshot = await getWorkflow(threadId);
      setSnapshot(snapshot);
      const terminalConnectionStatus = connectionStatusForTerminal(
        snapshot.terminal_status,
      );
      if (terminalConnectionStatus) {
        setConnectionStatus(terminalConnectionStatus);
      }
      return snapshot;
    } catch (error) {
      const uiError =
        error instanceof ApiClientError
          ? error.toUiError(
              error.statusCode === 404
                ? "Workflow no encontrado"
                : "No se pudo cargar el workflow",
            )
          : {
              title: "No se pudo cargar el workflow",
              message: "Ocurrió un error inesperado.",
              retryable: true,
            };
      setError(uiError);
      throw error;
    }
  }, [setConnectionStatus, setError, setSnapshot, threadId]);

  useEffect(() => {
    const current = useWorkflowStore.getState();
    if (current.threadId !== threadId || current.branchId !== branchId) {
      reset(threadId, branchId);
    }
    void loadWorkflow(threadId, branchId);
    return () => {
      releaseWorkflowLoad(threadId, branchId);
    };
  }, [branchId, reset, threadId]);

  const reload = useCallback(
    () => loadWorkflow(threadId, branchId, true),
    [branchId, threadId],
  );
  return { refreshSnapshot, reload };
}
