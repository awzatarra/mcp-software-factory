import { Check, GitBranch as GitBranchIcon, History, LoaderCircle, X } from "lucide-react";
import { useEffect, useState } from "react";
import { approveWorkflow, getWorkflowGit, getWorkflowGitBranches, getWorkflowGitDiff, getWorkflowGitLog, getWorkflowGitStatus, prepareWorkflowGitPromotion, rejectWorkflow } from "../../api/workflows";
import type { GitBranches, GitDiff, GitLogEntry, GitRepositoryInfo, GitStatus } from "../../api/types";

interface GitViewData { repository: GitRepositoryInfo; status: GitStatus | null; diff: GitDiff | null; stagedDiff: GitDiff | null; log: GitLogEntry[]; branches: GitBranches | null; }
const humanDate = (value: string) => new Intl.DateTimeFormat("es-PE", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
const isInfrastructureIgnored = (path: string) => {
  const parts = path.replace(/\\/g, "/").split("/").filter(Boolean);
  return parts.includes(".venv") || parts.includes("__pycache__") || parts.includes(".pytest_cache") || parts.at(-1) === ".coverage";
};

export function WorkflowGitTab({ threadId }: { threadId: string }) {
  const [result, setResult] = useState<{ threadId: string; data: GitViewData } | null>(null);
  const [failure, setFailure] = useState<{ threadId: string; message: string } | null>(null);
  const [resolving, setResolving] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void getWorkflowGit(threadId, controller.signal).then(async (repository) => {
      if (!repository.is_repository) { setResult({ threadId, data: { repository, status: null, diff: null, stagedDiff: null, log: [], branches: null } }); return; }
      const [status, diff, stagedDiff, log, branches] = await Promise.all([
        getWorkflowGitStatus(threadId, controller.signal), getWorkflowGitDiff(threadId, false, controller.signal),
        getWorkflowGitDiff(threadId, true, controller.signal),
        getWorkflowGitLog(threadId, 20, controller.signal), getWorkflowGitBranches(threadId, controller.signal),
      ]);
      setResult({ threadId, data: { repository, status, diff, stagedDiff, log, branches } });
    }).catch((reason: unknown) => { if (!controller.signal.aborted) setFailure({ threadId, message: reason instanceof Error ? reason.message : "No se pudo consultar Git." }); });
    return () => controller.abort();
  }, [threadId]);
  const data = result?.threadId === threadId ? result.data : null;
  const error = failure?.threadId === threadId ? failure.message : null;
  if (error) return <div className="git-empty" role="alert">{error}</div>;
  if (!data) return <div className="git-empty" role="status"><LoaderCircle className="spin" aria-hidden="true" />Consultando repositorio...</div>;
  if (!data.repository.is_repository) return <section className="git-empty" aria-label="Git"><div><h2>Git</h2><p>El proyecto no es un repositorio Git.</p></div></section>;
  const visibleUntracked = (data.status?.untracked ?? []).filter((path) => !isInfrastructureIgnored(path));
  const allDiffFiles = [...(data.diff?.files ?? []), ...(data.stagedDiff?.files ?? [])].filter((file) => !isInfrastructureIgnored(file.path));
  const added = new Set(allDiffFiles.filter((file) => file.status === "added").map((file) => file.path));
  const modified = new Set([...(data.status?.modified ?? []).filter((path) => !isInfrastructureIgnored(path)), ...allDiffFiles.filter((file) => file.status === "modified").map((file) => file.path)]);
  const deleted = (data.status?.deleted ?? []).filter((path) => !isInfrastructureIgnored(path));
  const staged = (data.status?.staged ?? []).filter((path) => !isInfrastructureIgnored(path));
  const relevantClean = data.status?.effective_clean ?? data.repository.effective_clean ?? data.status?.clean ?? data.repository.clean ?? false;
  const changeGroup = (label: string, paths: string[]) => <div className="git-change-group"><h3>{label} <span>{paths.length}</span></h3>{paths.length ? <ul>{paths.map((path) => <li key={`${label}-${path}`}><code>{path}</code></li>)}</ul> : <p>n/a</p>}</div>;
  const resolveCommit = async (approved: boolean) => {
    setResolving(true);
    try {
      await (approved ? approveWorkflow : rejectWorkflow)(threadId, approved ? "Commit approved" : "Commit rejected");
      const repository = await getWorkflowGit(threadId);
      setResult((current) => current?.threadId === threadId ? { threadId, data: { ...current.data, repository } } : current);
    } catch (reason) {
      setFailure({ threadId, message: reason instanceof Error ? reason.message : "No se pudo resolver el commit." });
    } finally { setResolving(false); }
  };
  const preparePromotion = async () => {
    setResolving(true);
    try {
      await prepareWorkflowGitPromotion(threadId);
      const repository = await getWorkflowGit(threadId);
      setResult((current) => current?.threadId === threadId ? { threadId, data: { ...current.data, repository } } : current);
    } catch (reason) {
      setFailure({ threadId, message: reason instanceof Error ? reason.message : "No se pudo preparar la promoción." });
    } finally { setResolving(false); }
  };
  const promotion = data.repository.promotion;
  const promotionPreview = promotion?.preview;
  const ci = promotionPreview?.ci ?? data.repository.ci_eligibility ?? null;
  const ciBlocksPromotion = Boolean(ci?.required && !ci.eligible);
  return <div className="git-view" aria-label="Git del workflow">
    <section className="git-repository"><div className="section-heading"><GitBranchIcon aria-hidden="true" /><div><h2>Repository</h2><p>Estado local de solo lectura</p></div></div><dl className="git-facts">
      <div><dt>Branch</dt><dd>{data.repository.current_branch ?? "detached HEAD"}</dd></div>
      <div><dt>HEAD</dt><dd><code title={data.repository.head_commit ?? undefined}>{data.repository.head_commit?.slice(0, 12) ?? "n/a"}</code></dd></div>
      <div><dt>Working tree Git</dt><dd><span className={`status-badge ${data.status?.clean ? "success" : "warning"}`}>{data.status?.clean ? "CLEAN" : "CHANGES"}</span></dd></div>
      <div><dt>Cambios relevantes</dt><dd><span className={`status-badge ${relevantClean ? "success" : "warning"}`}>{relevantClean ? "CLEAN" : "CHANGES"}</span></dd></div>
      <div><dt>Local branches</dt><dd>{data.branches?.branches.length ?? 0}</dd></div>
      <div><dt>Base branch</dt><dd>{data.repository.base_branch ?? "n/a"}</dd></div>
      <div><dt>Workflow branch</dt><dd>{data.repository.workflow_branch ?? "n/a"}</dd></div>
      <div><dt>CI Required</dt><dd>{ci?.required ? "YES" : "NO"}</dd></div>
      <div><dt>Promotion Eligible</dt><dd><span className={`status-badge ${ci?.eligible ? "success" : ci?.required ? "danger" : "warning"}`}>{ci?.eligible ? "YES" : ci?.required ? "NO" : "n/a"}</span></dd></div>
    </dl></section>
    <section className="git-changes"><h2>Changes</h2><div className="git-change-grid">
      {changeGroup("Modified", [...modified].sort())}{changeGroup("Added", [...added].sort())}{changeGroup("Deleted", deleted)}{changeGroup("Untracked", visibleUntracked)}{changeGroup("Staged", staged)}
    </div>{data.diff?.truncated || data.stagedDiff?.truncated ? <p className="git-truncated">Diff truncado por el límite configurado.</p> : null}</section>
    {data.repository.commit_preview ? <section className="git-commit-preview"><h2>Commit preview</h2><dl className="git-facts">
      <div><dt>Message</dt><dd>{data.repository.commit_preview.proposed_message}</dd></div><div><dt>Files</dt><dd>{data.repository.commit_preview.staged_files.length}</dd></div>
      <div><dt>Additions / deletions</dt><dd>+{data.repository.commit_preview.additions} / -{data.repository.commit_preview.deletions}</dd></div>
      <div><dt>Fingerprint</dt><dd><code title={data.repository.commit_preview.diff_fingerprint}>{data.repository.commit_preview.diff_fingerprint.slice(0, 12)}</code></dd></div>
    </dl>{data.repository.commit_status === "awaiting_approval" ? <div className="approval-actions"><button className="approve-button" disabled={resolving} onClick={() => void resolveCommit(true)}><Check aria-hidden="true" />Approve commit</button><button className="reject-button" disabled={resolving} onClick={() => void resolveCommit(false)}><X aria-hidden="true" />Reject</button></div> : <span className="status-badge">{data.repository.commit_status ?? "n/a"}</span>}</section> : null}
    <section className="git-commit-preview" aria-label="Promotion">
      <h2>Promotion</h2>
      {promotionPreview ? <>
        <dl className="git-facts">
          <div><dt>Base branch</dt><dd>{promotionPreview.base_branch}</dd></div>
          <div><dt>Workflow branch</dt><dd>{promotionPreview.workflow_branch}</dd></div>
          <div><dt>Base commit</dt><dd><code title={promotionPreview.current_base_commit ?? undefined}>{promotionPreview.current_base_commit?.slice(0, 12) ?? "n/a"}</code></dd></div>
          <div><dt>Workflow HEAD</dt><dd><code title={promotionPreview.workflow_head}>{promotionPreview.workflow_head.slice(0, 12)}</code></dd></div>
          <div><dt>Base advanced</dt><dd>{promotionPreview.base_advanced ? "YES" : "NO"}</dd></div>
          <div><dt>Ahead / behind</dt><dd>{promotionPreview.commits_ahead} / {promotionPreview.commits_behind}</dd></div>
          <div><dt>Files changed</dt><dd>{promotionPreview.files_changed.length}</dd></div>
          <div><dt>Strategy</dt><dd>{promotionPreview.merge_strategy_candidate}</dd></div>
          <div><dt>CI run</dt><dd><code title={promotionPreview.ci?.run_id ?? undefined}>{promotionPreview.ci?.run_id?.slice(0, 12) ?? "n/a"}</code></dd></div>
          <div><dt>CI decision</dt><dd>{promotionPreview.ci?.ci_decision ?? "n/a"}</dd></div>
          <div><dt>Validated commit</dt><dd><code title={promotionPreview.ci?.source_commit ?? undefined}>{promotionPreview.ci?.source_commit?.slice(0, 12) ?? "n/a"}</code></dd></div>
        </dl>
        {promotionPreview.conflict_state === "conflicts" ? <div className="git-change-group" role="alert"><h3>Promotion blocked</h3><p>Conflicting files</p><ul>{promotionPreview.conflicting_files.map((path) => <li key={path}><code>{path}</code></li>)}</ul></div> : null}
        {promotion?.state === "awaiting_approval" && promotionPreview.ready ? <div className="approval-actions"><button className="approve-button" disabled={resolving} onClick={() => void resolveCommit(true)}><Check aria-hidden="true" />Approve promotion</button><button className="reject-button" disabled={resolving} onClick={() => void resolveCommit(false)}><X aria-hidden="true" />Reject promotion</button></div> : null}
        {promotion?.result ? <dl className="git-facts"><div><dt>Merged commit</dt><dd><code title={promotion.result.result_commit}>{promotion.result.result_commit.slice(0, 12)}</code></dd></div><div><dt>Strategy</dt><dd>{promotion.result.strategy}</dd></div><div><dt>Completed</dt><dd>{humanDate(promotion.result.completed_at)}</dd></div></dl> : null}
        <span className="status-badge">{promotion?.state ?? promotionPreview.state}</span>
      </> : <>
        {ciBlocksPromotion ? <p role="alert">Promotion blocked: {ci?.reason ?? "ci_required_for_promotion"}</p> : null}
        <button type="button" className="approve-button" disabled={resolving || data.repository.commit_status !== "committed" || ciBlocksPromotion} onClick={() => void preparePromotion()}><GitBranchIcon aria-hidden="true" />Prepare promotion</button>
      </>}
    </section>
    <section className="git-history"><div className="section-heading"><History aria-hidden="true" /><h2>History</h2></div>{data.log.length ? <div className="table-scroll"><table><thead><tr><th>Commit</th><th>Subject</th><th>Author</th><th>Date</th></tr></thead><tbody>
      {data.log.map((entry) => <tr key={entry.commit}><td><code title={entry.commit}>{entry.short_commit}</code></td><td>{entry.subject}</td><td title={entry.author_email}>{entry.author_name}</td><td>{humanDate(entry.timestamp)}</td></tr>)}
    </tbody></table></div> : <p>No hay commits disponibles.</p>}</section>
  </div>;
}
