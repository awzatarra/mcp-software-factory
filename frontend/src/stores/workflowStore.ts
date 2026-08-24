import { create } from "zustand";

import type {
  ApprovalLock,
  UiError,
  WorkflowEvent,
  WorkflowSnapshot,
} from "../api/types";
import { mergeWorkflowEvents } from "../utils/eventDeduplication";

export type ConnectionStatus =
  | "idle"
  | "connecting"
  | "connected"
  | "reconnecting"
  | "disconnected"
  | "completed"
  | "error";

interface WorkflowStore {
  threadId: string | null;
  branchId: string;
  snapshot: WorkflowSnapshot | null;
  events: WorkflowEvent[];
  eventStreamVersion: number;
  connectionStatus: ConnectionStatus;
  isLoading: boolean;
  approvalSubmitting: boolean;
  approvalLock: ApprovalLock | null;
  error: UiError | null;
  setThreadId: (threadId: string) => void;
  setSnapshot: (snapshot: WorkflowSnapshot) => void;
  replaceHistory: (events: WorkflowEvent[]) => void;
  appendEvent: (event: WorkflowEvent) => void;
  appendEvents: (events: WorkflowEvent[]) => void;
  restartEventStream: () => void;
  setConnectionStatus: (status: ConnectionStatus) => void;
  setLoading: (loading: boolean) => void;
  setApprovalSubmitting: (submitting: boolean) => void;
  acquireApprovalLock: (lock: ApprovalLock) => boolean;
  clearApprovalLock: () => void;
  setError: (error: UiError | null) => void;
  reset: (threadId?: string, branchId?: string) => void;
}

const initialState = {
  threadId: null,
  branchId: "original",
  snapshot: null,
  events: [] as WorkflowEvent[],
  eventStreamVersion: 0,
  connectionStatus: "idle" as ConnectionStatus,
  isLoading: false,
  approvalSubmitting: false,
  approvalLock: null,
  error: null,
};

function conflictError(message?: string): UiError | null {
  return message
    ? {
        title: "Conflicto de eventos",
        message,
        retryable: true,
        scope: "history",
      }
    : null;
}

function isTerminal(snapshot: WorkflowSnapshot): boolean {
  return !["pending", "running"].includes(snapshot.terminal_status);
}

function lockResolvedBySnapshot(
  lock: ApprovalLock | null,
  snapshot: WorkflowSnapshot,
): boolean {
  if (!lock) return false;
  return (
    snapshot.thread_id !== lock.threadId ||
    !snapshot.interrupted ||
    isTerminal(snapshot) ||
    snapshot.pending_operation !== lock.operation ||
    snapshot.pending_tool !== lock.toolName
  );
}

function eventMatchesLock(
  event: WorkflowEvent,
  lock: ApprovalLock,
): boolean {
  return (
    event.thread_id === lock.threadId &&
    event.data.operation === lock.operation &&
    (event.data.tool_name === undefined ||
      event.data.tool_name === lock.toolName)
  );
}

function eventClearsLock(
  event: WorkflowEvent,
  lock: ApprovalLock | null,
): boolean {
  if (!lock) return false;
  if (
    ["workflow_completed", "workflow_failed"].includes(event.type)
  ) {
    return event.thread_id === lock.threadId;
  }
  if (
    ["approval_granted", "approval_rejected"].includes(event.type)
  ) {
    return eventMatchesLock(event, lock);
  }
  return (
    event.type === "approval_required" &&
    event.thread_id === lock.threadId &&
    (event.data.operation !== lock.operation ||
      event.data.tool_name !== lock.toolName)
  );
}

function snapshotWithEventSummary(
  snapshot: WorkflowSnapshot | null,
  event: WorkflowEvent,
): WorkflowSnapshot | null {
  if (!snapshot) return snapshot;
  if (event.type === "approval_required") {
    return {
      ...snapshot,
      interrupted: true,
      pending_operation:
        typeof event.data.operation === "string"
          ? event.data.operation
          : snapshot.pending_operation,
      pending_tool:
        typeof event.data.tool_name === "string"
          ? event.data.tool_name
          : snapshot.pending_tool,
    };
  }
  if (
    ["approval_granted", "approval_rejected"].includes(event.type) &&
    event.data.operation === snapshot.pending_operation
  ) {
    return {
      ...snapshot,
      interrupted: false,
      pending_operation: null,
      pending_tool: null,
    };
  }
  if (event.type !== "test_run_completed") return snapshot;
  return {
    ...snapshot,
    testing: {
      ...snapshot.testing,
      tests_executed: true,
      tests_passed: event.data.passed,
      final_test_result_summary: event.data.summary,
    },
  };
}

export const useWorkflowStore = create<WorkflowStore>((set, get) => ({
  ...initialState,
  setThreadId: (threadId) => set({ threadId }),
  setSnapshot: (snapshot) =>
    set((state) => {
      const clearApproval = lockResolvedBySnapshot(
        state.approvalLock,
        snapshot,
      );
      const pendingChanged =
        state.snapshot?.pending_operation !== snapshot.pending_operation ||
        state.snapshot?.pending_tool !== snapshot.pending_tool;
      return {
        snapshot,
        approvalLock: clearApproval ? null : state.approvalLock,
        approvalSubmitting: clearApproval
          ? false
          : state.approvalSubmitting,
        error:
          state.error?.scope === "approval" &&
          (pendingChanged || !snapshot.interrupted || isTerminal(snapshot))
            ? null
            : state.error,
      };
    }),
  replaceHistory: (events) =>
    set((state) => {
      const merged = mergeWorkflowEvents(state.events, events);
      return {
        events: merged.events,
        error: conflictError(merged.conflict) ?? state.error,
      };
    }),
  appendEvent: (event) =>
    set((state) => {
      const merged = mergeWorkflowEvents(state.events, [event]);
      const clearApproval = eventClearsLock(event, state.approvalLock);
      const clearsApprovalError =
        state.error?.scope === "approval" &&
        (event.type === "approval_required" ||
          event.type === "approval_granted" ||
          event.type === "approval_rejected" ||
          event.type === "workflow_completed" ||
          event.type === "workflow_failed");
      return {
        events: merged.events,
        snapshot: snapshotWithEventSummary(state.snapshot, event),
        approvalLock: clearApproval ? null : state.approvalLock,
        approvalSubmitting: clearApproval
          ? false
          : state.approvalSubmitting,
        error:
          conflictError(merged.conflict) ??
          (clearsApprovalError ? null : state.error),
      };
    }),
  appendEvents: (events) =>
    set((state) => {
      const merged = mergeWorkflowEvents(state.events, events);
      return {
        events: merged.events,
        error: conflictError(merged.conflict) ?? state.error,
      };
    }),
  restartEventStream: () =>
    set((state) => ({ eventStreamVersion: state.eventStreamVersion + 1 })),
  setConnectionStatus: (connectionStatus) => set({ connectionStatus }),
  setLoading: (isLoading) => set({ isLoading }),
  setApprovalSubmitting: (approvalSubmitting) =>
    set({ approvalSubmitting }),
  acquireApprovalLock: (approvalLock) => {
    if (get().approvalLock !== null) return false;
    set({ approvalLock, approvalSubmitting: true });
    return true;
  },
  clearApprovalLock: () =>
    set({ approvalLock: null, approvalSubmitting: false }),
  setError: (error) => set({ error }),
  reset: (threadId, branchId = "original") =>
    set({
      ...initialState,
      threadId: threadId ?? null,
      branchId,
    }),
}));
