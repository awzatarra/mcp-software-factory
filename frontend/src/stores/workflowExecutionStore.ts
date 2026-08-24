import { create } from "zustand";

import type { UiError, WorkflowAgentExecution } from "../api/types";

interface WorkflowExecutionStore {
  key: string | null;
  execution: WorkflowAgentExecution | null;
  isLoading: boolean;
  isRefreshing: boolean;
  error: UiError | null;
  reset: (key: string) => void;
  setLoading: (refresh: boolean) => void;
  setExecution: (execution: WorkflowAgentExecution) => void;
  setError: (error: UiError | null) => void;
}

export const useWorkflowExecutionStore = create<WorkflowExecutionStore>((set) => ({
  key: null,
  execution: null,
  isLoading: false,
  isRefreshing: false,
  error: null,
  reset: (key) =>
    set({
      key,
      execution: null,
      isLoading: false,
      isRefreshing: false,
      error: null,
    }),
  setLoading: (refresh) =>
    set({
      isLoading: !refresh,
      isRefreshing: refresh,
      error: null,
    }),
  setExecution: (execution) =>
    set({
      execution,
      isLoading: false,
      isRefreshing: false,
      error: null,
    }),
  setError: (error) =>
    set((state) => ({
      error,
      isLoading: false,
      isRefreshing: false,
      execution: state.execution,
    })),
}));
