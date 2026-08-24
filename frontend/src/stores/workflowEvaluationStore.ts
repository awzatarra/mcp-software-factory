import { create } from "zustand";

import type { UiError, WorkflowEvaluation } from "../api/types";

interface WorkflowEvaluationStore {
  key: string | null;
  evaluation: WorkflowEvaluation | null;
  isLoading: boolean;
  isRefreshing: boolean;
  error: UiError | null;
  reset: (key: string) => void;
  setLoading: (refresh: boolean) => void;
  setEvaluation: (evaluation: WorkflowEvaluation) => void;
  setError: (error: UiError | null) => void;
}

export const useWorkflowEvaluationStore = create<WorkflowEvaluationStore>((set) => ({
  key: null,
  evaluation: null,
  isLoading: false,
  isRefreshing: false,
  error: null,
  reset: (key) =>
    set({
      key,
      evaluation: null,
      isLoading: false,
      isRefreshing: false,
      error: null,
    }),
  setLoading: (refresh) =>
    set((state) => ({
      isLoading: !refresh,
      isRefreshing: refresh,
      error: null,
      evaluation: state.evaluation,
    })),
  setEvaluation: (evaluation) =>
    set({
      evaluation,
      isLoading: false,
      isRefreshing: false,
      error: null,
    }),
  setError: (error) =>
    set((state) => ({
      error,
      isLoading: false,
      isRefreshing: false,
      evaluation: state.evaluation,
    })),
}));
