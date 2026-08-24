import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";

import * as workflows from "../../api/workflows";
import { WorkflowGitTab } from "./WorkflowGitTab";


describe("WorkflowGitTab", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("shows not_repository without requesting repository details", async () => {
    vi.spyOn(workflows, "getWorkflowGit").mockResolvedValue({
      state: "not_repository", is_repository: false, repository_root: null,
      current_branch: null, head_commit: null, detached_head: false, clean: null,
    });
    const status = vi.spyOn(workflows, "getWorkflowGitStatus");
    render(<WorkflowGitTab threadId="thread-1" />);
    expect(await screen.findByText("El proyecto no es un repositorio Git.")).toBeVisible();
    expect(status).not.toHaveBeenCalled();
  });

  it("renders repository, changes and local history without mutation actions", async () => {
    vi.spyOn(workflows, "getWorkflowGit").mockResolvedValue({
      state: "available", is_repository: true, repository_root: "health-api",
      current_branch: "main", head_commit: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      detached_head: false, clean: false, effective_clean: false,
    });
    vi.spyOn(workflows, "getWorkflowGitStatus").mockResolvedValue({
      branch: "main", clean: false, effective_clean: false, staged: ["src/staged.py"], modified: ["README.md"],
      untracked: ["tests/new_test.py"], deleted: ["old.py"],
    });
    vi.spyOn(workflows, "getWorkflowGitDiff").mockResolvedValue({
      staged: false, truncated: false, total_bytes: 80,
      files: [
        { path: "README.md", status: "modified", additions: 1, deletions: 0, patch: "private patch" },
        { path: "src/new.py", status: "added", additions: 3, deletions: 0, patch: "private patch" },
      ],
    });
    vi.spyOn(workflows, "getWorkflowGitLog").mockResolvedValue([{
      commit: "a".repeat(40), short_commit: "aaaaaaa", author_name: "Test User",
      author_email: "test@example.com", timestamp: "2026-08-10T00:00:00Z", subject: "Initial commit",
    }]);
    vi.spyOn(workflows, "getWorkflowGitBranches").mockResolvedValue({
      current: "main", branches: [{ name: "main", current: true, commit: "a".repeat(40) }],
    });
    render(<WorkflowGitTab threadId="thread-1" />);
    const view = await screen.findByLabelText("Git del workflow");
    expect(within(view).getByText("main")).toBeVisible();
    expect(within(view).getByText("README.md")).toBeVisible();
    expect(within(view).getByText("src/new.py")).toBeVisible();
    expect(within(view).getByText("old.py")).toBeVisible();
    expect(within(view).getByText("tests/new_test.py")).toBeVisible();
    expect(within(view).getByText("src/staged.py")).toBeVisible();
    expect(within(view).getByText("Initial commit")).toBeVisible();
    expect(screen.queryByRole("button", { name: /commit|push|checkout|reset/i })).not.toBeInTheDocument();
    expect(screen.queryByText("private patch")).not.toBeInTheDocument();
  });

  it("separates Git dirty state from relevant workflow changes", async () => {
    vi.spyOn(workflows, "getWorkflowGit").mockResolvedValue({
      state: "available", is_repository: true, repository_root: "health-api",
      current_branch: "main", head_commit: "a".repeat(40),
      detached_head: false, clean: false, effective_clean: true,
    });
    vi.spyOn(workflows, "getWorkflowGitStatus").mockResolvedValue({
      branch: "main", clean: false, effective_clean: true,
      staged: [], modified: [], untracked: [".venv/"], deleted: [],
    });
    vi.spyOn(workflows, "getWorkflowGitDiff").mockResolvedValue({ staged: false, truncated: false, total_bytes: 0, files: [] });
    vi.spyOn(workflows, "getWorkflowGitLog").mockResolvedValue([]);
    vi.spyOn(workflows, "getWorkflowGitBranches").mockResolvedValue({ current: "main", branches: [] });

    render(<WorkflowGitTab threadId="thread-1" />);

    const view = await screen.findByLabelText("Git del workflow");
    expect(within(view).getByText("Working tree Git")).toBeVisible();
    expect(within(view).getByText("Cambios relevantes")).toBeVisible();
    expect(within(view).getByText("CHANGES")).toBeVisible();
    expect(within(view).getByText("CLEAN")).toBeVisible();
    expect(screen.queryByText(".venv/")).not.toBeInTheDocument();
  });

  it("renders a sanitized commit preview and approves through the existing workflow endpoint", async () => {
    const repository = {
      state: "available" as const, is_repository: true, repository_root: "health-api",
      current_branch: "workflow/a2b27b16", head_commit: "a".repeat(40),
      detached_head: false, clean: false, base_branch: "main",
      workflow_branch: "workflow/a2b27b16", commit_status: "awaiting_approval",
      commits: [], commit_preview: {
        approval_id: "approval-1", branch: "workflow/a2b27b16",
        staged_files: ["README.md"], additions: 2, deletions: 1,
        diff_fingerprint: "f".repeat(64), proposed_message: "feat: validated change",
        ready: true, status: "awaiting_approval" as const,
      },
    };
    vi.spyOn(workflows, "getWorkflowGit")
      .mockResolvedValueOnce(repository)
      .mockResolvedValueOnce({ ...repository, commit_status: "committed" });
    vi.spyOn(workflows, "getWorkflowGitStatus").mockResolvedValue({ branch: repository.current_branch, clean: false, staged: ["README.md"], modified: [], untracked: [], deleted: [] });
    vi.spyOn(workflows, "getWorkflowGitDiff").mockResolvedValue({ staged: false, truncated: false, total_bytes: 0, files: [] });
    vi.spyOn(workflows, "getWorkflowGitLog").mockResolvedValue([]);
    vi.spyOn(workflows, "getWorkflowGitBranches").mockResolvedValue({ current: repository.current_branch, branches: [] });
    const approve = vi.spyOn(workflows, "approveWorkflow").mockResolvedValue({ thread_id: "thread-1", accepted: true, operation: "git_commit", tool_name: "git__commit", status: "completed" });

    render(<WorkflowGitTab threadId="thread-1" />);
    expect(await screen.findByText("feat: validated change")).toBeVisible();
    expect(screen.getByText("+2 / -1")).toBeVisible();
    expect(screen.queryByText("f".repeat(64))).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve commit" }));
    expect(approve).toHaveBeenCalledWith("thread-1", "Commit approved");
    expect(await screen.findByText("committed")).toBeVisible();
  });

  it("renders promotion conflicts without approval and approves a ready promotion", async () => {
    const base = {
      state: "available" as const, is_repository: true, repository_root: "health-api",
      current_branch: "workflow/a2b27b16", head_commit: "b".repeat(40),
      detached_head: false, clean: true, base_branch: "main",
      workflow_branch: "workflow/a2b27b16", commit_status: "committed",
    };
    const preview = {
      promotion_id: "promotion-1", approval_id: "approval-promotion", state: "conflicts",
      base_branch: "main", base_commit_at_branch_creation: "a".repeat(40),
      current_base_commit: "c".repeat(40), base_advanced: true,
      workflow_branch: "workflow/a2b27b16", workflow_head: "b".repeat(40),
      commits_ahead: 1, commits_behind: 1, commits: ["b".repeat(40)],
      files_changed: ["README.md"], additions: 1, deletions: 1,
      conflict_state: "conflicts" as const, conflicting_files: ["README.md"],
      merge_strategy_candidate: "blocked" as const, promotion_fingerprint: "p".repeat(64), ready: false,
    };
    vi.spyOn(workflows, "getWorkflowGit").mockResolvedValue({
      ...base, promotion: { state: "conflicts", promotion_id: "promotion-1", approval_id: null, preview, result: null, rejection_reason: null },
    });
    vi.spyOn(workflows, "getWorkflowGitStatus").mockResolvedValue({ branch: base.current_branch, clean: true, staged: [], modified: [], untracked: [], deleted: [] });
    vi.spyOn(workflows, "getWorkflowGitDiff").mockResolvedValue({ staged: false, truncated: false, total_bytes: 0, files: [] });
    vi.spyOn(workflows, "getWorkflowGitLog").mockResolvedValue([]);
    vi.spyOn(workflows, "getWorkflowGitBranches").mockResolvedValue({ current: base.current_branch, branches: [] });
    render(<WorkflowGitTab threadId="thread-1" />);
    expect(await screen.findByText("Promotion blocked")).toBeVisible();
    expect(screen.getByText("README.md")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Approve promotion" })).not.toBeInTheDocument();
  });
});
