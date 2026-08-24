import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  getCompleteWorkflowHistory,
  getWorkflow,
} from "../api/workflows";
import { useWorkflowStore } from "../stores/workflowStore";
import { workflowSnapshot } from "../test/fixtures";
import {
  loadWorkflow,
  releaseWorkflowLoad,
  resetWorkflowLoadsForTests,
} from "./workflowLoader";

vi.mock("../api/workflows", () => ({
  getWorkflow: vi.fn(),
  getCompleteWorkflowHistory: vi.fn(),
}));

const getWorkflowMock = vi.mocked(getWorkflow);
const getHistoryMock = vi.mocked(getCompleteWorkflowHistory);

function resolvedHistory(threadId = "thread-1") {
  return {
    thread_id: threadId,
    branch_id: "original",
    events: [],
    last_sequence: 0,
    has_more: false,
  };
}

describe("workflow loader", () => {
  beforeEach(() => {
    vi.useRealTimers();
    resetWorkflowLoadsForTests();
    useWorkflowStore.getState().reset("thread-1");
    getWorkflowMock.mockReset();
    getHistoryMock.mockReset();
    getWorkflowMock.mockResolvedValue(workflowSnapshot());
    getHistoryMock.mockResolvedValue(resolvedHistory());
  });

  it("reuses one Promise and one request pair per thread", async () => {
    const first = loadWorkflow("thread-1");
    const second = loadWorkflow("thread-1");
    expect(second).toBe(first);
    await first;
    expect(getWorkflowMock).toHaveBeenCalledTimes(1);
    expect(getHistoryMock).toHaveBeenCalledTimes(1);
  });

  it("cancels an obsolete thread load after navigation", async () => {
    vi.useFakeTimers();
    let obsoleteSignal: AbortSignal | undefined;
    getWorkflowMock.mockImplementation((threadId, signal) => {
      if (threadId !== "old-thread") {
        return Promise.resolve(
          workflowSnapshot({ thread_id: "new-thread" }),
        );
      }
      obsoleteSignal = signal;
      return new Promise((_resolve, reject) => {
        signal?.addEventListener(
          "abort",
          () => reject(new DOMException("Aborted", "AbortError")),
          { once: true },
        );
      });
    });
    getHistoryMock.mockImplementation((threadId, signal) => {
      if (threadId !== "old-thread") {
        return Promise.resolve(resolvedHistory("new-thread"));
      }
      return new Promise((_resolve, reject) => {
        signal?.addEventListener(
          "abort",
          () => reject(new DOMException("Aborted", "AbortError")),
          { once: true },
        );
      });
    });

    useWorkflowStore.getState().reset("old-thread");
    const obsolete = loadWorkflow("old-thread");
    releaseWorkflowLoad("old-thread");
    useWorkflowStore.getState().reset("new-thread");
    await loadWorkflow("new-thread");
    await vi.advanceTimersByTimeAsync(51);
    await obsolete;

    expect(obsoleteSignal?.aborted).toBe(true);
    expect(useWorkflowStore.getState().error).toBeNull();
    expect(useWorkflowStore.getState().snapshot?.thread_id).toBe("new-thread");
  });

  it("loads durable history from the active branch without mixing original", async () => {
    useWorkflowStore.getState().reset("thread-1", "fork-1");
    getHistoryMock.mockResolvedValue({
      ...resolvedHistory(),
      branch_id: "fork-1",
    });
    await loadWorkflow("thread-1", "fork-1");
    expect(getHistoryMock).toHaveBeenCalledWith(
      "thread-1",
      expect.any(AbortSignal),
      "fork-1",
    );
    expect(useWorkflowStore.getState().branchId).toBe("fork-1");
  });
});
