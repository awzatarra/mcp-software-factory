import { useEffect } from "react";

import {
  WORKFLOW_EVENT_TYPES,
  type WorkflowEvent,
} from "../api/types";
import { getWorkflow, workflowEventsUrl } from "../api/workflows";
import { useWorkflowStore } from "../stores/workflowStore";

const SNAPSHOT_EVENTS = new Set<WorkflowEvent["type"]>([
  "approval_required",
  "approval_granted",
  "approval_rejected",
  "tool_completed",
  "test_run_completed",
  "testing_completed",
  "workflow_completed",
  "workflow_failed",
  "supervisor_loop_detected",
]);
const TERMINAL_EVENTS = new Set<WorkflowEvent["type"]>([
  "workflow_completed",
  "workflow_failed",
]);
const STRICT_MODE_GRACE_MS = 50;

interface ManagedEventStream {
  key: string;
  version: number;
  source: EventSource;
  consume: EventListener;
  consumers: number;
  refreshTimer: number | null;
  cleanupTimer: number | null;
  terminalController: AbortController;
  terminalRefresh: Promise<void> | null;
}

const activeEventStreams = new Map<string, ManagedEventStream>();

export function parseWorkflowEvent(raw: string): WorkflowEvent | null {
  try {
    const parsed = JSON.parse(raw) as Partial<WorkflowEvent>;
    if (
      typeof parsed.event_id !== "string" ||
      typeof parsed.thread_id !== "string" ||
      typeof parsed.sequence !== "number" ||
      typeof parsed.type !== "string" ||
      !WORKFLOW_EVENT_TYPES.includes(parsed.type as WorkflowEvent["type"]) ||
      typeof parsed.data !== "object" ||
      parsed.data === null
    ) {
      return null;
    }
    return parsed as WorkflowEvent;
  } catch {
    return null;
  }
}

function closeManagedStream(
  stream: ManagedEventStream,
  abortTerminal = true,
): void {
  if (abortTerminal) stream.terminalController.abort();
  stream.source.close();
  for (const eventType of WORKFLOW_EVENT_TYPES) {
    stream.source.removeEventListener(eventType, stream.consume);
  }
  if (stream.refreshTimer !== null) {
    window.clearTimeout(stream.refreshTimer);
  }
  if (stream.cleanupTimer !== null) {
    window.clearTimeout(stream.cleanupTimer);
  }
}

function waitForRetry(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const timer = window.setTimeout(resolve, ms);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

function snapshotMatchesTerminal(
  terminalType: WorkflowEvent["type"],
  terminalStatus: string,
): boolean {
  return terminalType === "workflow_completed"
    ? terminalStatus === "completed"
    : !["pending", "running"].includes(terminalStatus);
}

async function refreshTerminalSnapshot(
  threadId: string,
  terminalType: WorkflowEvent["type"],
  signal: AbortSignal,
): Promise<void> {
  for (const delay of [100, 250, 500]) {
    await waitForRetry(delay, signal);
    const snapshot = await getWorkflow(threadId);
    useWorkflowStore.getState().setSnapshot(snapshot);
    if (snapshotMatchesTerminal(terminalType, snapshot.terminal_status)) {
      return;
    }
  }
}

export function handleTerminalEvent(
  threadId: string,
  event: WorkflowEvent,
  stream: ManagedEventStream,
): Promise<void> {
  if (stream.terminalRefresh) return stream.terminalRefresh;
  if (stream.refreshTimer !== null) {
    window.clearTimeout(stream.refreshTimer);
    stream.refreshTimer = null;
  }
  stream.terminalRefresh = refreshTerminalSnapshot(
    threadId,
    event.type,
    stream.terminalController.signal,
  )
    .catch((error: unknown) => {
      if (
        !(error instanceof DOMException && error.name === "AbortError")
      ) {
        useWorkflowStore.getState().setError({
          title: "No se pudo sincronizar el resultado final",
          message:
            "El evento terminal llegó, pero el snapshot final no pudo recuperarse.",
          retryable: true,
          scope: "connection",
        });
      }
    })
    .finally(() => {
      if (stream.terminalController.signal.aborted) return;
      useWorkflowStore.getState().setConnectionStatus(
        event.type === "workflow_completed" ? "completed" : "error",
      );
      if (activeEventStreams.get(stream.key) === stream) {
        activeEventStreams.delete(stream.key);
      }
      closeManagedStream(stream, false);
    });
  return stream.terminalRefresh;
}

function createManagedStream(
  threadId: string,
  branchId: string,
  version: number,
): ManagedEventStream {
  const key = `${threadId}:${branchId}`;
  const state = useWorkflowStore.getState();
  const lastSequence = state.events.at(-1)?.sequence ?? 0;
  state.setConnectionStatus("connecting");
  const source = new EventSource(
    workflowEventsUrl(threadId, lastSequence, branchId),
  );
  const stream: ManagedEventStream = {
    key,
    version,
    source,
    consumers: 0,
    refreshTimer: null,
    cleanupTimer: null,
    terminalController: new AbortController(),
    terminalRefresh: null,
    consume: (() => undefined) as EventListener,
  };

  const refreshSnapshot = () => {
    if (stream.refreshTimer !== null) {
      window.clearTimeout(stream.refreshTimer);
    }
    stream.refreshTimer = window.setTimeout(() => {
      void getWorkflow(threadId)
        .then((snapshot) => useWorkflowStore.getState().setSnapshot(snapshot))
        .catch(() => undefined);
    }, 180);
  };

  stream.consume = ((message: MessageEvent<string>) => {
    const event = parseWorkflowEvent(message.data);
    if (!event) return;
    const current = useWorkflowStore.getState();
    current.appendEvent(event);
    if (TERMINAL_EVENTS.has(event.type)) {
      void handleTerminalEvent(threadId, event, stream);
    } else if (SNAPSHOT_EVENTS.has(event.type)) {
      refreshSnapshot();
    }
  }) as EventListener;

  for (const eventType of WORKFLOW_EVENT_TYPES) {
    source.addEventListener(eventType, stream.consume);
  }
  source.onmessage = stream.consume as (
    event: MessageEvent<string>,
  ) => void;
  source.onopen = () => {
    useWorkflowStore.getState().setConnectionStatus("connected");
  };
  source.onerror = () => {
    const status = useWorkflowStore.getState().connectionStatus;
    if (status !== "completed" && status !== "error") {
      useWorkflowStore.getState().setConnectionStatus("reconnecting");
    }
  };
  activeEventStreams.set(key, stream);
  return stream;
}

function acquireEventStream(
  threadId: string,
  branchId: string,
  version: number,
): void {
  const key = `${threadId}:${branchId}`;
  const existing = activeEventStreams.get(key);
  if (existing?.version === version) {
    if (existing.cleanupTimer !== null) {
      window.clearTimeout(existing.cleanupTimer);
      existing.cleanupTimer = null;
    }
    existing.consumers += 1;
    return;
  }
  if (existing) {
    closeManagedStream(existing);
    activeEventStreams.delete(key);
  }
  createManagedStream(threadId, branchId, version).consumers = 1;
}

function releaseEventStream(threadId: string, branchId: string): void {
  const key = `${threadId}:${branchId}`;
  const stream = activeEventStreams.get(key);
  if (!stream) return;
  stream.consumers = Math.max(0, stream.consumers - 1);
  if (stream.consumers > 0 || stream.cleanupTimer !== null) return;
  stream.cleanupTimer = window.setTimeout(() => {
    if (
      stream.consumers === 0 &&
      activeEventStreams.get(key) === stream
    ) {
      activeEventStreams.delete(key);
      closeManagedStream(stream);
    }
  }, STRICT_MODE_GRACE_MS);
}

export function resetEventStreamsForTests(): void {
  for (const stream of activeEventStreams.values()) {
    closeManagedStream(stream);
  }
  activeEventStreams.clear();
}

export function useWorkflowEvents(
  threadId: string,
  enabled = true,
  streamVersion = 0,
  branchId = "original",
): void {
  useEffect(() => {
    if (!enabled) return;
    acquireEventStream(threadId, branchId, streamVersion);
    return () => {
      releaseEventStream(threadId, branchId);
    };
  }, [branchId, enabled, streamVersion, threadId]);
}
