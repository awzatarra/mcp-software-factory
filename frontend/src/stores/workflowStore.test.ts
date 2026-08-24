import { beforeEach, describe, expect, it } from "vitest";

import { workflowEvent, workflowSnapshot } from "../test/fixtures";
import { useWorkflowStore } from "./workflowStore";

describe("workflow store", () => {
  beforeEach(() => useWorkflowStore.getState().reset());

  it("stores a snapshot", () => {
    useWorkflowStore.getState().setSnapshot(workflowSnapshot());
    expect(useWorkflowStore.getState().snapshot?.project_name).toBe(
      "health-api",
    );
  });

  it("replaces history in sequence order", () => {
    useWorkflowStore
      .getState()
      .replaceHistory([workflowEvent(2), workflowEvent(1)]);
    expect(useWorkflowStore.getState().events.map((event) => event.sequence)).toEqual([
      1, 2,
    ]);
  });

  it("does not duplicate history and live events", () => {
    useWorkflowStore.getState().replaceHistory([workflowEvent(1)]);
    useWorkflowStore.getState().appendEvent(workflowEvent(1));
    expect(useWorkflowStore.getState().events).toHaveLength(1);
  });

  it("preserves live events received before history finishes loading", () => {
    useWorkflowStore.getState().appendEvent(workflowEvent(2));
    useWorkflowStore.getState().replaceHistory([workflowEvent(1)]);
    expect(
      useWorkflowStore.getState().events.map((event) => event.sequence),
    ).toEqual([1, 2]);
  });

  it("reports incompatible sequences", () => {
    useWorkflowStore.getState().appendEvent(workflowEvent(1));
    useWorkflowStore
      .getState()
      .appendEvent(workflowEvent(1, { event_id: "other" }));
    expect(useWorkflowStore.getState().error?.title).toBe(
      "Conflicto de eventos",
    );
  });

  it("resets state for another thread", () => {
    useWorkflowStore.getState().appendEvent(workflowEvent(1));
    useWorkflowStore.getState().reset("thread-2");
    expect(useWorkflowStore.getState().events).toEqual([]);
    expect(useWorkflowStore.getState().threadId).toBe("thread-2");
  });

  it("increments the stream version for a durable resume", () => {
    useWorkflowStore.getState().restartEventStream();
    expect(useWorkflowStore.getState().eventStreamVersion).toBe(1);
  });

  it("acquires an approval lock synchronously only once", () => {
    const lock = {
      threadId: "thread-1",
      operation: "create_project",
      toolName: "filesystem__create_project_structure",
      startedAt: 1,
    };
    expect(useWorkflowStore.getState().acquireApprovalLock(lock)).toBe(true);
    expect(useWorkflowStore.getState().acquireApprovalLock(lock)).toBe(false);
    expect(useWorkflowStore.getState().approvalSubmitting).toBe(true);
  });

  it("keeps the approval lock while the same operation remains pending", () => {
    const lock = {
      threadId: "thread-1",
      operation: "create_project",
      toolName: "filesystem__create_project_structure",
      startedAt: 1,
    };
    useWorkflowStore.getState().acquireApprovalLock(lock);
    useWorkflowStore.getState().setSnapshot(
      workflowSnapshot({
        interrupted: true,
        pending_operation: lock.operation,
        pending_tool: lock.toolName,
      }),
    );
    expect(useWorkflowStore.getState().approvalLock).toEqual(lock);
  });

  it("releases an approval lock when the pending operation changes", () => {
    useWorkflowStore.getState().acquireApprovalLock({
      threadId: "thread-1",
      operation: "create_project",
      toolName: "filesystem__create_project_structure",
      startedAt: 1,
    });
    useWorkflowStore.getState().setSnapshot(
      workflowSnapshot({
        interrupted: true,
        pending_operation: "prepare_environment",
        pending_tool: "testing__prepare_test_environment",
      }),
    );
    expect(useWorkflowStore.getState().approvalLock).toBeNull();
    expect(useWorkflowStore.getState().approvalSubmitting).toBe(false);
  });

  it("releases the matching lock on approval_granted", () => {
    useWorkflowStore.getState().setSnapshot(
      workflowSnapshot({
        interrupted: true,
        pending_operation: "run_tests",
        pending_tool: "testing__run_tests",
      }),
    );
    useWorkflowStore.getState().acquireApprovalLock({
      threadId: "thread-1",
      operation: "run_tests",
      toolName: "testing__run_tests",
      startedAt: 1,
    });
    useWorkflowStore.getState().appendEvent(
      workflowEvent(4, {
        type: "approval_granted",
        status: "completed",
        data: { operation: "run_tests", tool_name: "testing__run_tests" },
      }),
    );
    expect(useWorkflowStore.getState().approvalLock).toBeNull();
    expect(useWorkflowStore.getState().snapshot?.interrupted).toBe(false);
    expect(useWorkflowStore.getState().snapshot?.pending_operation).toBeNull();
  });

  it("projects a test summary immediately from test_run_completed", () => {
    useWorkflowStore.getState().setSnapshot(workflowSnapshot());
    useWorkflowStore.getState().appendEvent(
      workflowEvent(5, {
        type: "test_run_completed",
        status: "completed",
        data: { passed: true, summary: "1 passed, 2 warnings" },
      }),
    );
    expect(
      useWorkflowStore.getState().snapshot?.testing.final_test_result_summary,
    ).toBe("1 passed, 2 warnings");
  });

  it("clears approval errors when the operation changes or completes", () => {
    useWorkflowStore.getState().setSnapshot(
      workflowSnapshot({
        interrupted: true,
        pending_operation: "create_project",
      }),
    );
    useWorkflowStore.getState().setError({
      title: "Operación ya resuelta",
      message: "stale",
      retryable: false,
      scope: "approval",
    });
    useWorkflowStore.getState().setSnapshot(
      workflowSnapshot({
        interrupted: true,
        pending_operation: "prepare_environment",
      }),
    );
    expect(useWorkflowStore.getState().error).toBeNull();
    useWorkflowStore.getState().setError({
      title: "Operación ya resuelta",
      message: "stale",
      retryable: false,
      scope: "approval",
    });
    useWorkflowStore.getState().appendEvent(
      workflowEvent(9, {
        type: "workflow_completed",
        status: "completed",
      }),
    );
    expect(useWorkflowStore.getState().error).toBeNull();
  });
});
