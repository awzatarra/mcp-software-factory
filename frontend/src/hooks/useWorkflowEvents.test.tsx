import { StrictMode } from "react";
import { act, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as workflows from "../api/workflows";
import { useWorkflowStore } from "../stores/workflowStore";
import { workflowEvent, workflowSnapshot } from "../test/fixtures";
import {
  parseWorkflowEvent,
  resetEventStreamsForTests,
  useWorkflowEvents,
} from "./useWorkflowEvents";

class MockEventSource {
  static instances: MockEventSource[] = [];
  readonly url: string;
  readonly close = vi.fn();
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private listeners = new Map<string, Set<EventListener>>();

  constructor(url: string | URL) {
    this.url = String(url);
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, callback: EventListener) {
    const callbacks = this.listeners.get(type) ?? new Set<EventListener>();
    callbacks.add(callback);
    this.listeners.set(type, callbacks);
  }

  removeEventListener(type: string, callback: EventListener) {
    this.listeners.get(type)?.delete(callback);
  }

  emit(type: string, data: string) {
    const message = new MessageEvent(type, { data });
    for (const callback of this.listeners.get(type) ?? []) {
      callback(message);
    }
  }
}

function HookHarness({ enabled = true }: { enabled?: boolean }) {
  useWorkflowEvents("thread-1", enabled);
  return null;
}

describe("useWorkflowEvents", () => {
  beforeEach(() => {
    resetEventStreamsForTests();
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource);
    useWorkflowStore.getState().reset("thread-1");
  });

  it("ignores corrupt event JSON", () => {
    expect(parseWorkflowEvent("{broken")).toBeNull();
    expect(parseWorkflowEvent(JSON.stringify({ sequence: 1 }))).toBeNull();
  });

  it("connects after the last known sequence", () => {
    useWorkflowStore.getState().appendEvent(workflowEvent(7));
    render(<HookHarness />);
    expect(MockEventSource.instances[0].url).toContain("after_sequence=7");
  });

  it("reuses one EventSource under StrictMode", () => {
    render(
      <StrictMode>
        <HookHarness />
      </StrictMode>,
    );
    expect(MockEventSource.instances).toHaveLength(1);
  });

  it("reuses one EventSource for simultaneous consumers", () => {
    render(
      <>
        <HookHarness />
        <HookHarness />
      </>,
    );
    expect(MockEventSource.instances).toHaveLength(1);
  });

  it("appends a valid named event", () => {
    render(<HookHarness />);
    act(() => {
      MockEventSource.instances[0].emit(
        "planning_started",
        JSON.stringify(
          workflowEvent(2, {
            type: "planning_started",
          }),
        ),
      );
    });
    expect(useWorkflowStore.getState().events).toHaveLength(1);
  });

  it("refreshes the final snapshot before closing completed SSE", async () => {
    vi.useFakeTimers();
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(
      workflowSnapshot({ terminal_status: "completed" }),
    );
    render(<HookHarness />);
    const source = MockEventSource.instances[0];
    act(() => {
      source.emit(
        "workflow_completed",
        JSON.stringify(
          workflowEvent(9, {
            type: "workflow_completed",
            status: "completed",
          }),
        ),
      );
    });
    expect(source.close).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(100));
    expect(source.close).toHaveBeenCalled();
    expect(workflows.getWorkflow).toHaveBeenCalledTimes(1);
    expect(useWorkflowStore.getState().snapshot?.terminal_status).toBe(
      "completed",
    );
    expect(useWorkflowStore.getState().connectionStatus).toBe("completed");
    vi.useRealTimers();
  });

  it("refreshes the final snapshot before closing failed SSE", async () => {
    vi.useFakeTimers();
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(
      workflowSnapshot({ terminal_status: "infrastructure_failed" }),
    );
    render(<HookHarness />);
    const source = MockEventSource.instances[0];
    act(() => {
      source.emit(
        "workflow_failed",
        JSON.stringify(
          workflowEvent(9, {
            type: "workflow_failed",
            status: "failed",
          }),
        ),
      );
    });
    expect(source.close).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(100));
    expect(source.close).toHaveBeenCalled();
    expect(useWorkflowStore.getState().connectionStatus).toBe("error");
    vi.useRealTimers();
  });

  it.each(["test_run_completed", "testing_completed"] as const)(
    "refreshes the snapshot for %s",
    async (type) => {
      vi.useFakeTimers();
      vi.spyOn(workflows, "getWorkflow").mockResolvedValue(
        workflowSnapshot({ testing: { executed: true, passed: true } }),
      );
      useWorkflowStore.getState().setSnapshot(workflowSnapshot());
      render(<HookHarness />);
      act(() => {
        MockEventSource.instances[0].emit(
          type,
          JSON.stringify(
            workflowEvent(6, {
              type,
              status: "completed",
              stage: "testing",
              data:
                type === "test_run_completed"
                  ? { passed: true, summary: "1 passed, 2 warnings" }
                  : {},
            }),
          ),
        );
      });
      if (type === "test_run_completed") {
        expect(
          useWorkflowStore.getState().snapshot?.testing
            .final_test_result_summary,
        ).toBe("1 passed, 2 warnings");
      }
      await act(async () => vi.advanceTimersByTimeAsync(180));
      expect(workflows.getWorkflow).toHaveBeenCalledTimes(1);
      vi.useRealTimers();
    },
  );

  it("retries a stale terminal snapshot at most three times", async () => {
    vi.useFakeTimers();
    vi.spyOn(workflows, "getWorkflow")
      .mockResolvedValueOnce(workflowSnapshot({ terminal_status: "running" }))
      .mockResolvedValueOnce(workflowSnapshot({ terminal_status: "running" }))
      .mockResolvedValueOnce(
        workflowSnapshot({ terminal_status: "completed" }),
      );
    render(<HookHarness />);
    act(() => {
      MockEventSource.instances[0].emit(
        "workflow_completed",
        JSON.stringify(
          workflowEvent(9, {
            type: "workflow_completed",
            status: "completed",
          }),
        ),
      );
    });
    await act(async () => vi.advanceTimersByTimeAsync(850));
    expect(workflows.getWorkflow).toHaveBeenCalledTimes(3);
    expect(MockEventSource.instances[0].close).toHaveBeenCalled();
    vi.useRealTimers();
  });

  it("cancels terminal retries when the final consumer unmounts", async () => {
    vi.useFakeTimers();
    vi.spyOn(workflows, "getWorkflow").mockResolvedValue(
      workflowSnapshot({ terminal_status: "running" }),
    );
    const view = render(<HookHarness />);
    act(() => {
      MockEventSource.instances[0].emit(
        "workflow_completed",
        JSON.stringify(
          workflowEvent(9, {
            type: "workflow_completed",
            status: "completed",
          }),
        ),
      );
    });
    view.unmount();
    await act(async () => vi.advanceTimersByTimeAsync(900));
    expect(workflows.getWorkflow).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
    vi.useRealTimers();
  });

  it("shows reconnecting without a permanent banner", () => {
    render(<HookHarness />);
    act(() => MockEventSource.instances[0].onerror?.());
    expect(useWorkflowStore.getState().connectionStatus).toBe("reconnecting");
    expect(useWorkflowStore.getState().error).toBeNull();
  });

  it("closes EventSource on unmount", async () => {
    const view = render(<HookHarness />);
    const source = MockEventSource.instances[0];
    view.unmount();
    await waitFor(() => expect(source.close).toHaveBeenCalled());
  });
});
