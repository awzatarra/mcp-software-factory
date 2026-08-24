import type {
  EventStatus,
  WorkflowEvent,
  WorkflowSnapshot,
} from "../api/types";

export type StageKey =
  | "planning"
  | "workspace"
  | "implementation"
  | "testing"
  | "finalize";

export interface WorkflowStage {
  key: StageKey;
  label: string;
  status: EventStatus;
}

export type StageStatus = EventStatus;

export interface StageState {
  planning: StageStatus;
  workspace: StageStatus;
  implementation: StageStatus;
  testing: StageStatus;
  finalize: StageStatus;
}

const STAGES: Array<{ key: StageKey; label: string }> = [
  { key: "planning", label: "Planning" },
  { key: "workspace", label: "Workspace" },
  { key: "implementation", label: "Implementation" },
  { key: "testing", label: "Testing" },
  { key: "finalize", label: "Finalize" },
];

function stageForEvent(event: WorkflowEvent): StageKey | null {
  const value = `${event.stage ?? ""} ${event.type}`;
  if (value.includes("planning")) return "planning";
  if (value.includes("workspace") || value.includes("inspect_workspace")) {
    return "workspace";
  }
  if (value.includes("implementation") || value.includes("create_project")) {
    return "implementation";
  }
  if (
    value.includes("testing") ||
    value.includes("test_") ||
    value.includes("repair")
  ) {
    return "testing";
  }
  if (value.includes("finalize") || value.includes("workflow_completed")) {
    return "finalize";
  }
  return null;
}

export function deriveStageProgress(
  events: WorkflowEvent[],
  snapshot: WorkflowSnapshot | null,
): WorkflowStage[] {
  const state = deriveStageState(snapshot, events);
  return STAGES.map((stage) => ({
    ...stage,
    status: state[stage.key],
  }));
}

const priority: Record<StageStatus, number> = {
  pending: 0,
  running: 1,
  waiting: 2,
  completed: 3,
  failed: 4,
};

function boolField(group: Record<string, unknown>, key: string): boolean {
  return group[key] === true;
}

function eventStatus(event: WorkflowEvent): StageStatus {
  if (
    event.status === "failed" ||
    event.type.endsWith("_failed") ||
    event.type === "workflow_failed"
  ) {
    return "failed";
  }
  if (
    event.status === "completed" ||
    event.type.endsWith("_completed")
  ) {
    return "completed";
  }
  return event.status === "waiting" ? "waiting" : "running";
}

export function deriveStageState(
  snapshot: WorkflowSnapshot | null,
  events: WorkflowEvent[],
): StageState {
  if (snapshot?.terminal_status === "completed") {
    return {
      planning: "completed",
      workspace: "completed",
      implementation: "completed",
      testing: "completed",
      finalize: "completed",
    };
  }

  const statuses: StageState = {
    planning: "pending",
    workspace: "pending",
    implementation: "pending",
    testing: "pending",
    finalize: "pending",
  };
  const snapshotCompleted = new Set<StageKey>();

  if (snapshot) {
    if (boolField(snapshot.planning, "valid")) {
      statuses.planning = "completed";
      snapshotCompleted.add("planning");
    }
    if (
      boolField(snapshot.implementation, "environment_prepared") &&
      boolField(snapshot.implementation, "dependencies_installed")
    ) {
      statuses.implementation = "completed";
      snapshotCompleted.add("implementation");
    }
    if (
      boolField(snapshot.testing, "executed") &&
      boolField(snapshot.testing, "passed")
    ) {
      statuses.testing = "completed";
      snapshotCompleted.add("testing");
    }
  }

  for (const event of events) {
    const stage = stageForEvent(event);
    if (!stage || snapshotCompleted.has(stage)) continue;
    const candidate = eventStatus(event);
    if (priority[candidate] > priority[statuses[stage]]) {
      statuses[stage] = candidate;
    }
  }

  if (
    statuses.workspace !== "failed" &&
    (events.some(
      (event) => event.type === "workspace_inspection_completed",
    ) ||
      statuses.implementation === "completed" ||
      statuses.testing === "completed")
  ) {
    statuses.workspace = "completed";
  }

  if (snapshot?.interrupted) {
    const operation = snapshot.pending_operation ?? "";
    const waitingStage: StageKey = operation.includes("test")
      ? "testing"
      : operation.includes("environment") || operation.includes("create")
        ? "implementation"
        : "planning";
    if (statuses[waitingStage] !== "completed") {
      statuses[waitingStage] = "waiting";
    }
  }
  if (
    snapshot &&
    !["pending", "running", "completed"].includes(snapshot.terminal_status)
  ) {
    statuses.finalize = "failed";
  }
  return statuses;
}
