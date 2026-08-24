import { useCallback, useEffect, useMemo, useRef } from "react";

import { ApiClientError } from "../api/client";
import {
  getProjectFileContent,
  getProjectFileTree,
  getWorkflowProject,
} from "../api/workflows";
import type { WorkflowEvent } from "../api/types";
import { useProjectExplorerStore } from "../stores/projectExplorerStore";

const REFRESH_EVENTS = new Set([
  "implementation_completed",
  "repair_completed",
  "workflow_completed",
]);
const REFRESH_TOOLS = new Set([
  "create_project_structure",
  "filesystem__create_project_structure",
  "update_project_files",
  "filesystem__update_project_files",
]);
interface ProjectLoad {
  controller: AbortController;
  promise: Promise<unknown>;
}

const projectLoads = new Map<string, ProjectLoad>();

export function resetProjectLoadsForTests(): void {
  for (const load of projectLoads.values()) load.controller.abort();
  projectLoads.clear();
}

export function isSafeProjectFilePath(path: string): boolean {
  const normalized = path.replaceAll("\\", "/");
  return Boolean(
    normalized &&
      !normalized.startsWith("/") &&
      !normalized.startsWith("//") &&
      !/^[a-zA-Z]:/.test(normalized) &&
      !normalized.split("/").includes(".."),
  );
}

function projectError(caught: unknown, title: string) {
  if (caught instanceof ApiClientError) {
    return { ...caught.toUiError(title), scope: "project" as const };
  }
  return {
    title,
    message: "Ocurrió un error inesperado.",
    retryable: true,
    scope: "project" as const,
  };
}

export function useWorkflowProject(
  threadId: string,
  active: boolean,
  events: WorkflowEvent[],
) {
  const storedThreadId = useProjectExplorerStore((state) => state.threadId);
  const selectedPath = useProjectExplorerStore((state) => state.selectedPath);
  const setLoading = useProjectExplorerStore((state) => state.setLoading);
  const setProject = useProjectExplorerStore((state) => state.setProject);
  const setError = useProjectExplorerStore((state) => state.setError);
  const reset = useProjectExplorerStore((state) => state.reset);
  const select = useProjectExplorerStore((state) => state.select);
  const setFileLoading = useProjectExplorerStore((state) => state.setFileLoading);
  const setContent = useProjectExplorerStore((state) => state.setContent);
  const setFileError = useProjectExplorerStore((state) => state.setFileError);
  const invalidateContent = useProjectExplorerStore((state) => state.invalidateContent);
  const requestVersion = useRef(0);
  const fileController = useRef<AbortController | null>(null);
  const latestRefreshEvent = useMemo(
    () =>
      events
        .filter(
          (event) =>
            REFRESH_EVENTS.has(event.type) ||
            (event.type === "tool_completed" &&
              REFRESH_TOOLS.has(String(event.data.tool_name ?? event.data.tool ?? ""))),
        )
        .at(-1)?.sequence ?? 0,
    [events],
  );

  const loadProject = useCallback(
    async (refresh = false) => {
      const version = ++requestVersion.current;
      setLoading(refresh);
      const key = `${threadId}:project`;
      let load = projectLoads.get(key);
      if (!load) {
        const controller = new AbortController();
        const promise = (async () => {
          const summary = await getWorkflowProject(threadId, controller.signal);
          const tree = summary.project_exists
            ? await getProjectFileTree(threadId, controller.signal)
            : null;
          return { summary, tree };
        })().finally(() => projectLoads.delete(key));
        load = { controller, promise };
        projectLoads.set(key, load);
      }
      try {
        const result = (await load.promise) as {
          summary: Awaited<ReturnType<typeof getWorkflowProject>>;
          tree: Awaited<ReturnType<typeof getProjectFileTree>> | null;
        };
        if (version === requestVersion.current) setProject(result.summary, result.tree);
      } catch (caught) {
        if (caught instanceof DOMException && caught.name === "AbortError") return;
        if (version === requestVersion.current) {
          setError(projectError(caught, "No se pudo cargar el proyecto"));
        }
      }
    },
    [setError, setLoading, setProject, threadId],
  );

  const loadFile = useCallback(
    async (path: string, force = false) => {
      if (!isSafeProjectFilePath(path)) {
        setFileError({
          title: "Ruta inválida",
          message: "La ruta seleccionada no pertenece al proyecto.",
          retryable: false,
          scope: "project",
        });
        return;
      }
      select(path);
      if (!force && useProjectExplorerStore.getState().contentCache[path]) return;
      fileController.current?.abort();
      const controller = new AbortController();
      fileController.current = controller;
      setFileLoading(true);
      try {
        setContent(await getProjectFileContent(threadId, path, controller.signal));
      } catch (caught) {
        if (caught instanceof DOMException && caught.name === "AbortError") return;
        setFileError(projectError(caught, "No se pudo abrir el archivo"));
      } finally {
        if (fileController.current === controller) fileController.current = null;
      }
    },
    [select, setContent, setFileError, setFileLoading, threadId],
  );

  useEffect(() => {
    if (storedThreadId !== threadId) reset(threadId);
  }, [reset, storedThreadId, threadId]);

  useEffect(
    () => () => {
      requestVersion.current += 1;
      fileController.current?.abort();
    },
    [],
  );

  useEffect(() => {
    if (active) {
      void loadProject(Boolean(useProjectExplorerStore.getState().summary));
    }
  }, [active, loadProject]);

  useEffect(() => {
    if (!active || latestRefreshEvent === 0) return;
    const handle = window.setTimeout(() => {
      invalidateContent();
      void loadProject(true);
      if (selectedPath) void loadFile(selectedPath, true);
    }, 250);
    return () => window.clearTimeout(handle);
  }, [
    active,
    invalidateContent,
    latestRefreshEvent,
    loadFile,
    loadProject,
    selectedPath,
  ]);

  return { loadProject, loadFile };
}
