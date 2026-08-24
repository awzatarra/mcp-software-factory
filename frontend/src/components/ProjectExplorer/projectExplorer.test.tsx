import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { MemoryRouter, useSearchParams } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiClientError } from "../../api/client";
import type {
  ProjectFileTreeResponse,
  WorkflowEvent,
  WorkflowProjectSummary,
} from "../../api/types";
import * as workflows from "../../api/workflows";
import {
  isSafeProjectFilePath,
  resetProjectLoadsForTests,
} from "../../hooks/useWorkflowProject";
import { useProjectExplorerStore } from "../../stores/projectExplorerStore";
import { workflowEvent } from "../../test/fixtures";
import { ProjectExplorer } from "./ProjectExplorer";

const summary: WorkflowProjectSummary = {
  thread_id: "thread-1",
  project_name: "demo-api",
  project_exists: true,
  relative_project_path: "demo-api",
  total_files: 4,
  total_directories: 2,
  total_size_bytes: 512,
  generated_files: ["app/main.py"],
  updated_files: ["tests/test_health.py"],
  detected_framework: "FastAPI",
  detected_test_framework: "pytest",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-02T00:00:00Z",
};

const tree: ProjectFileTreeResponse = {
  thread_id: "thread-1",
  project_name: "demo-api",
  truncated: false,
  total_entries: 6,
  root: {
    name: "demo-api",
    path: "",
    type: "directory",
    size_bytes: null,
    extension: null,
    language: null,
    content_type: null,
    content_available: false,
    is_generated: false,
    is_updated: false,
    children: [
      {
        name: "app",
        path: "app",
        type: "directory",
        size_bytes: null,
        extension: null,
        language: null,
        content_type: null,
        content_available: false,
        is_generated: false,
        is_updated: false,
        children: [
          {
            name: "main.py",
            path: "app/main.py",
            type: "file",
            size_bytes: 44,
            extension: ".py",
            language: "python",
            content_type: "text",
            content_available: true,
            is_generated: true,
            is_updated: false,
          },
        ],
      },
      {
        name: "tests",
        path: "tests",
        type: "directory",
        size_bytes: null,
        extension: null,
        language: null,
        content_type: null,
        content_available: false,
        is_generated: false,
        is_updated: false,
        children: [
          {
            name: "test_health.py",
            path: "tests/test_health.py",
            type: "file",
            size_bytes: 35,
            extension: ".py",
            language: "python",
            content_type: "text",
            content_available: true,
            is_generated: false,
            is_updated: true,
          },
        ],
      },
      {
        name: "image.png",
        path: "image.png",
        type: "file",
        size_bytes: 200,
        extension: ".png",
        language: null,
        content_type: "image",
        content_available: false,
        is_generated: false,
        is_updated: false,
      },
      {
        name: "large.txt",
        path: "large.txt",
        type: "file",
        size_bytes: 2_000_000,
        extension: ".txt",
        language: "text",
        content_type: "text",
        content_available: false,
        is_generated: false,
        is_updated: false,
      },
    ],
  },
};

const content = {
  thread_id: "thread-1",
  project_name: "demo-api",
  path: "app/main.py",
  name: "main.py",
  extension: ".py",
  language: "python",
  content_type: "text" as const,
  encoding: "utf-8",
  size_bytes: 44,
  content: 'from fastapi import FastAPI\napp = FastAPI()\n',
  truncated: false,
  line_count: 2,
  is_generated: true,
  is_updated: false,
};

function Harness({ events = [] }: { events?: WorkflowEvent[] }) {
  const [params, setParams] = useSearchParams();
  return (
    <>
      <ProjectExplorer
        threadId="thread-1"
        events={events}
        selectedFile={params.get("file")}
        onSelectFile={(path) =>
          setParams((current) => {
            const next = new URLSearchParams(current);
            next.set("tab", "project");
            if (path) next.set("file", path);
            return next;
          })
        }
      />
      <output data-testid="query">{params.toString()}</output>
    </>
  );
}

function renderExplorer(
  path = "/workflows/thread-1?tab=project",
  events: WorkflowEvent[] = [],
  strict = false,
) {
  const content = (
    <MemoryRouter initialEntries={[path]}>
      <Harness events={events} />
    </MemoryRouter>
  );
  return render(strict ? <StrictMode>{content}</StrictMode> : content);
}

function mockProject(projectSummary = summary) {
  vi.spyOn(workflows, "getWorkflowProject").mockResolvedValue(projectSummary);
  vi.spyOn(workflows, "getProjectFileTree").mockResolvedValue(tree);
  vi.spyOn(workflows, "getProjectFileContent").mockResolvedValue(content);
}

describe("ProjectExplorer", () => {
  beforeEach(() => {
    vi.useRealTimers();
    resetProjectLoadsForTests();
    useProjectExplorerStore.getState().reset("thread-1");
  });

  it("shows initial loading", () => {
    vi.spyOn(workflows, "getWorkflowProject").mockReturnValue(new Promise(() => undefined));
    renderExplorer();
    expect(screen.getByText("Cargando proyecto…")).toBeVisible();
  });

  it("shows pre-creation empty state", async () => {
    mockProject({ ...summary, project_exists: false, relative_project_path: null });
    renderExplorer();
    expect(await screen.findByText("El proyecto todavía no fue creado.")).toBeVisible();
  });

  it("renders project summary", async () => {
    mockProject();
    renderExplorer();
    expect(await screen.findByRole("heading", { name: "demo-api" })).toBeVisible();
    expect(screen.getByText(/4 archivos/)).toBeVisible();
    expect(screen.getByText("Framework: FastAPI")).toBeVisible();
  });

  it("loads and renders the file tree", async () => {
    mockProject();
    renderExplorer();
    expect(await screen.findByRole("tree", { name: "Archivos del proyecto" })).toBeVisible();
    expect(screen.getByText("main.py")).toBeVisible();
  });

  it("directories collapse and expand", async () => {
    mockProject();
    renderExplorer();
    const app = await screen.findByRole("button", { name: /app/ });
    await userEvent.click(app);
    expect(screen.queryByText("main.py")).not.toBeInTheDocument();
    await userEvent.click(app);
    expect(screen.getByText("main.py")).toBeVisible();
  });

  it("selecting a file requests only its content", async () => {
    mockProject();
    renderExplorer();
    await userEvent.click(await screen.findByRole("button", { name: /main.py/ }));
    await waitFor(() =>
      expect(workflows.getProjectFileContent).toHaveBeenCalledWith(
        "thread-1",
        "app/main.py",
        expect.any(AbortSignal),
      ),
    );
    expect(workflows.getProjectFileContent).toHaveBeenCalledTimes(1);
  });

  it("does not preload content for tree nodes", async () => {
    mockProject();
    renderExplorer();
    await screen.findByText("main.py");
    expect(workflows.getProjectFileContent).not.toHaveBeenCalled();
  });

  it("renders Prism tokens for Python", async () => {
    mockProject();
    const { container } = renderExplorer(
      "/workflows/thread-1?tab=project&file=app%2Fmain.py",
    );
    await screen.findByText("2 líneas");
    expect(container.querySelector(".token.keyword")).toBeInTheDocument();
  });

  it("renders plain text without executing it", async () => {
    mockProject();
    vi.mocked(workflows.getProjectFileContent).mockResolvedValue({
      ...content,
      language: "text",
      content: "<script>window.evil=true</script>",
    });
    renderExplorer("/workflows/thread-1?tab=project&file=app%2Fmain.py");
    expect(await screen.findByText("<script>window.evil=true</script>")).toBeVisible();
    expect(document.querySelector("script")).not.toBeInTheDocument();
  });

  it("shows generated badge", async () => {
    mockProject();
    renderExplorer();
    const main = await screen.findByRole("button", { name: /main.py/ });
    expect(within(main).getByText("Nuevo")).toBeVisible();
  });

  it("shows modified badge", async () => {
    mockProject();
    renderExplorer();
    const test = await screen.findByRole("button", { name: /test_health.py/ });
    expect(within(test).getByText("Modificado")).toBeVisible();
  });

  it("copies only file content", async () => {
    mockProject();
    renderExplorer("/workflows/thread-1?tab=project&file=app%2Fmain.py");
    await userEvent.click(await screen.findByRole("button", { name: "Copiar" }));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(content.content);
    expect(screen.getByRole("button", { name: "Copiado" })).toBeVisible();
  });

  it("shows clipboard failures", async () => {
    mockProject();
    vi.mocked(navigator.clipboard.writeText).mockRejectedValueOnce(new Error("denied"));
    renderExplorer("/workflows/thread-1?tab=project&file=app%2Fmain.py");
    await userEvent.click(await screen.findByRole("button", { name: "Copiar" }));
    expect(screen.getByRole("alert")).toHaveTextContent("No se pudo copiar");
  });

  it("builds an encoded individual download URL", async () => {
    mockProject();
    renderExplorer("/workflows/thread-1?tab=project&file=app%2Fmain.py");
    const link = await screen.findByRole("link", { name: "Descargar" });
    expect(link).toHaveAttribute("href", expect.stringContaining("path=app%2Fmain.py"));
  });

  it("offers ZIP download", async () => {
    mockProject();
    renderExplorer();
    expect(await screen.findByRole("link", { name: "Descargar ZIP" })).toHaveAttribute(
      "href",
      expect.stringContaining("/project/archive"),
    );
  });

  it.each([
    ["image.png", "image.png"],
    ["large.txt", "large.txt"],
  ])("shows non-previewable fallback for %s", async (path, label) => {
    mockProject();
    renderExplorer(`/workflows/thread-1?tab=project&file=${path}`);
    await screen.findByRole("heading", { name: label });
    expect(screen.getByText("Este archivo no puede previsualizarse.")).toBeVisible();
    expect(workflows.getProjectFileContent).not.toHaveBeenCalled();
  });

  it("shows file loading independently", async () => {
    mockProject();
    vi.mocked(workflows.getProjectFileContent).mockReturnValue(new Promise(() => undefined));
    renderExplorer("/workflows/thread-1?tab=project&file=app%2Fmain.py");
    expect(await screen.findByText("Cargando archivo…")).toBeVisible();
  });

  it("shows retryable project errors", async () => {
    vi.spyOn(workflows, "getWorkflowProject").mockRejectedValue(
      new ApiClientError({
        title: "Offline",
        message: "API offline",
        retryable: true,
      }),
    );
    renderExplorer();
    expect(await screen.findByRole("button", { name: "Reintentar" })).toBeVisible();
  });

  it.each([
    ["app/main.py", true],
    ["tests/test_health.py", true],
    ["../secret", false],
    [String.raw`..\secret`, false],
    ["/etc/passwd", false],
    [String.raw`C:\Windows`, false],
    [String.raw`\\server\share`, false],
    ["", false],
  ])("validates client path %s", (path, expected) => {
    expect(isSafeProjectFilePath(path)).toBe(expected);
  });

  it("hydrates selected file from query params", async () => {
    mockProject();
    renderExplorer("/workflows/thread-1?tab=project&file=tests%2Ftest_health.py");
    await waitFor(() =>
      expect(workflows.getProjectFileContent).toHaveBeenCalledWith(
        "thread-1",
        "tests/test_health.py",
        expect.any(AbortSignal),
      ),
    );
  });

  it("updates query params when selecting a file", async () => {
    mockProject();
    renderExplorer();
    await userEvent.click(await screen.findByRole("button", { name: /main.py/ }));
    expect(screen.getByTestId("query")).toHaveTextContent("file=app%2Fmain.py");
  });

  it("changing thread resets content cache", () => {
    useProjectExplorerStore.getState().setContent(content);
    useProjectExplorerStore.getState().reset("thread-2");
    expect(useProjectExplorerStore.getState().content).toBeNull();
    expect(useProjectExplorerStore.getState().contentCache).toEqual({});
  });

  it("reuses cached content", async () => {
    mockProject();
    useProjectExplorerStore.getState().setContent(content);
    renderExplorer("/workflows/thread-1?tab=project&file=app%2Fmain.py");
    await screen.findByText("2 líneas");
    expect(workflows.getProjectFileContent).not.toHaveBeenCalled();
  });

  it.each([
    ["implementation_completed", {}],
    ["repair_completed", {}],
    ["workflow_completed", {}],
    ["tool_completed", { tool_name: "filesystem__create_project_structure" }],
    ["tool_completed", { tool_name: "filesystem__update_project_files" }],
  ])("refreshes for %s event", async (type, data) => {
    vi.useFakeTimers();
    mockProject();
    const rendered = renderExplorer();
    await act(async () => Promise.resolve());
    const callsBefore = vi.mocked(workflows.getWorkflowProject).mock.calls.length;
    rendered.rerender(
      <MemoryRouter initialEntries={["/workflows/thread-1?tab=project"]}>
        <Harness
          events={[
            workflowEvent(20, {
              type: type as WorkflowEvent["type"],
              data,
            }),
          ]}
        />
      </MemoryRouter>,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(vi.mocked(workflows.getWorkflowProject).mock.calls.length).toBeGreaterThan(callsBefore);
    vi.useRealTimers();
  });

  it("debounces duplicate refresh events", async () => {
    vi.useFakeTimers();
    mockProject();
    renderExplorer("", [
      workflowEvent(20, { type: "implementation_completed" }),
      workflowEvent(21, { type: "workflow_completed" }),
    ]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(workflows.getWorkflowProject).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });

  it("exposes an accessible tree", async () => {
    mockProject();
    renderExplorer();
    const selectedTree = await screen.findByRole("tree", { name: "Archivos del proyecto" });
    expect(within(selectedTree).getAllByRole("treeitem").length).toBeGreaterThan(2);
  });

  it("deduplicates project requests under StrictMode", async () => {
    mockProject();
    renderExplorer("/workflows/thread-1?tab=project", [], true);
    await screen.findByText("main.py");
    expect(workflows.getWorkflowProject).toHaveBeenCalledTimes(1);
    expect(workflows.getProjectFileTree).toHaveBeenCalledTimes(1);
  });

  it("shows exact size and date in tooltips", async () => {
    mockProject();
    renderExplorer("/workflows/thread-1?tab=project&file=app%2Fmain.py");
    expect(await screen.findByTitle("44 bytes")).toBeVisible();
    expect(screen.getByTitle("2026-01-02T00:00:00Z")).toBeVisible();
  });
});
