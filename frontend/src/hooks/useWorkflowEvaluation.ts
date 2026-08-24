import { useCallback, useEffect, useMemo, useRef } from "react";

import { ApiClientError } from "../api/client";
import type { WorkflowEvent } from "../api/types";
import { getWorkflowEvaluation } from "../api/workflows";
import { useWorkflowEvaluationStore } from "../stores/workflowEvaluationStore";

const REFRESH_EVENTS = new Set<WorkflowEvent["type"]>([
  "planning_completed",
  "approval_required",
  "approval_granted",
  "implementation_completed",
  "test_run_completed",
  "repair_started",
  "repair_completed",
  "workflow_completed",
  "workflow_failed",
  "supervisor_loop_detected",
]);

interface SharedEvaluationLoad {
  controller: AbortController;
  promise: ReturnType<typeof getWorkflowEvaluation>;
}

const evaluationLoads = new Map<string, SharedEvaluationLoad>();

export function resetEvaluationLoadsForTests(): void {
  for (const load of evaluationLoads.values()) load.controller.abort();
  evaluationLoads.clear();
}

function evaluationError(error: unknown) {
  if (error instanceof ApiClientError) {
    return {
      ...error.toUiError("No pudimos actualizar la evaluación"),
      scope: "evaluation" as const,
    };
  }
  return {
    title: "No pudimos actualizar la evaluación",
    message: "Ocurrió un error inesperado.",
    retryable: true,
    scope: "evaluation" as const,
  };
}

export function useWorkflowEvaluation(
  threadId: string,
  branchId: string,
  active: boolean,
  events: WorkflowEvent[],
) {
  const key = `${threadId}:${branchId}`;
  const storedKey = useWorkflowEvaluationStore((state) => state.key);
  const reset = useWorkflowEvaluationStore((state) => state.reset);
  const setLoading = useWorkflowEvaluationStore((state) => state.setLoading);
  const setEvaluation = useWorkflowEvaluationStore((state) => state.setEvaluation);
  const setError = useWorkflowEvaluationStore((state) => state.setError);
  const version = useRef(0);
  const latestRelevantEvent = useMemo(
    () => events.filter((event) => REFRESH_EVENTS.has(event.type)).at(-1),
    [events],
  );

  const load = useCallback(
    async (refresh = false) => {
      const requestVersion = ++version.current;
      setLoading(refresh);
      let shared = evaluationLoads.get(key);
      if (!shared) {
        const controller = new AbortController();
        const promise = getWorkflowEvaluation(
          threadId,
          branchId,
          controller.signal,
        ).finally(() => evaluationLoads.delete(key));
        shared = { controller, promise };
        evaluationLoads.set(key, shared);
      }
      try {
        const result = await shared.promise;
        if (requestVersion === version.current) setEvaluation(result);
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        if (requestVersion === version.current) setError(evaluationError(error));
      }
    },
    [branchId, key, setError, setEvaluation, setLoading, threadId],
  );

  useEffect(() => {
    if (storedKey !== key) reset(key);
  }, [key, reset, storedKey]);

  useEffect(() => {
    if (active) {
      const current = useWorkflowEvaluationStore.getState();
      if (!current.evaluation && !current.error) void load(false);
    }
  }, [active, load]);

  useEffect(() => {
    if (!active || !latestRelevantEvent) return;
    const timer = window.setTimeout(() => void load(true), 300);
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
