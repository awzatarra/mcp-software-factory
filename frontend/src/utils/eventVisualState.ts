import type { WorkflowEvent, WorkflowSnapshot } from "../api/types";

export type EventVisualIcon =
  | "spinner"
  | "started"
  | "completed"
  | "failed"
  | "waiting"
  | "resolved"
  | "rejected"
  | "info";

export interface EventVisualState {
  isActive: boolean;
  isResolved: boolean;
  icon: EventVisualIcon;
  resolvedByEventId?: string;
}

export type EventVisualStateMap = Map<string, EventVisualState>;

type EventFamily =
  | "workflow"
  | "stage"
  | "supervisor"
  | "tool"
  | "test"
  | "planning"
  | "implementation"
  | "testing"
  | "workspace"
  | "repair";

interface PairDescriptor {
  family: EventFamily;
  key: string;
  activityKey: string;
  phase: "start" | "completed" | "failed";
}

const PAIRS: Partial<
  Record<
    WorkflowEvent["type"],
    { family: EventFamily; phase: PairDescriptor["phase"] }
  >
> = {
  workflow_started: { family: "workflow", phase: "start" },
  workflow_completed: { family: "workflow", phase: "completed" },
  workflow_failed: { family: "workflow", phase: "failed" },
  stage_started: { family: "stage", phase: "start" },
  stage_completed: { family: "stage", phase: "completed" },
  stage_failed: { family: "stage", phase: "failed" },
  supervisor_decision_started: { family: "supervisor", phase: "start" },
  supervisor_decision_completed: {
    family: "supervisor",
    phase: "completed",
  },
  supervisor_loop_detected: { family: "supervisor", phase: "failed" },
  tool_started: { family: "tool", phase: "start" },
  tool_completed: { family: "tool", phase: "completed" },
  tool_failed: { family: "tool", phase: "failed" },
  test_run_started: { family: "test", phase: "start" },
  test_run_completed: { family: "test", phase: "completed" },
  test_run_failed: { family: "test", phase: "failed" },
  planning_started: { family: "planning", phase: "start" },
  planning_completed: { family: "planning", phase: "completed" },
  planning_failed: { family: "planning", phase: "failed" },
  implementation_started: { family: "implementation", phase: "start" },
  implementation_completed: {
    family: "implementation",
    phase: "completed",
  },
  implementation_failed: { family: "implementation", phase: "failed" },
  testing_started: { family: "testing", phase: "start" },
  testing_completed: { family: "testing", phase: "completed" },
  testing_failed: { family: "testing", phase: "failed" },
  workspace_inspection_started: { family: "workspace", phase: "start" },
  workspace_inspection_completed: {
    family: "workspace",
    phase: "completed",
  },
  repair_started: { family: "repair", phase: "start" },
  repair_completed: { family: "repair", phase: "completed" },
  repair_failed: { family: "repair", phase: "failed" },
};

function safePart(value: unknown): string {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value)) {
    const parts = value.map(safePart).filter((part) => part !== "_");
    return parts.length ? parts.join("/") : "_";
  }
  return "_";
}

function commonParts(event: WorkflowEvent): string[] {
  return [
    safePart(event.data.branch_id),
    safePart(event.data.lineage),
  ];
}

function branchScopeKey(event: WorkflowEvent): string {
  return commonParts(event).join(":");
}

export function getWorkflowPairingKey(event: WorkflowEvent): string {
  return [...commonParts(event), event.thread_id].join(":");
}

export function getStagePairingKey(event: WorkflowEvent): string {
  return [
    ...commonParts(event),
    safePart(event.stage),
    safePart(event.data.node),
    safePart(event.data.namespace),
  ].join(":");
}

export function getSupervisorPairingKey(event: WorkflowEvent): string {
  return [
    ...commonParts(event),
    safePart(event.data.handoff_sequence),
  ].join(":");
}

export function getToolPairingKey(event: WorkflowEvent): string {
  return [
    ...commonParts(event),
    safePart(event.stage),
    safePart(event.data.server),
    safePart(event.data.tool ?? event.data.tool_name),
    safePart(event.data.attempt),
  ].join(":");
}

export function getTestPairingKey(event: WorkflowEvent): string {
  return [
    ...commonParts(event),
    safePart(event.data.framework),
    safePart(event.data.attempt),
  ].join(":");
}

function approvalKeyParts(
  event: WorkflowEvent,
  includeCheckpoint: boolean,
): string[] {
  const parts = [
    ...commonParts(event),
    safePart(event.data.operation),
    safePart(event.data.tool_name ?? event.data.tool),
    safePart(event.data.attempt),
    safePart(event.stage),
    safePart(event.data.project_name),
  ];
  if (includeCheckpoint) parts.push(safePart(event.data.checkpoint_id));
  return parts;
}

export function getApprovalPairingKey(event: WorkflowEvent): string {
  return approvalKeyParts(event, true).join(":");
}

function getApprovalFallbackKey(event: WorkflowEvent): string {
  return approvalKeyParts(event, false).join(":");
}

function domainPairingKey(event: WorkflowEvent): string {
  return [
    ...commonParts(event),
    safePart(event.stage),
    safePart(event.data.attempt),
  ].join(":");
}

function pairingKey(family: EventFamily, event: WorkflowEvent): string {
  switch (family) {
    case "workflow":
      return getWorkflowPairingKey(event);
    case "stage":
      return getStagePairingKey(event);
    case "supervisor":
      return getSupervisorPairingKey(event);
    case "tool":
      return getToolPairingKey(event);
    case "test":
      return getTestPairingKey(event);
    default:
      return domainPairingKey(event);
  }
}

function activityKey(family: EventFamily, event: WorkflowEvent): string {
  const common = commonParts(event);
  if (family === "stage") {
    const namespace = Array.isArray(event.data.namespace)
      ? event.data.namespace[0]
      : event.data.namespace;
    return [family, ...common, safePart(namespace)].join(":");
  }
  if (family === "supervisor") {
    return [family, ...common].join(":");
  }
  if (family === "tool") {
    return [
      family,
      ...common,
      safePart(event.stage),
      safePart(event.data.server),
      safePart(event.data.tool ?? event.data.tool_name),
    ].join(":");
  }
  if (family === "test") {
    return [
      family,
      ...common,
      safePart(event.data.framework),
    ].join(":");
  }
  return [family, ...common, safePart(event.stage)].join(":");
}

function descriptor(event: WorkflowEvent): PairDescriptor | null {
  const definition = PAIRS[event.type];
  if (!definition) return null;
  return {
    ...definition,
    key: `${definition.family}:${pairingKey(definition.family, event)}`,
    activityKey: activityKey(definition.family, event),
  };
}

function baseVisualState(event: WorkflowEvent): EventVisualState {
  if (event.status === "failed" || event.type.endsWith("_failed")) {
    return { isActive: false, isResolved: true, icon: "failed" };
  }
  if (event.status === "completed" || event.type.endsWith("_completed")) {
    return { isActive: false, isResolved: true, icon: "completed" };
  }
  if (event.status === "waiting" || event.type === "approval_required") {
    return { isActive: false, isResolved: false, icon: "waiting" };
  }
  return { isActive: false, isResolved: false, icon: "info" };
}

function isTerminal(snapshot: WorkflowSnapshot | null): boolean {
  return Boolean(
    snapshot &&
      !["pending", "running"].includes(snapshot.terminal_status),
  );
}

function staticStartedState(resolved = true): EventVisualState {
  return { isActive: false, isResolved: resolved, icon: "started" };
}

function approvalMatchesSnapshot(
  event: WorkflowEvent,
  snapshot: WorkflowSnapshot | null,
): boolean {
  const operation = safePart(event.data.operation);
  const tool = safePart(event.data.tool_name ?? event.data.tool);
  return (
    !isTerminal(snapshot) &&
    snapshot?.interrupted === true &&
    operation === safePart(snapshot.pending_operation) &&
    tool === safePart(snapshot.pending_tool)
  );
}

export function buildEventVisualStateMap(
  events: WorkflowEvent[],
  snapshot: WorkflowSnapshot | null,
): EventVisualStateMap {
  const result: EventVisualStateMap = new Map();
  const pendingByPair = new Map<string, string>();
  const activeByScope = new Map<string, string>();
  const approvalBarrierByBranch = new Map<string, number>();
  const pendingApprovalByExactKey = new Map<string, string>();
  const pendingApprovalByFallbackKey = new Map<string, string>();
  const approvalKeysByEventId = new Map<
    string,
    { exact: string; fallback: string }
  >();
  const terminal = isTerminal(snapshot);

  for (const event of events) {
    const pair = descriptor(event);
    if (event.type === "approval_required") {
      approvalBarrierByBranch.set(branchScopeKey(event), event.sequence);
      result.set(event.event_id, {
        isActive: false,
        isResolved: false,
        icon: "resolved",
      });
      const exact = getApprovalPairingKey(event);
      const fallback = getApprovalFallbackKey(event);
      pendingApprovalByExactKey.set(exact, event.event_id);
      pendingApprovalByFallbackKey.set(fallback, event.event_id);
      approvalKeysByEventId.set(event.event_id, { exact, fallback });
      continue;
    }
    if (
      event.type === "approval_granted" ||
      event.type === "approval_rejected"
    ) {
      const exactKey = getApprovalPairingKey(event);
      const fallbackKey = getApprovalFallbackKey(event);
      const requiredId =
        pendingApprovalByExactKey.get(exactKey) ??
        pendingApprovalByFallbackKey.get(fallbackKey);
      if (requiredId) {
        result.set(requiredId, {
          isActive: false,
          isResolved: true,
          icon:
            event.type === "approval_granted" ? "resolved" : "rejected",
          resolvedByEventId: event.event_id,
        });
        const requiredKeys = approvalKeysByEventId.get(requiredId);
        if (requiredKeys) {
          pendingApprovalByExactKey.delete(requiredKeys.exact);
          pendingApprovalByFallbackKey.delete(requiredKeys.fallback);
          approvalKeysByEventId.delete(requiredId);
        }
      }
      result.set(event.event_id, baseVisualState(event));
      continue;
    }
    if (!pair) {
      result.set(event.event_id, baseVisualState(event));
      continue;
    }
    if (pair.phase === "start") {
      const replacedId = activeByScope.get(pair.activityKey);
      if (replacedId) result.set(replacedId, staticStartedState());
      const duplicateId = pendingByPair.get(pair.key);
      if (duplicateId) result.set(duplicateId, staticStartedState());
      result.set(
        event.event_id,
        terminal
          ? staticStartedState()
          : { isActive: true, isResolved: false, icon: "spinner" },
      );
      pendingByPair.set(pair.key, event.event_id);
      activeByScope.set(pair.activityKey, event.event_id);
      continue;
    }

    result.set(event.event_id, baseVisualState(event));
    const startedId = pendingByPair.get(pair.key);
    if (!startedId) continue;
    result.set(startedId, staticStartedState());
    pendingByPair.delete(pair.key);
    if (activeByScope.get(pair.activityKey) === startedId) {
      activeByScope.delete(pair.activityKey);
    }
  }

  let currentApproval: WorkflowEvent | null = null;
  for (const event of events) {
    if (event.type === "approval_required") {
      const state = result.get(event.event_id);
      if (state?.resolvedByEventId === undefined) {
        result.set(event.event_id, {
          isActive: false,
          isResolved: true,
          icon: "resolved",
        });
        if (
          approvalMatchesSnapshot(event, snapshot) &&
          (currentApproval === null ||
            event.sequence > currentApproval.sequence)
        ) {
          currentApproval = event;
        }
      }
    }
  }
  if (currentApproval) {
    result.set(currentApproval.event_id, {
      isActive: true,
      isResolved: false,
      icon: "waiting",
    });
  }

  for (const event of events) {
    const pair = descriptor(event);
    const barrier = approvalBarrierByBranch.get(branchScopeKey(event));
    if (
      pair?.phase === "start" &&
      pair.family !== "workflow" &&
      barrier !== undefined &&
      event.sequence < barrier &&
      result.get(event.event_id)?.icon === "spinner"
    ) {
      result.set(event.event_id, staticStartedState());
    }
  }

  if (terminal) {
    for (const event of events) {
      const pair = descriptor(event);
      if (pair?.phase === "start") {
        result.set(event.event_id, staticStartedState());
      }
    }
  }
  return result;
}

export function deriveEventVisualState(
  event: WorkflowEvent,
  allEvents: WorkflowEvent[],
  snapshot: WorkflowSnapshot | null,
): EventVisualState {
  return (
    buildEventVisualStateMap(allEvents, snapshot).get(event.event_id) ??
    baseVisualState(event)
  );
}
