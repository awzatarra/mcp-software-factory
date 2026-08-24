import {
  Archive,
  Check,
  Copy,
  Download,
  FileQuestion,
  FolderOpen,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import {
  projectArchiveUrl,
  projectFileDownloadUrl,
} from "../../api/workflows";
import type { ProjectFileNode, WorkflowEvent } from "../../api/types";
import { useWorkflowProject, isSafeProjectFilePath } from "../../hooks/useWorkflowProject";
import { useProjectExplorerStore } from "../../stores/projectExplorerStore";
import { ErrorBanner } from "../ErrorBanner";
import { ProjectFileTree } from "./ProjectFileTree";
import { SyntaxHighlightedCode } from "./SyntaxHighlightedCode";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} bytes`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function findNode(root: ProjectFileNode, path: string): ProjectFileNode | null {
  if (root.path === path) return root;
  for (const child of root.children ?? []) {
    const selected = findNode(child, path);
    if (selected) return selected;
  }
  return null;
}

export function ProjectExplorer({
  threadId,
  events,
  selectedFile,
  onSelectFile,
}: {
  threadId: string;
  events: WorkflowEvent[];
  selectedFile: string | null;
  onSelectFile: (path: string | null) => void;
}) {
  const summary = useProjectExplorerStore((state) => state.summary);
  const tree = useProjectExplorerStore((state) => state.tree);
  const selectedPath = useProjectExplorerStore((state) => state.selectedPath);
  const content = useProjectExplorerStore((state) => state.content);
  const loading = useProjectExplorerStore((state) => state.isLoading);
  const fileLoading = useProjectExplorerStore((state) => state.isFileLoading);
  const refreshing = useProjectExplorerStore((state) => state.isRefreshing);
  const error = useProjectExplorerStore((state) => state.error);
  const fileError = useProjectExplorerStore((state) => state.fileError);
  const setError = useProjectExplorerStore((state) => state.setError);
  const setFileError = useProjectExplorerStore((state) => state.setFileError);
  const { loadProject, loadFile } = useWorkflowProject(threadId, true, events);
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const selectedNode = useMemo(
    () => tree && selectedPath ? findNode(tree.root, selectedPath) : null,
    [selectedPath, tree],
  );

  useEffect(() => {
    if (
      tree &&
      selectedFile &&
      isSafeProjectFilePath(selectedFile) &&
      findNode(tree.root, selectedFile)?.type === "file"
    ) {
      const node = findNode(tree.root, selectedFile);
      if (node?.content_available) void loadFile(selectedFile);
      else useProjectExplorerStore.getState().select(selectedFile);
    }
  }, [loadFile, selectedFile, tree]);

  const selectFile = (node: ProjectFileNode) => {
    onSelectFile(node.path);
    useProjectExplorerStore.getState().select(node.path);
  };

  const copy = async () => {
    if (!content) return;
    try {
      await navigator.clipboard.writeText(content.content);
      setCopied(true);
      setCopyFailed(false);
      window.setTimeout(() => setCopied(false), 1_500);
    } catch {
      setCopyFailed(true);
    }
  };

  if (loading && !summary) {
    return (
      <div className="project-empty" role="status">
        <LoaderCircle className="spin" aria-hidden="true" />
        Cargando proyecto…
      </div>
    );
  }
  if (error && !summary) {
    return (
      <ErrorBanner
        error={error}
        onRetry={error.retryable ? () => void loadProject() : undefined}
        onDismiss={() => setError(null)}
      />
    );
  }
  if (!summary?.project_exists) {
    return (
      <div className="project-empty">
        <FolderOpen aria-hidden="true" />
        <h2>El proyecto todavía no fue creado.</h2>
        <p>Aprueba la creación para explorar sus archivos.</p>
      </div>
    );
  }

  return (
    <section className="project-explorer" aria-labelledby="project-explorer-title">
      <header className="project-explorer-header">
        <div>
          <span className="section-kicker">Proyecto</span>
          <h2 id="project-explorer-title">{summary.project_name}</h2>
          <span className="project-stats" title={`${summary.total_size_bytes} bytes`}>
            {summary.total_files} archivos · {summary.total_directories} carpetas · {formatSize(summary.total_size_bytes)}
          </span>
        </div>
        <div className="project-header-actions">
          {refreshing ? <span><RefreshCw className="spin" aria-hidden="true" /> Actualizando…</span> : null}
          <a className="project-command" href={projectArchiveUrl(threadId)} download>
            <Archive aria-hidden="true" /> Descargar ZIP
          </a>
        </div>
      </header>
      <div className="project-metadata">
        <span>Framework: {summary.detected_framework ?? "n/a"}</span>
        <span>Tests: {summary.detected_test_framework ?? "n/a"}</span>
        <span title={summary.updated_at ?? undefined}>Actualizado: {summary.updated_at ? new Date(summary.updated_at).toLocaleString() : "n/a"}</span>
      </div>
      {!tree || !(tree.root.children?.length) ? (
        <div className="project-empty"><h2>El proyecto no contiene archivos visibles.</h2></div>
      ) : (
        <div className="project-explorer-body">
          <aside className="project-tree-panel">
            <h3>Árbol</h3>
            <ProjectFileTree root={tree.root} selectedPath={selectedPath} onSelect={selectFile} />
            {tree.truncated ? <p className="tree-warning">Árbol truncado por el límite configurado.</p> : null}
          </aside>
          <div className="project-viewer">
            {!selectedNode ? (
              <div className="project-empty compact">
                <FileQuestion aria-hidden="true" />
                Selecciona un archivo para verlo.
              </div>
            ) : (
              <>
                <header className="project-file-header">
                  <div>
                    <h3>{selectedNode.name}</h3>
                    <code>{selectedNode.path}</code>
                    <div className="file-metadata">
                      <span>{selectedNode.language ?? selectedNode.content_type}</span>
                      <span title={`${selectedNode.size_bytes ?? 0} bytes`}>
                        {formatSize(selectedNode.size_bytes ?? 0)}
                      </span>
                      {content ? <span>{content.line_count} líneas</span> : null}
                      {selectedNode.is_generated ? <strong>Nuevo</strong> : null}
                      {selectedNode.is_updated ? <strong>Modificado</strong> : null}
                    </div>
                  </div>
                  <div className="project-file-actions">
                    {selectedNode.content_available ? (
                      <button type="button" onClick={() => void copy()} disabled={!content}>
                        {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
                        {copied ? "Copiado" : "Copiar"}
                      </button>
                    ) : null}
                    <a href={projectFileDownloadUrl(threadId, selectedNode.path)} download>
                      <Download aria-hidden="true" /> Descargar
                    </a>
                  </div>
                </header>
                {copyFailed ? <p className="file-error" role="alert">No se pudo copiar el contenido.</p> : null}
                {fileLoading ? (
                  <div className="project-empty compact" role="status">
                    <LoaderCircle className="spin" aria-hidden="true" /> Cargando archivo…
                  </div>
                ) : fileError ? (
                  <ErrorBanner error={fileError} onRetry={() => void loadFile(selectedNode.path, true)} onDismiss={() => setFileError(null)} />
                ) : content ? (
                  <SyntaxHighlightedCode content={content.content} language={content.language} />
                ) : (
                  <div className="project-empty compact">
                    <FileQuestion aria-hidden="true" />
                    <p>Este archivo no puede previsualizarse.</p>
                    <a className="project-command" href={projectFileDownloadUrl(threadId, selectedNode.path)} download>
                      <Download aria-hidden="true" /> Descargar
                    </a>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
