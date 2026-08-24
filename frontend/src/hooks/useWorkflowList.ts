import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { ApiClientError } from "../api/client";
import { getWorkflows } from "../api/workflows";
import type {
  WorkflowListStatus,
  WorkflowSortBy,
  WorkflowSortOrder,
} from "../api/types";
import { useWorkflowListStore } from "../stores/workflowListStore";

const PAGE_SIZE = 20;
const ACTIVE_STATUSES = new Set(["pending", "running"]);
type WorkflowListOptions = NonNullable<Parameters<typeof getWorkflows>[0]>;
interface SharedRequest {
  promise: Promise<Awaited<ReturnType<typeof getWorkflows>>>;
  controller: AbortController;
  consumers: number;
  abortTimer: number | null;
}
const inFlight = new Map<string, SharedRequest>();

export function resetWorkflowListRequestsForTests(): void {
  for (const request of inFlight.values()) {
    request.controller.abort();
    if (request.abortTimer !== null) window.clearTimeout(request.abortTimer);
  }
  inFlight.clear();
}

function requestKey(options: Record<string, string | number | undefined>): string {
  return JSON.stringify(options);
}

function requestOnce(
  options: WorkflowListOptions,
): SharedRequest {
  const key = requestKey({
    status: options.status,
    search: options.search,
    limit: options.limit,
    offset: options.offset,
    sortBy: options.sortBy,
    sortOrder: options.sortOrder,
  });
  const existing = inFlight.get(key);
  if (existing) {
    existing.consumers += 1;
    if (existing.abortTimer !== null) {
      window.clearTimeout(existing.abortTimer);
      existing.abortTimer = null;
    }
    return existing;
  }
  const controller = new AbortController();
  const request: SharedRequest = {
    controller,
    consumers: 1,
    abortTimer: null,
    promise: Promise.resolve({ items: [], total: 0, limit: 20, offset: 0, has_more: false }),
  };
  request.promise = getWorkflows({ ...options, signal: controller.signal }).finally(() => {
    if (inFlight.get(key) === request) inFlight.delete(key);
  });
  inFlight.set(key, request);
  return request;
}

function releaseRequest(options: WorkflowListOptions, request: SharedRequest): void {
  const key = requestKey({
    status: options.status,
    search: options.search,
    limit: options.limit,
    offset: options.offset,
    sortBy: options.sortBy,
    sortOrder: options.sortOrder,
  });
  request.consumers = Math.max(0, request.consumers - 1);
  if (request.consumers > 0 || request.abortTimer !== null) return;
  request.abortTimer = window.setTimeout(() => {
    if (request.consumers === 0 && inFlight.get(key) === request) {
      request.controller.abort();
      inFlight.delete(key);
    }
  }, 0);
}

function parseOffset(value: string | null): number {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : 0;
}

function parseSort(params: URLSearchParams): {
  sortBy: WorkflowSortBy;
  sortOrder: WorkflowSortOrder;
} {
  const sortBy = params.get("sort_by");
  const sortOrder = params.get("sort_order");
  return {
    sortBy:
      sortBy === "created_at" ||
      sortBy === "project_name" ||
      sortBy === "terminal_status"
        ? sortBy
        : "updated_at",
    sortOrder: sortOrder === "asc" ? "asc" : "desc",
  };
}

export function useWorkflowList() {
  const [params, setParams] = useSearchParams();
  const items = useWorkflowListStore((state) => state.items);
  const setLoading = useWorkflowListStore((state) => state.setLoading);
  const setResult = useWorkflowListStore((state) => state.setResult);
  const setError = useWorkflowListStore((state) => state.setError);
  const [searchInput, setSearchInput] = useState(params.get("search") ?? "");
  const requestVersion = useRef(0);
  const status = params.get("status") as WorkflowListStatus | null;
  const offset = parseOffset(params.get("offset"));
  const { sortBy, sortOrder } = parseSort(params);
  const search = params.get("search")?.trim() || undefined;

  const options = useMemo(
    () => ({
      status: status ?? undefined,
      search,
      limit: PAGE_SIZE,
      offset,
      sortBy,
      sortOrder,
    }),
    [offset, search, sortBy, sortOrder, status],
  );

  const load = useCallback(
    async (refreshing = false, signal?: AbortSignal) => {
      const version = ++requestVersion.current;
      setLoading(refreshing);
      const request = requestOnce(options);
      let released = false;
      const release = () => {
        if (released) return;
        released = true;
        releaseRequest(options, request);
      };
      signal?.addEventListener("abort", release, { once: true });
      try {
        const result = await request.promise;
        if (!signal?.aborted && version === requestVersion.current) setResult(result);
      } catch (caught) {
        if (
          caught instanceof DOMException &&
          caught.name === "AbortError"
        ) {
          if (version === requestVersion.current) setError(null);
          return;
        }
        if (version !== requestVersion.current) return;
        setError(
          caught instanceof ApiClientError
            ? { ...caught.toUiError("No se pudieron cargar los workflows"), scope: "list" }
            : {
                title: "No se pudieron cargar los workflows",
                message: "Ocurrió un error inesperado.",
                retryable: true,
                scope: "list",
            },
        );
      } finally {
        signal?.removeEventListener("abort", release);
        release();
      }
    },
    [options, setError, setLoading, setResult],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(
      useWorkflowListStore.getState().items.length > 0,
      controller.signal,
    );
    return () => {
      requestVersion.current += 1;
      controller.abort();
    };
  }, [load]);

  useEffect(() => {
    const handle = window.setTimeout(() => {
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          const normalized = searchInput.trim();
          if (normalized) next.set("search", normalized);
          else next.delete("search");
          next.delete("offset");
          return next.toString() === current.toString() ? current : next;
        },
        { replace: true },
      );
    }, 300);
    return () => window.clearTimeout(handle);
  }, [params, searchInput, setParams]);

  const hasActiveItems = items.some(
    (item) =>
      ACTIVE_STATUSES.has(item.terminal_status) ||
      (item.interrupted && item.pending_operation !== null),
  );
  useEffect(() => {
    if (!hasActiveItems || document.visibilityState === "hidden") return;
    const handle = window.setInterval(() => void load(true), 5_000);
    return () => window.clearInterval(handle);
  }, [hasActiveItems, load]);

  const patchParams = useCallback(
    (updates: Record<string, string | null>) => {
      setParams((current) => {
        const next = new URLSearchParams(current);
        for (const [key, value] of Object.entries(updates)) {
          if (value) next.set(key, value);
          else next.delete(key);
        }
        if (!Object.hasOwn(updates, "offset")) next.delete("offset");
        return next;
      });
    },
    [setParams],
  );

  return {
    searchInput,
    setSearchInput,
    status: status ?? "",
    offset,
    sortBy,
    sortOrder,
    setStatus: (value: string) => patchParams({ status: value || null }),
    setSort: (value: string) => {
      const [nextSortBy, nextSortOrder] = value.split(":");
      patchParams({
        sort_by: nextSortBy === "updated_at" ? null : nextSortBy,
        sort_order: nextSortOrder === "desc" ? null : nextSortOrder,
      });
    },
    nextPage: () => patchParams({ offset: String(offset + PAGE_SIZE) }),
    previousPage: () =>
      patchParams({ offset: offset > PAGE_SIZE ? String(offset - PAGE_SIZE) : null }),
    clearFilters: () => {
      setSearchInput("");
      setParams(new URLSearchParams(), { replace: true });
    },
    refetch: () => void load(true),
  };
}
