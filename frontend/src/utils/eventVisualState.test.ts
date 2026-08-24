import { describe, expect, it } from "vitest";

import type { WorkflowEvent } from "../api/types";
import { workflowEvent, workflowSnapshot } from "../test/fixtures";
import {
  buildEventVisualStateMap,
  deriveEventVisualState,
  getApprovalPairingKey,
  getStagePairingKey,
  getSupervisorPairingKey,
  getTestPairingKey,
  getToolPairingKey,
  getWorkflowPairingKey,
} from "./eventVisualState";

function event(
  sequence: number,
  type: WorkflowEvent["type"],
  data: Record<string, unknown> = {},
  stage = "testing",
): WorkflowEvent {
  return workflowEvent(sequence, {
    type,
    stage,
    status:
      type.endsWith("_completed") || type === "approval_granted"
        ? "completed"
        : type.endsWith("_failed") || type === "approval_rejected"
          ? "failed"
          : type === "approval_required"
            ? "waiting"
            : "running",
    data: { branch_id: "original", lineage: "original", ...data },
  });
}

function visual(
  target: WorkflowEvent,
  events: WorkflowEvent[],
  terminalStatus = "running",
) {
  return deriveEventVisualState(
    target,
    events,
    workflowSnapshot({ terminal_status: terminalStatus }),
  );
}

describe("event visual state", () => {
  it("keeps workflow_started active until a terminal event arrives", () => {
    const started = event(1, "workflow_started");
    expect(visual(started, [started]).icon).toBe("spinner");
  });

  it("resolves workflow_started with workflow_completed", () => {
    const started = event(1, "workflow_started");
    const completed = event(2, "workflow_completed");
    expect(visual(started, [started, completed]).icon).toBe("started");
  });

  it("uses a static workflow_started icon for a completed snapshot", () => {
    const started = event(1, "workflow_started");
    expect(visual(started, [started], "completed")).toEqual({
      isActive: false,
      isResolved: true,
      icon: "started",
    });
  });

  it("matches a generic stage by node and namespace", () => {
    const started = event(
      1,
      "stage_started",
      { node: "planning", namespace: ["parent", "planning"] },
      "planning",
    );
    const completed = event(
      2,
      "stage_completed",
      { node: "planning", namespace: ["parent", "planning"] },
      "planning",
    );
    expect(getStagePairingKey(started)).toBe(getStagePairingKey(completed));
    expect(visual(started, [started, completed]).isResolved).toBe(true);
  });

  it("does not close a stage with another node", () => {
    const started = event(
      1,
      "stage_started",
      { node: "planning", namespace: ["parent", "planning"] },
      "planning",
    );
    const other = event(
      2,
      "stage_completed",
      { node: "supervisor", namespace: ["parent", "supervisor"] },
      "supervisor",
    );
    expect(getStagePairingKey(started)).not.toBe(getStagePairingKey(other));
    expect(visual(started, [started, other]).icon).toBe("spinner");
  });

  it("matches supervisor decisions by handoff sequence", () => {
    const started = event(1, "supervisor_decision_started", {
      handoff_sequence: 4,
    });
    const completed = event(2, "supervisor_decision_completed", {
      handoff_sequence: 4,
    });
    expect(getSupervisorPairingKey(started)).toBe(
      getSupervisorPairingKey(completed),
    );
    expect(visual(started, [started, completed]).icon).toBe("started");
  });

  it("matches tool events by server, tool, stage and attempt", () => {
    const started = event(1, "tool_started", {
      server: "testing",
      tool: "run_tests",
      attempt: 0,
    });
    const completed = event(2, "tool_completed", {
      server: "testing",
      tool: "run_tests",
      attempt: 0,
    });
    expect(getToolPairingKey(started)).toBe(getToolPairingKey(completed));
    expect(visual(started, [started, completed]).icon).toBe("started");
  });

  it("does not resolve a tool with another tool", () => {
    const started = event(1, "tool_started", {
      server: "testing",
      tool: "run_tests",
      attempt: 0,
    });
    const completed = event(2, "tool_completed", {
      server: "testing",
      tool: "prepare_environment",
      attempt: 0,
    });
    expect(getToolPairingKey(started)).not.toBe(getToolPairingKey(completed));
    expect(visual(started, [started, completed]).icon).toBe("spinner");
  });

  it("keeps tool attempts distinct and replaces the older attempt", () => {
    const first = event(1, "tool_started", {
      server: "testing",
      tool: "run_tests",
      attempt: 0,
    });
    const retry = event(2, "tool_started", {
      server: "testing",
      tool: "run_tests",
      attempt: 1,
    });
    const map = buildEventVisualStateMap(
      [first, retry],
      workflowSnapshot({ terminal_status: "running" }),
    );
    expect(getToolPairingKey(first)).not.toBe(getToolPairingKey(retry));
    expect(map.get(first.event_id)?.icon).toBe("started");
    expect(map.get(retry.event_id)?.icon).toBe("spinner");
  });

  it("matches test runs by framework and attempt", () => {
    const started = event(1, "test_run_started", {
      framework: "pytest",
      attempt: 1,
    });
    const completed = event(2, "test_run_completed", {
      framework: "pytest",
      attempt: 1,
    });
    expect(getTestPairingKey(started)).toBe(getTestPairingKey(completed));
    expect(visual(started, [started, completed]).isResolved).toBe(true);
  });

  it.each([
    ["planning_started", "planning_completed"],
    ["implementation_started", "implementation_completed"],
    ["testing_started", "testing_completed"],
    ["workspace_inspection_started", "workspace_inspection_completed"],
    ["repair_started", "repair_completed"],
  ] as const)("resolves %s with %s", (startType, completedType) => {
    const started = event(1, startType);
    const completed = event(2, completedType);
    expect(visual(started, [started, completed]).icon).toBe("started");
  });

  it("shows approval_required as waiting rather than spinning", () => {
    const approval = event(1, "approval_required", {
      operation: "run_tests",
      tool_name: "testing__run_tests",
    });
    expect(
      deriveEventVisualState(
        approval,
        [approval],
        workflowSnapshot({
          interrupted: true,
          pending_operation: "run_tests",
          pending_tool: "testing__run_tests",
        }),
      ),
    ).toEqual({
      isActive: true,
      isResolved: false,
      icon: "waiting",
    });
  });

  it("suspends prior operations visually when approval is required", () => {
    const workflow = event(1, "workflow_started");
    const implementation = event(
      2,
      "implementation_started",
      {},
      "implementation",
    );
    const approval = event(3, "approval_required", {
      operation: "create_project",
      tool_name: "filesystem__create_project_structure",
    });
    const snapshot = workflowSnapshot({
      interrupted: true,
      pending_operation: "create_project",
      pending_tool: "filesystem__create_project_structure",
    });
    const map = buildEventVisualStateMap(
      [workflow, implementation, approval],
      snapshot,
    );
    expect(map.get(workflow.event_id)?.icon).toBe("spinner");
    expect(map.get(implementation.event_id)?.icon).toBe("started");
    expect(map.get(approval.event_id)?.icon).toBe("waiting");
  });

  it("resolves approval_required when pending_operation changes", () => {
    const approval = event(1, "approval_required", {
      operation: "create_project",
      tool_name: "filesystem__create_project_structure",
    });
    const snapshot = workflowSnapshot({
      interrupted: true,
      pending_operation: "prepare_environment",
      pending_tool: "testing__prepare_test_environment",
    });
    expect(
      deriveEventVisualState(approval, [approval], snapshot).isResolved,
    ).toBe(true);
  });

  it("builds approval keys from operation, tool, attempt and scope", () => {
    const approval = event(
      1,
      "approval_required",
      {
        operation: "create_project",
        tool_name: "filesystem__create_project_structure",
        attempt: 2,
        project_name: "health-api",
        checkpoint_id: "checkpoint-1",
      },
      "create_project",
    );
    expect(getApprovalPairingKey(approval)).toContain(
      "create_project:filesystem__create_project_structure:2",
    );
    expect(getApprovalPairingKey(approval)).toContain(
      "health-api:checkpoint-1",
    );
  });

  it("resolves approval_required with a matching approval_granted", () => {
    const required = event(1, "approval_required", {
      operation: "run_tests",
      tool_name: "testing__run_tests",
      attempt: 0,
    });
    const granted = event(2, "approval_granted", {
      operation: "run_tests",
      tool_name: "testing__run_tests",
      attempt: 0,
    });
    expect(visual(required, [required, granted])).toEqual({
      isActive: false,
      isResolved: true,
      icon: "resolved",
      resolvedByEventId: granted.event_id,
    });
  });

  it("matches a grant when checkpoint metadata advanced after interrupt", () => {
    const required = event(1, "approval_required", {
      operation: "run_tests",
      tool_name: "testing__run_tests",
      attempt: 0,
    });
    const granted = event(2, "approval_granted", {
      operation: "run_tests",
      tool_name: "testing__run_tests",
      attempt: 0,
      checkpoint_id: "checkpoint-after-resume",
    });
    expect(visual(required, [required, granted]).resolvedByEventId).toBe(
      granted.event_id,
    );
  });

  it("marks a rejected approval with rejected visual state", () => {
    const required = event(1, "approval_required", {
      operation: "create_project",
      tool_name: "filesystem__create_project_structure",
      attempt: 0,
    });
    const rejected = event(2, "approval_rejected", {
      operation: "create_project",
      tool_name: "filesystem__create_project_structure",
      attempt: 0,
    });
    expect(visual(required, [required, rejected])).toEqual({
      isActive: false,
      isResolved: true,
      icon: "rejected",
      resolvedByEventId: rejected.event_id,
    });
  });

  it.each([
    [
      { operation: "create_project", tool_name: "filesystem__create" },
      { operation: "prepare_environment", tool_name: "testing__prepare" },
    ],
    [
      { operation: "run_tests", tool_name: "testing__run_tests", attempt: 0 },
      { operation: "run_tests", tool_name: "testing__run_tests", attempt: 1 },
    ],
    [
      { operation: "run_tests", tool_name: "testing__run_tests" },
      { operation: "run_tests", tool_name: "testing__other_tool" },
    ],
  ])("does not mix incompatible approval metadata", (requiredData, grantData) => {
    const required = event(1, "approval_required", requiredData);
    const granted = event(2, "approval_granted", grantData);
    const state = visual(required, [required, granted]);
    expect(state.resolvedByEventId).toBeUndefined();
  });

  it("does not mix approvals from different branches", () => {
    const required = event(1, "approval_required", {
      operation: "run_tests",
      tool_name: "testing__run_tests",
    });
    const granted = event(2, "approval_granted", {
      branch_id: "fork-1",
      lineage: "fork",
      operation: "run_tests",
      tool_name: "testing__run_tests",
    });
    expect(visual(required, [required, granted]).resolvedByEventId).toBeUndefined();
  });

  it("keeps only the latest approval matching the snapshot active", () => {
    const old = event(1, "approval_required", {
      operation: "run_tests",
      tool_name: "testing__run_tests",
    });
    const current = event(2, "approval_required", {
      operation: "run_tests",
      tool_name: "testing__run_tests",
    });
    const snapshot = workflowSnapshot({
      interrupted: true,
      pending_operation: "run_tests",
      pending_tool: "testing__run_tests",
    });
    const map = buildEventVisualStateMap([old, current], snapshot);
    expect(map.get(old.event_id)?.icon).toBe("resolved");
    expect(map.get(current.event_id)?.icon).toBe("waiting");
    expect(
      [...map.values()].filter((state) => state.icon === "waiting"),
    ).toHaveLength(1);
  });

  it.each(["completed", "failed"] as const)(
    "never revives incomplete approval history for terminal %s",
    (terminalStatus) => {
      const required = event(1, "approval_required", {
        operation: "run_tests",
        tool_name: "testing__run_tests",
      });
      const state = visual(required, [required], terminalStatus);
      expect(state).toEqual({
        isActive: false,
        isResolved: true,
        icon: "resolved",
      });
    },
  );

  it("uses interrupted snapshot as fallback for incomplete active history", () => {
    const required = event(1, "approval_required", {
      operation: "prepare_environment",
      tool_name: "testing__prepare_test_environment",
    });
    const snapshot = workflowSnapshot({
      interrupted: true,
      pending_operation: "prepare_environment",
      pending_tool: "testing__prepare_test_environment",
    });
    expect(
      deriveEventVisualState(required, [required], snapshot).icon,
    ).toBe("waiting");
  });

  it("deactivates every approval when snapshot is not interrupted", () => {
    const approvals = [
      event(1, "approval_required", {
        operation: "create_project",
        tool_name: "filesystem__create",
      }),
      event(2, "approval_required", {
        operation: "run_tests",
        tool_name: "testing__run_tests",
      }),
    ];
    const map = buildEventVisualStateMap(
      approvals,
      workflowSnapshot({
        interrupted: false,
        pending_operation: null,
        pending_tool: null,
      }),
    );
    expect(
      [...map.values()].filter((state) => state.icon === "waiting"),
    ).toHaveLength(0);
  });

  it("keeps historical approvals resolved and only current one waiting", () => {
    const create = event(1, "approval_required", {
      operation: "create_project",
      tool_name: "filesystem__create",
      attempt: 0,
    });
    const createGranted = event(2, "approval_granted", {
      operation: "create_project",
      tool_name: "filesystem__create",
      attempt: 0,
    });
    const environment = event(3, "approval_required", {
      operation: "prepare_environment",
      tool_name: "testing__prepare",
      attempt: 0,
    });
    const environmentGranted = event(4, "approval_granted", {
      operation: "prepare_environment",
      tool_name: "testing__prepare",
      attempt: 0,
    });
    const tests = event(5, "approval_required", {
      operation: "run_tests",
      tool_name: "testing__run_tests",
      attempt: 0,
    });
    const map = buildEventVisualStateMap(
      [create, createGranted, environment, environmentGranted, tests],
      workflowSnapshot({
        interrupted: true,
        pending_operation: "run_tests",
        pending_tool: "testing__run_tests",
      }),
    );
    expect(map.get(create.event_id)?.icon).toBe("resolved");
    expect(map.get(environment.event_id)?.icon).toBe("resolved");
    expect(map.get(tests.event_id)?.icon).toBe("waiting");
    expect(
      [...map.values()].filter((state) => state.icon === "waiting"),
    ).toHaveLength(1);
  });

  it("handles approval metadata missing without breaking the map", () => {
    const required = workflowEvent(1, {
      type: "approval_required",
      status: "waiting",
      data: {},
    });
    expect(() =>
      buildEventVisualStateMap([required], workflowSnapshot()),
    ).not.toThrow();
    expect(getApprovalPairingKey(required)).toContain("_");
  });

  it.each(["completed", "failed"] as const)(
    "removes every spinner for a %s workflow",
    (terminalStatus) => {
      const events = [
        event(1, "workflow_started"),
        event(2, "stage_started", { node: "planning" }, "planning"),
        event(3, "tool_started", {
          server: "testing",
          tool: "run_tests",
          attempt: 0,
        }),
      ];
      const map = buildEventVisualStateMap(
        events,
        workflowSnapshot({ terminal_status: terminalStatus }),
      );
      expect(
        [...map.values()].filter((state) => state.icon === "spinner"),
      ).toHaveLength(0);
    },
  );

  it("uses safe fallback keys when metadata is missing", () => {
    const started = workflowEvent(1, {
      type: "tool_started",
      status: "running",
      data: {},
    });
    expect(() =>
      buildEventVisualStateMap([started], workflowSnapshot()),
    ).not.toThrow();
    expect(getToolPairingKey(started)).toContain("_");
  });

  it("does not break on malformed metadata objects", () => {
    const malformed = event(1, "stage_started", {
      node: { unexpected: true },
      namespace: [null, { invalid: true }],
    });
    expect(
      buildEventVisualStateMap([malformed], workflowSnapshot()).size,
    ).toBe(1);
  });

  it("builds one visual state per event in a single indexed pass", () => {
    const events = Array.from({ length: 1_000 }, (_, index) =>
      event(index + 1, "tool_started", {
        server: "testing",
        tool: `tool-${index}`,
        attempt: 0,
      }),
    );
    expect(buildEventVisualStateMap(events, workflowSnapshot()).size).toBe(
      events.length,
    );
  });

  it("keeps only the latest unresolved stage in the same namespace active", () => {
    const first = event(
      1,
      "stage_started",
      { node: "planning", namespace: ["parent", "planning"] },
      "planning",
    );
    const latest = event(
      2,
      "stage_started",
      { node: "supervisor", namespace: ["parent", "supervisor"] },
      "supervisor",
    );
    const map = buildEventVisualStateMap(
      [first, latest],
      workflowSnapshot(),
    );
    expect(map.get(first.event_id)?.icon).toBe("started");
    expect(map.get(latest.event_id)?.icon).toBe("spinner");
  });

  it("allows two unresolved tools to remain active in parallel", () => {
    const first = event(1, "tool_started", {
      server: "testing",
      tool: "one",
      attempt: 0,
    });
    const second = event(2, "tool_started", {
      server: "testing",
      tool: "two",
      attempt: 0,
    });
    const map = buildEventVisualStateMap(
      [first, second],
      workflowSnapshot(),
    );
    expect(
      [...map.values()].filter((state) => state.icon === "spinner"),
    ).toHaveLength(2);
  });

  it("resolving one parallel tool leaves the other active", () => {
    const first = event(1, "tool_started", {
      server: "testing",
      tool: "one",
      attempt: 0,
    });
    const second = event(2, "tool_started", {
      server: "testing",
      tool: "two",
      attempt: 0,
    });
    const completed = event(3, "tool_completed", {
      server: "testing",
      tool: "one",
      attempt: 0,
    });
    const map = buildEventVisualStateMap(
      [first, second, completed],
      workflowSnapshot(),
    );
    expect(map.get(first.event_id)?.icon).toBe("started");
    expect(map.get(second.event_id)?.icon).toBe("spinner");
  });

  it("keeps workflow pairing scoped to branch and lineage", () => {
    const original = event(1, "workflow_started");
    const fork = event(2, "workflow_completed", {
      branch_id: "fork-1",
      lineage: "fork",
    });
    expect(getWorkflowPairingKey(original)).not.toBe(
      getWorkflowPairingKey(fork),
    );
    expect(visual(original, [original, fork]).icon).toBe("spinner");
  });
});
