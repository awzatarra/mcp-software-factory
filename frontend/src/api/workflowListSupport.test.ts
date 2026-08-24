import { beforeEach, describe, expect, it, vi } from "vitest";

import { getWorkflows } from "./workflows";
import { useWorkflowListStore } from "../stores/workflowListStore";
import type { WorkflowListResponse } from "./types";

const empty: WorkflowListResponse = {
  items: [],
  total: 0,
  limit: 20,
  offset: 0,
  has_more: false,
};

function successfulFetch() {
  return vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(empty), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

describe("workflow list support", () => {
  beforeEach(() => useWorkflowListStore.getState().reset());

  it("uses documented API defaults", async () => {
    const fetch = successfulFetch();
    await getWorkflows();
    expect(fetch.mock.calls[0][0]).toContain(
      "limit=20&offset=0&sort_by=updated_at&sort_order=desc",
    );
  });

  it("encodes search instead of concatenating it", async () => {
    const fetch = successfulFetch();
    await getWorkflows({ search: "ui auto&status=failed" });
    const url = String(fetch.mock.calls[0][0]);
    expect(url).toContain("search=ui+auto%26status%3Dfailed");
    expect(url).not.toContain("search=ui auto");
  });

  it("maps every list option to query parameters", async () => {
    const fetch = successfulFetch();
    await getWorkflows({
      status: "waiting",
      limit: 100,
      offset: 40,
      sortBy: "project_name",
      sortOrder: "asc",
    });
    const url = String(fetch.mock.calls[0][0]);
    expect(url).toContain("status=waiting");
    expect(url).toContain("limit=100");
    expect(url).toContain("offset=40");
    expect(url).toContain("sort_by=project_name");
    expect(url).toContain("sort_order=asc");
  });

  it("trims an empty search from the request", async () => {
    const fetch = successfulFetch();
    await getWorkflows({ search: "   " });
    expect(String(fetch.mock.calls[0][0])).not.toContain("search=");
  });

  it("stores a typed page result", () => {
    useWorkflowListStore.getState().setResult({
      ...empty,
      total: 30,
      offset: 20,
      has_more: true,
    });
    const state = useWorkflowListStore.getState();
    expect(state.total).toBe(30);
    expect(state.offset).toBe(20);
    expect(state.hasMore).toBe(true);
  });

  it("distinguishes initial loading from refresh", () => {
    useWorkflowListStore.getState().setLoading(false);
    expect(useWorkflowListStore.getState().isLoading).toBe(true);
    useWorkflowListStore.getState().setResult(empty);
    useWorkflowListStore.getState().setLoading(true);
    expect(useWorkflowListStore.getState().isRefreshing).toBe(true);
  });

  it("keeps the previous page when refresh fails", () => {
    const current = {
      ...empty,
      items: [{ thread_id: "kept" }] as never,
      total: 1,
    };
    useWorkflowListStore.getState().setResult(current);
    useWorkflowListStore.getState().setError({
      title: "Offline",
      message: "No connection",
      retryable: true,
    });
    expect(useWorkflowListStore.getState().items).toHaveLength(1);
  });

  it("reset clears list state", () => {
    useWorkflowListStore.getState().setResult({ ...empty, total: 4 });
    useWorkflowListStore.getState().reset();
    expect(useWorkflowListStore.getState().total).toBe(0);
    expect(useWorkflowListStore.getState().error).toBeNull();
  });
});
