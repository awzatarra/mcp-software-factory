import { LoaderCircle } from "lucide-react";
import { useMemo } from "react";
import { useParams } from "react-router-dom";
import { useSearchParams } from "react-router-dom";

import { ApprovalPanel } from "../components/ApprovalPanel";
import { ErrorBanner } from "../components/ErrorBanner";
import { EventTimeline } from "../components/EventTimeline";
import { StageProgress } from "../components/StageProgress";
import { WorkflowHeader } from "../components/WorkflowHeader";
import { WorkflowSummary } from "../components/WorkflowSummary";
import { ProjectExplorer } from "../components/ProjectExplorer/ProjectExplorer";
import { WorkflowExecutionTab } from "../components/WorkflowExecution/WorkflowExecutionTab";
import { WorkflowEvaluationTab } from "../components/WorkflowEvaluation/WorkflowEvaluationTab";
import { useWorkflow } from "../hooks/useWorkflow";
import { useWorkflowEvents } from "../hooks/useWorkflowEvents";
import { useWorkflowPollingFallback } from "../hooks/useWorkflowPollingFallback";
import { useWorkflowStore } from "../stores/workflowStore";
import { deriveStageProgress } from "../utils/stageDerivation";
import { ActiveWorkflowAlerts } from "../components/Alerts/ActiveWorkflowAlerts";
import { WorkflowGitTab } from "../components/WorkflowGit/WorkflowGitTab";
import { WorkflowCITab } from "../components/WorkflowCI/WorkflowCITab";

export function WorkflowPage() {
  const { threadId = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = searchParams.get("tab") ?? "timeline";
  const selectedFile = searchParams.get("file");
  const selectedTask = searchParams.get("task");
  const selectedEvent = searchParams.get("event");
  const selectedCategory = searchParams.get("category");
  const selectedFinding = searchParams.get("finding");
  const branchId = searchParams.get("branch_id") ?? "original";
  const snapshot = useWorkflowStore((state) => state.snapshot);
  const events = useWorkflowStore((state) => state.events);
  const status = useWorkflowStore((state) => state.connectionStatus);
  const loading = useWorkflowStore((state) => state.isLoading);
  const error = useWorkflowStore((state) => state.error);
  const eventStreamVersion = useWorkflowStore(
    (state) => state.eventStreamVersion,
  );
  const setError = useWorkflowStore((state) => state.setError);
  const { reload } = useWorkflow(threadId, branchId);
  const terminal =
    snapshot !== null &&
    !["pending", "running"].includes(snapshot.terminal_status);
  useWorkflowEvents(
    threadId,
    Boolean(threadId) &&
      !terminal &&
      !loading &&
      snapshot?.thread_id === threadId,
    eventStreamVersion,
    branchId,
  );
  useWorkflowPollingFallback(
    threadId,
    status === "disconnected" || status === "error",
  );
  const stages = useMemo(
    () => deriveStageProgress(events, snapshot),
    [events, snapshot],
  );
  const selectTab = (nextTab: string) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      if (nextTab === "timeline") next.delete("tab");
      else next.set("tab", nextTab);
      if (nextTab !== "project") next.delete("file");
      if (nextTab !== "execution") {
        next.delete("task");
        next.delete("section");
      }
      if (nextTab !== "timeline") next.delete("event");
      if (nextTab !== "evaluation") {
        next.delete("category");
        next.delete("finding");
      }
      return next;
    });
  };
  const selectFile = (path: string | null) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      next.set("tab", "project");
      if (path) next.set("file", path);
      else next.delete("file");
      next.delete("category");
      next.delete("finding");
      return next;
    });
  };
  const selectTask = (taskId: string | null) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      next.set("tab", "execution");
      next.set("section", "tasks");
      if (taskId) next.set("task", taskId);
      else next.delete("task");
      next.delete("category");
      next.delete("finding");
      return next;
    });
  };
  const openEvent = (eventId: string) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      next.set("tab", "timeline");
      next.set("event", eventId);
      next.delete("task");
      next.delete("section");
      next.delete("category");
      next.delete("finding");
      return next;
    });
  };
  const selectEvaluationCategory = (category: string | null) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      next.set("tab", "evaluation");
      if (category) next.set("category", category);
      else next.delete("category");
      return next;
    });
  };
  const selectEvaluationFinding = (finding: string | null) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      next.set("tab", "evaluation");
      if (finding) next.set("finding", finding);
      else next.delete("finding");
      return next;
    });
  };

  return (
    <main className="workflow-page">
      <WorkflowHeader
        threadId={threadId}
        snapshot={snapshot}
        connectionStatus={status}
      />
      <StageProgress stages={stages} />
      <ActiveWorkflowAlerts threadId={threadId} branchId={branchId} />
      <nav className="workflow-tabs" aria-label="Vistas del workflow">
        <button type="button" aria-selected={tab === "overview"} onClick={() => selectTab("overview")}>Resumen</button>
        <button type="button" aria-selected={tab === "execution"} onClick={() => selectTab("execution")}>Ejecución</button>
        <button type="button" aria-selected={tab === "evaluation"} onClick={() => selectTab("evaluation")}>Evaluación</button>
        <button type="button" aria-selected={tab === "timeline"} onClick={() => selectTab("timeline")}>Timeline</button>
        <button type="button" aria-selected={tab === "project"} onClick={() => selectTab("project")}>Proyecto</button>
        <button type="button" aria-selected={tab === "git"} onClick={() => selectTab("git")}>Git</button>
        <button type="button" aria-selected={tab === "ci"} onClick={() => selectTab("ci")}>CI</button>
      </nav>
      {error ? (
        <div className="page-banner">
          <ErrorBanner
            error={error}
            onRetry={error.retryable ? () => void reload() : undefined}
            onDismiss={() => setError(null)}
          />
        </div>
      ) : null}
      {loading && !snapshot ? (
        <div className="loading-state" role="status">
          <LoaderCircle className="spin" aria-hidden="true" />
          Recuperando snapshot e historial…
        </div>
      ) : tab === "project" ? (
        <div className="project-page-content">
          <ProjectExplorer
            threadId={threadId}
            events={events}
            selectedFile={selectedFile}
            onSelectFile={selectFile}
          />
        </div>
      ) : tab === "execution" ? (
        <div className="execution-page-content">
          <WorkflowExecutionTab
            threadId={threadId}
            branchId={branchId}
            events={events}
            selectedTask={selectedTask}
            onSelectTask={selectTask}
            onOpenEvent={openEvent}
            onOpenFile={selectFile}
          />
        </div>
      ) : tab === "git" ? (
        <div className="git-page-content"><WorkflowGitTab threadId={threadId} /></div>
      ) : tab === "ci" ? (
        <div className="git-page-content"><WorkflowCITab threadId={threadId} /></div>
      ) : tab === "evaluation" ? (
        <div className="evaluation-page-content">
          <WorkflowEvaluationTab
            threadId={threadId}
            branchId={branchId}
            events={events}
            selectedCategory={selectedCategory}
            selectedFinding={selectedFinding}
            onSelectCategory={selectEvaluationCategory}
            onSelectFinding={selectEvaluationFinding}
            onOpenTask={(taskId) => selectTask(taskId)}
            onOpenEvent={openEvent}
            onOpenFile={(path) => selectFile(path)}
          />
        </div>
      ) : tab === "overview" ? (
        <div className="overview-page-content">
          {snapshot ? (
            <>
              <ApprovalPanel snapshot={snapshot} events={events} />
              <WorkflowSummary snapshot={snapshot} />
            </>
          ) : null}
        </div>
      ) : (
        <div className="workflow-layout">
          <EventTimeline
            events={events}
            snapshot={snapshot}
            selectedEventId={selectedEvent}
          />
          <aside className="workflow-sidebar">
            {snapshot ? (
              <>
                <ApprovalPanel snapshot={snapshot} events={events} />
                <WorkflowSummary snapshot={snapshot} />
              </>
            ) : null}
          </aside>
        </div>
      )}
    </main>
  );
}
