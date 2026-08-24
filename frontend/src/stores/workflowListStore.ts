import { create } from "zustand";

import type { UiError, WorkflowListItem, WorkflowListResponse } from "../api/types";

interface WorkflowListStore {
  items: WorkflowListItem[];
  total: number;
  limit: number;
  offset: number;
  hasMore: boolean;
  isLoading: boolean;
  isRefreshing: boolean;
  error: UiError | null;
  setLoading: (refreshing: boolean) => void;
  setResult: (result: WorkflowListResponse) => void;
  setError: (error: UiError | null) => void;
  reset: () => void;
}

const initialState = {
  items: [] as WorkflowListItem[],
  total: 0,
  limit: 20,
  offset: 0,
  hasMore: false,
  isLoading: false,
  isRefreshing: false,
  error: null,
};

export const useWorkflowListStore = create<WorkflowListStore>((set, get) => ({
  ...initialState,
  setLoading: (refreshing) =>
    set({
      isLoading: !refreshing && get().items.length === 0,
      isRefreshing: refreshing || get().items.length > 0,
      error: null,
    }),
  setResult: (result) =>
    set({
      items: result.items,
      total: result.total,
      limit: result.limit,
      offset: result.offset,
      hasMore: result.has_more,
      isLoading: false,
      isRefreshing: false,
      error: null,
    }),
  setError: (error) =>
    set({ error, isLoading: false, isRefreshing: false }),
  reset: () => set(initialState),
}));
