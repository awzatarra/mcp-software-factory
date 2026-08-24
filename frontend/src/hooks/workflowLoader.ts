import { ApiClientError } from "../api/client";
import {
  getCompleteWorkflowHistory,
  getWorkflow,
} from "../api/workflows";
import { useWorkflowStore } from "../stores/workflowStore";

interface ActiveWorkflowLoad {
  controller: AbortController;
  promise: Promise<void>;
  consumers: number;
  settled: boolean;
  cleanupTimer: number | null;
}

const activeLoads = new Map<string, ActiveWorkflowLoad>();
const STRICT_MODE_GRACE_MS = 50;

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function terminalConnectionStatus(
  terminalStatus: string,
): "completed" | "disconnected" | null {
  if (terminalStatus === "completed") return "completed";
  if (!["pending", "running"].includes(terminalStatus)) return "disconnected";
  return null;
}

function uiErrorForLoad(error: unknown) {
  return error instanceof ApiClientError
    ? error.toUiError(
        error.statusCode === 404
          ? "Workflow no encontrado"
          : "No se pudieron recuperar los datos",
      )
    : {
        title: "No se pudieron recuperar los datos",
        message: "Ocurrió un error inesperado.",
        retryable: true,
      };
}

function loadKey(threadId: string, branchId: string): string {
  return `${threadId}:${branchId}`;
}

function createLoad(threadId: string, branchId: string): ActiveWorkflowLoad {
  const key = loadKey(threadId, branchId);
  const controller = new AbortController();
  const entry: ActiveWorkflowLoad = {
    controller,
    consumers: 0,
    settled: false,
    cleanupTimer: null,
    promise: Promise.resolve(),
  };
  entry.promise = (async () => {
    const store = useWorkflowStore.getState();
    if (store.threadId === threadId && store.branchId === branchId) {
      store.setLoading(true);
      store.setError(null);
    }
    try {
      const [snapshot, history] = await Promise.all([
        getWorkflow(threadId, controller.signal),
        getCompleteWorkflowHistory(threadId, controller.signal, branchId),
      ]);
      const current = useWorkflowStore.getState();
      if (
        current.threadId !== threadId ||
        current.branchId !== branchId ||
        controller.signal.aborted
      ) return;
      current.setSnapshot(snapshot);
      current.replaceHistory(history.events);
      const connectionStatus = terminalConnectionStatus(
        snapshot.terminal_status,
      );
      if (connectionStatus) current.setConnectionStatus(connectionStatus);
    } catch (error) {
      if (isAbortError(error)) return;
      const current = useWorkflowStore.getState();
      if (current.threadId === threadId && current.branchId === branchId) {
        current.setError(uiErrorForLoad(error));
      }
    } finally {
      entry.settled = true;
      const current = useWorkflowStore.getState();
      if (current.threadId === threadId && current.branchId === branchId) {
        current.setLoading(false);
      }
    }
  })();
  activeLoads.set(key, entry);
  return entry;
}

export function loadWorkflow(
  threadId: string,
  branchId = "original",
  force = false,
): Promise<void> {
  const key = loadKey(threadId, branchId);
  const existing = activeLoads.get(key);
  if (existing && !force) {
    if (existing.cleanupTimer !== null) {
      window.clearTimeout(existing.cleanupTimer);
      existing.cleanupTimer = null;
    }
    existing.consumers += 1;
    return existing.promise;
  }
  if (existing) {
    existing.controller.abort();
    if (existing.cleanupTimer !== null) {
      window.clearTimeout(existing.cleanupTimer);
    }
    activeLoads.delete(key);
  }
  const created = createLoad(threadId, branchId);
  created.consumers = 1;
  return created.promise;
}

export function releaseWorkflowLoad(
  threadId: string,
  branchId = "original",
): void {
  const key = loadKey(threadId, branchId);
  const entry = activeLoads.get(key);
  if (!entry) return;
  entry.consumers = Math.max(0, entry.consumers - 1);
  if (entry.consumers > 0 || entry.cleanupTimer !== null) return;
  entry.cleanupTimer = window.setTimeout(() => {
    if (entry.consumers > 0) return;
    if (!entry.settled) entry.controller.abort();
    activeLoads.delete(key);
  }, STRICT_MODE_GRACE_MS);
}

export function resetWorkflowLoadsForTests(): void {
  for (const entry of activeLoads.values()) {
    entry.controller.abort();
    if (entry.cleanupTimer !== null) {
      window.clearTimeout(entry.cleanupTimer);
    }
  }
  activeLoads.clear();
}
