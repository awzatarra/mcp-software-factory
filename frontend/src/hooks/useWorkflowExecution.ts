import { useCallback, useEffect, useMemo, useRef } from "react";

import { ApiClientError } from "../api/client";
import type { WorkflowEvent } from "../api/types";
import { getWorkflowExecution } from "../api/workflows";
import { useWorkflowExecutionStore } from "../stores/workflowExecutionStore";

const REFRESH_EVENTS = new Set<WorkflowEvent["type"]>([
  "planning_completed",
  "approval_required",
  "approval_granted",
  "implementation_validation_completed",
  "implementation_completed",
  "test_run_completed",
  "repair_started",
  "repair_completed",
  "workflow_completed",
  "workflow_failed",
]);

interface SharedLoad {
  controller: AbortController;
  promise: ReturnType<typeof getWorkflowExecution>;
}

const executionLoads = new Map<string, SharedLoad>();

export function resetExecutionLoadsForTests(): void {
  for (const load of executionLoads.values()) load.controller.abort();
  executionLoads.clear();
}

function executionError(error: unknown) {
  if (error instanceof ApiClientError) {
    return {
      ...error.toUiError("No pudimos actualizar la ejecución"),
      scope: "execution" as const,
    };
  }
  return {
    title: "No pudimos actualizar la ejecución",
    message: "Ocurrió un error inesperado.",
    retryable: true,
    scope: "execution" as const,
  };
}

export function useWorkflowExecution(
  threadId: string,
  branchId: string,
  active: boolean,
  events: WorkflowEvent[],
) {
  const key = `${threadId}:${branchId}`;
  const storedKey = useWorkflowExecutionStore((state) => state.key);
  const reset = useWorkflowExecutionStore((state) => state.reset);
  const setLoading = useWorkflowExecutionStore((state) => state.setLoading);
  const setExecution = useWorkflowExecutionStore((state) => state.setExecution);
  const setError = useWorkflowExecutionStore((state) => state.setError);
  const version = useRef(0);
  const latestRelevantEvent = useMemo(
    () => events.filter((event) => REFRESH_EVENTS.has(event.type)).at(-1),
    [events],
  );

  const load = useCallback(
    async (refresh = false) => {
      const requestVersion = ++version.current;
      setLoading(refresh);
      let shared = executionLoads.get(key);
      if (!shared) {
        const controller = new AbortController();
        const promise = getWorkflowExecution(
          threadId,
          branchId,
          controller.signal,
        ).finally(() => executionLoads.delete(key));
        shared = { controller, promise };
        executionLoads.set(key, shared);
      }
      try {
        const result = await shared.promise;
        if (requestVersion === version.current) setExecution(result);
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        if (requestVersion === version.current) setError(executionError(error));
      }
    },
    [branchId, key, setError, setExecution, setLoading, threadId],
  );

  useEffect(() => {
    if (storedKey !== key) reset(key);
  }, [key, reset, storedKey]);

  useEffect(() => {
    if (active) {
      void load(Boolean(useWorkflowExecutionStore.getState().execution));
    }
  }, [active, load]);

  useEffect(() => {
    if (!active || !latestRelevantEvent) return;
    const timer = window.setTimeout(() => void load(true), 250);
    return () => window.clearTimeout(timer);
  }, [active, latestRelevantEvent, load]);

  useEffect(
    () => () => {
      version.current += 1;
    },
    [],
  );

  return { load };
}
