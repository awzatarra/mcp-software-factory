import { create } from "zustand";

import type {
  ProjectFileContentResponse,
  ProjectFileTreeResponse,
  UiError,
  WorkflowProjectSummary,
} from "../api/types";

interface ProjectExplorerStore {
  threadId: string | null;
  summary: WorkflowProjectSummary | null;
  tree: ProjectFileTreeResponse | null;
  selectedPath: string | null;
  content: ProjectFileContentResponse | null;
  contentCache: Record<string, ProjectFileContentResponse>;
  isLoading: boolean;
  isFileLoading: boolean;
  isRefreshing: boolean;
  error: UiError | null;
  fileError: UiError | null;
  reset: (threadId: string) => void;
  setProject: (
    summary: WorkflowProjectSummary,
    tree: ProjectFileTreeResponse | null,
  ) => void;
  setLoading: (refresh: boolean) => void;
  select: (path: string | null) => void;
  setContent: (content: ProjectFileContentResponse) => void;
  setFileLoading: (loading: boolean) => void;
  setError: (error: UiError | null) => void;
  setFileError: (error: UiError | null) => void;
  invalidateContent: () => void;
}

export const useProjectExplorerStore = create<ProjectExplorerStore>((set) => ({
  threadId: null,
  summary: null,
  tree: null,
  selectedPath: null,
  content: null,
  contentCache: {},
  isLoading: false,
  isFileLoading: false,
  isRefreshing: false,
  error: null,
  fileError: null,
  reset: (threadId) =>
    set({
      threadId,
      summary: null,
      tree: null,
      selectedPath: null,
      content: null,
      contentCache: {},
      isLoading: false,
      isFileLoading: false,
      isRefreshing: false,
      error: null,
      fileError: null,
    }),
  setProject: (summary, tree) =>
    set({
      summary,
      tree,
      isLoading: false,
      isRefreshing: false,
      error: null,
    }),
  setLoading: (refresh) =>
    set({ isLoading: !refresh, isRefreshing: refresh, error: null }),
  select: (selectedPath) =>
    set((state) => ({
      selectedPath,
      content:
        selectedPath === null ? null : state.contentCache[selectedPath] ?? null,
      fileError: null,
    })),
  setContent: (content) =>
    set((state) => ({
      content,
      contentCache: { ...state.contentCache, [content.path]: content },
      isFileLoading: false,
      fileError: null,
    })),
  setFileLoading: (isFileLoading) => set({ isFileLoading, fileError: null }),
  setError: (error) =>
    set({ error, isLoading: false, isRefreshing: false }),
  setFileError: (fileError) => set({ fileError, isFileLoading: false }),
  invalidateContent: () => set({ contentCache: {}, content: null }),
}));
