import { describe, expect, it } from "vitest";

import { workflowEvent, workflowSnapshot } from "../test/fixtures";
import {
  classifyWorkflowEvent,
  shouldShowInSummary,
} from "./eventClassification";
import { mergeWorkflowEvents } from "./eventDeduplication";
import { formatWorkflowEvent } from "./eventFormatting";
import { deriveStageProgress, deriveStageState } from "./stageDerivation";

describe("event utilities", () => {
  it("classifies the planning subgraph milestone as functional", () => {
    const event = workflowEvent(10, {
      source: "planning_subgraph",
      type: "planning_completed",
    });
    expect(classifyWorkflowEvent(event)).toBe("functional");
    expect(shouldShowInSummary(event)).toBe(true);
  });

  it("classifies the LangGraph planning update as technical", () => {
    const event = workflowEvent(11, {
      source: "langgraph.update",
      type: "planning_completed",
    });
    expect(classifyWorkflowEvent(event)).toBe("technical");
    expect(shouldShowInSummary(event)).toBe(false);
  });

  it("does not hide functionally distinct LangGraph events", () => {
    const event = workflowEvent(12, {
      source: "langgraph.update",
      type: "implementation_completed",
    });
    expect(shouldShowInSummary(event)).toBe(true);
  });

  it("orders events by sequence", () => {
    const result = mergeWorkflowEvents([], [workflowEvent(3), workflowEvent(1)]);
    expect(result.events.map((event) => event.sequence)).toEqual([1, 3]);
  });

  it("deduplicates by event id", () => {
    const event = workflowEvent(1);
    expect(mergeWorkflowEvents([event], [event]).events).toHaveLength(1);
  });

  it("rejects incompatible duplicate sequences", () => {
    const conflict = workflowEvent(1, { event_id: "different" });
    const result = mergeWorkflowEvents([workflowEvent(1)], [conflict]);
    expect(result.events).toHaveLength(1);
    expect(result.conflict).toContain("secuencia 1");
  });

  it("deduplicates overlapping history and live events", () => {
    const history = [workflowEvent(1), workflowEvent(2)];
    const live = [workflowEvent(2), workflowEvent(3)];
    expect(mergeWorkflowEvents(history, live).events).toHaveLength(3);
  });

  it("formats approval operation", () => {
    const formatted = formatWorkflowEvent(
      workflowEvent(4, {
        type: "approval_required",
        status: "waiting",
        data: { operation: "create_project" },
      }),
    );
    expect(formatted.title).toBe("Aprobación requerida: create project");
  });

  it("formats tool names", () => {
    const formatted = formatWorkflowEvent(
      workflowEvent(5, {
        type: "tool_started",
        data: { tool: "list_files" },
      }),
    );
    expect(formatted.title).toBe("Tool iniciada: list_files");
  });

  it("derives completed planning and running implementation", () => {
    const stages = deriveStageProgress(
      [
        workflowEvent(1, {
          type: "planning_completed",
          status: "completed",
        }),
        workflowEvent(2, {
          type: "implementation_started",
          stage: "implementation",
        }),
      ],
      workflowSnapshot(),
    );
    expect(stages.find((stage) => stage.key === "planning")?.status).toBe(
      "completed",
    );
    expect(stages.find((stage) => stage.key === "implementation")?.status).toBe(
      "running",
    );
  });

  it("marks approval stage as waiting", () => {
    const stages = deriveStageProgress(
      [],
      workflowSnapshot({
        interrupted: true,
        pending_operation: "run_tests",
      }),
    );
    expect(stages.find((stage) => stage.key === "testing")?.status).toBe(
      "waiting",
    );
  });

  it("uses a completed snapshot as authority for every normal stage", () => {
    const state = deriveStageState(
      workflowSnapshot({
        terminal_status: "completed",
        planning: { valid: true },
        implementation: {
          project_created: true,
          environment_prepared: true,
          dependencies_installed: true,
        },
        testing: { executed: true, passed: true },
        supervisor: { supervisor_decision: "finalize" },
      }),
      [
        workflowEvent(1, { type: "planning_started" }),
        workflowEvent(2, {
          type: "test_run_started",
          stage: "testing",
        }),
      ],
    );
    expect(Object.values(state)).toEqual([
      "completed",
      "completed",
      "completed",
      "completed",
      "completed",
    ]);
  });

  it("does not let old running events downgrade snapshot facts", () => {
    const state = deriveStageState(
      workflowSnapshot({
        planning: { valid: true },
        implementation: {
          environment_prepared: true,
          dependencies_installed: true,
        },
        testing: { executed: true, passed: true },
      }),
      [
        workflowEvent(1, { type: "planning_started" }),
        workflowEvent(2, {
          type: "implementation_started",
          stage: "implementation",
        }),
        workflowEvent(3, {
          type: "test_run_started",
          stage: "testing",
        }),
      ],
    );
    expect(state.planning).toBe("completed");
    expect(state.implementation).toBe("completed");
    expect(state.testing).toBe("completed");
    expect(state.workspace).toBe("completed");
  });

  it("does not mark every stage completed for a cancelled workflow", () => {
    const state = deriveStageState(
      workflowSnapshot({ terminal_status: "user_cancelled" }),
      [workflowEvent(1, { type: "planning_completed", status: "completed" })],
    );
    expect(state.planning).toBe("completed");
    expect(state.implementation).toBe("pending");
    expect(state.testing).toBe("pending");
    expect(state.finalize).toBe("failed");
  });
});
