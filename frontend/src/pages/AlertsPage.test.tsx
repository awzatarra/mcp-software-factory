import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AlertsPage } from "./AlertsPage";

const api = vi.hoisted(() => ({ list: vi.fn(), summary: vi.fn(), detail: vi.fn(), rules: vi.fn(), update: vi.fn(), reset: vi.fn(), action: vi.fn() }));
vi.mock("../api/alerts", () => ({ getAlerts: api.list, getAlertSummary: api.summary, getAlertDetail: api.detail, getAlertRules: api.rules, updateAlertRule: api.update, resetAlertRule: api.reset, actOnAlert: api.action }));

const alert = { alert_id: "alert-1", rule_id: "builtin:tests_failed", rule_code: "TESTS_FAILED", thread_id: "thread-1", branch_id: "original", project_name: "checkout-api", category: "testing", severity: "error", status: "open", title: "Tests failed", message: "The latest test execution did not pass.", fingerprint: "fingerprint", first_detected_at: "2026-08-03T10:00:00Z", last_detected_at: "2026-08-03T10:10:00Z", occurrence_count: 1, acknowledged_at: null, acknowledged_by: null, resolved_at: null, resolved_by: null, resolution_note: null, muted_until: null, current_evidence: { metric: "tests_failed", actual_value: false, threshold_value: true, related_task_id: "qa", related_event_id: "event-1", related_file: "tests/test_health.py", pending_operation: null }, primary_event_id: "event-1", related_task_id: "qa", related_file: "tests/test_health.py", created_at: "2026-08-03T10:00:00Z", updated_at: "2026-08-03T10:10:00Z" };
const summary = { open_total: 1, acknowledged_total: 0, resolved_total: 0, muted_total: 0, critical_open: 0, error_open: 1, warning_open: 0, info_open: 0, affected_workflows: 1, top_rules: [], average_time_to_acknowledge_seconds: null, average_time_to_resolve_seconds: null, oldest_open_alert_seconds: 600, calculated_at: "2026-08-03T10:10:00Z" };

beforeEach(() => {
  api.list.mockResolvedValue({ items: [alert], total: 1, limit: 50, offset: 0, has_more: false, counts: { open: 1, acknowledged: 0, resolved: 0, muted: 0, critical: 0, error: 1, warning: 0, info: 0 } });
  api.summary.mockResolvedValue(summary); api.action.mockResolvedValue({ ...alert, status: "acknowledged" });
  api.rules.mockResolvedValue({ items: [], total: 0 });
  api.detail.mockResolvedValue({ alert, rule: { code: "TESTS_FAILED" }, occurrences: [], actions: [], navigation: {} });
});

describe("AlertsPage", () => {
  it("renders durable alerts, severity, status and navigation", async () => {
    render(<MemoryRouter><AlertsPage /></MemoryRouter>);
    expect(await screen.findByText("Tests failed")).toBeInTheDocument();
    expect(document.querySelector(".alert-severity")).toHaveTextContent("error");
    expect(document.querySelector(".alert-status")).toHaveTextContent("open");
    expect(screen.getByRole("link", { name: "Ver workflow" })).toHaveAttribute("href", "/workflows/thread-1");
    expect(screen.getByRole("link", { name: "Ver tarea" })).toHaveAttribute("href", "/workflows/thread-1?tab=execution&task=qa");
    expect(screen.getByRole("link", { name: "Ver en Timeline" })).toHaveAttribute("href", "/workflows/thread-1?tab=timeline&event=event-1");
    expect(screen.getByRole("link", { name: "Ver archivo" })).toHaveAttribute("href", "/workflows/thread-1?tab=project&file=tests%2Ftest_health.py");
  });

  it("persists filters in the URL and reloads", async () => {
    const user = userEvent.setup(); render(<MemoryRouter initialEntries={["/alerts"]}><AlertsPage /></MemoryRouter>);
    await screen.findByText("Tests failed");
    await user.selectOptions(screen.getByLabelText("Severidad"), "error");
    await waitFor(() => expect(api.list.mock.calls.at(-1)?.[0].get("severity")).toBe("error"));
  });

  it("acknowledges once with an optimistic state", async () => {
    const user = userEvent.setup(); render(<MemoryRouter><AlertsPage /></MemoryRouter>);
    await user.click(await screen.findByRole("button", { name: /Reconocer/ }));
    expect(api.action).toHaveBeenCalledWith("alert-1", "acknowledge", undefined);
  });

  it("renders loading, empty and offline states", async () => {
    api.list.mockResolvedValueOnce({ items: [], total: 0, limit: 50, offset: 0, has_more: false, counts: {} });
    const { unmount } = render(<MemoryRouter><AlertsPage /></MemoryRouter>);
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(await screen.findByText(/No hay alertas/)).toBeInTheDocument(); unmount();
    api.list.mockRejectedValueOnce(new Error("API offline")); render(<MemoryRouter><AlertsPage /></MemoryRouter>);
    expect(await screen.findByRole("alert")).toHaveTextContent("API offline");
  });

  it.each(["README.md", "requirements.txt"])("does not link non-test evidence %s", async (relatedFile) => {
    api.list.mockResolvedValueOnce({
      items: [{ ...alert, rule_code: "HIGH_WARNING_COUNT", related_file: relatedFile }],
      total: 1, limit: 50, offset: 0, has_more: false, counts: {},
    });
    render(<MemoryRouter><AlertsPage /></MemoryRouter>);
    await screen.findByText("Tests failed");
    expect(screen.queryByRole("link", { name: "Ver archivo" })).not.toBeInTheDocument();
  });

  it("keeps branch identity in all testing navigation links", async () => {
    api.list.mockResolvedValueOnce({ items: [{ ...alert, branch_id: "fork-1" }], total: 1, limit: 50, offset: 0, has_more: false, counts: {} });
    render(<MemoryRouter><AlertsPage /></MemoryRouter>);
    await screen.findByText("Tests failed");
    expect(screen.getByRole("link", { name: "Ver tarea" })).toHaveAttribute("href", expect.stringContaining("branch_id=fork-1"));
    expect(screen.getByRole("link", { name: "Ver en Timeline" })).toHaveAttribute("href", expect.stringContaining("branch_id=fork-1"));
    expect(screen.getByRole("link", { name: "Ver archivo" })).toHaveAttribute("href", expect.stringContaining("branch_id=fork-1"));
  });

  it("removes an auto-resolved alert from the active view after refresh", async () => {
    api.list
      .mockResolvedValueOnce({ items: [alert], total: 1, limit: 50, offset: 0, has_more: false, counts: {} })
      .mockResolvedValueOnce({ items: [], total: 0, limit: 50, offset: 0, has_more: false, counts: {} });
    const user = userEvent.setup();
    render(<MemoryRouter><AlertsPage /></MemoryRouter>);
    await screen.findByText("Tests failed");
    await user.click(screen.getByRole("button", { name: "Actualizar alertas" }));
    await waitFor(() => expect(screen.queryByText("Tests failed")).not.toBeInTheDocument());
  });

  it("restores the resolved filter and renders auto-resolve audit history", async () => {
    const resolved = { ...alert, status: "resolved", resolved_by: "system" };
    api.list.mockResolvedValueOnce({ items: [resolved], total: 1, limit: 50, offset: 0, has_more: false, counts: {} });
    api.detail.mockResolvedValueOnce({
      alert: resolved,
      rule: { code: "TESTS_FAILED" },
      occurrences: [],
      actions: [{ action_id: "action-1", action: "auto_resolve", actor: "system", note: "Condition cleared", previous_status: "acknowledged", new_status: "resolved", created_at: "2026-08-03T10:20:00Z" }],
      navigation: {},
    });
    const user = userEvent.setup();
    render(<MemoryRouter initialEntries={["/alerts?status=resolved"]}><AlertsPage /></MemoryRouter>);
    await screen.findByText("Tests failed");
    expect(api.list.mock.calls.at(-1)?.[0].get("status")).toBe("resolved");
    await user.click(screen.getByRole("button", { name: "Detalle" }));
    expect(await screen.findByText(/auto_resolve · system/)).toBeVisible();
  });

  it("does not collapse alerts with different operation fingerprints", async () => {
    api.list.mockResolvedValueOnce({
      items: [
        { ...alert, alert_id: "approval-create", rule_code: "APPROVAL_WAIT_TOO_LONG", category: "approval", title: "Create approval", message: "create_project is waiting", fingerprint: "create-fingerprint", related_task_id: null, related_file: null },
        { ...alert, alert_id: "approval-environment", rule_code: "APPROVAL_WAIT_TOO_LONG", category: "approval", title: "Environment approval", message: "prepare_environment is waiting", fingerprint: "environment-fingerprint", related_task_id: null, related_file: null },
      ],
      total: 2, limit: 50, offset: 0, has_more: false, counts: {},
    });
    render(<MemoryRouter><AlertsPage /></MemoryRouter>);
    expect(await screen.findByText("create_project is waiting")).toBeVisible();
    expect(screen.getByText("prepare_environment is waiting")).toBeVisible();
  });
});
