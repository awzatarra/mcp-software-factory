import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { NotificationChannelsPage, NotificationDeliveriesPage, NotificationPoliciesPage } from "./NotificationsPage";

const api = vi.hoisted(() => ({ channels: vi.fn(), createChannel: vi.fn(), updateChannel: vi.fn(), deleteChannel: vi.fn(), testChannel: vi.fn(), policies: vi.fn(), createPolicy: vi.fn(), updatePolicy: vi.fn(), deletePolicy: vi.fn(), quiet: vi.fn(), escalations: vi.fn(), createEscalation: vi.fn(), deleteEscalation: vi.fn(), deliveries: vi.fn(), delivery: vi.fn(), action: vi.fn(), summary: vi.fn(), inbox: vi.fn(), read: vi.fn(), readAll: vi.fn() }));
vi.mock("../api/notifications", () => ({ getChannels: api.channels, createChannel: api.createChannel, updateChannel: api.updateChannel, deleteChannel: api.deleteChannel, testChannel: api.testChannel, getPolicies: api.policies, createPolicy: api.createPolicy, updatePolicy: api.updatePolicy, deletePolicy: api.deletePolicy, getQuietHours: api.quiet, getEscalations: api.escalations, createEscalation: api.createEscalation, deleteEscalation: api.deleteEscalation, getDeliveries: api.deliveries, getDelivery: api.delivery, deliveryAction: api.action, getNotificationSummary: api.summary, getInbox: api.inbox, markNotificationRead: api.read, markAllNotificationsRead: api.readAll }));
vi.mock("../api/alerts", () => ({ getAlertSummary: vi.fn().mockResolvedValue({ critical_open: 0, error_open: 0 }) }));

const channel = { channel_id: "webhook-1", name: "Operations", description: null, channel_type: "webhook", enabled: true, configuration: { url: "https://example.com[:port]/[redacted]" }, secret_reference: "env:OPS_WEBHOOK", secret_configured: true, health: "healthy", timeout_seconds: 10, max_attempts: 3, initial_backoff_seconds: 2, max_backoff_seconds: 300, rate_limit_per_minute: 30, verify_tls: true, created_at: "2026-08-04T10:00:00Z", updated_at: null };
const internal = { ...channel, channel_id: "builtin:internal", name: "Internal inbox", channel_type: "internal", configuration: {}, secret_reference: null, secret_configured: false };
const policy = { policy_id: "policy-1", name: "Operations failures", description: null, enabled: true, channel_ids: ["webhook-1"], rule_codes: ["WORKFLOW_FAILED", "TESTS_FAILED"], severities: ["error", "critical"], alert_events: ["alert_opened", "alert_resolved"], statuses: ["open", "resolved"], branch_scope: "all", project_name_pattern: null, cooldown_seconds: 0, send_resolved: true, send_acknowledged: false, quiet_hours_id: "quiet-1", escalation_policy_id: "escalation-1", created_at: "2026-08-04T10:00:00Z", updated_at: null };
const escalation = { escalation_policy_id: "escalation-1", name: "Critical escalation", description: "Internal then webhook", enabled: true, steps: [{ step: 1, delay_seconds: 0, channel_ids: ["builtin:internal"], severities: ["warning", "error", "critical"], require_unacknowledged: false, repeat: false, repeat_interval_seconds: null, max_repeats: null }, { step: 2, delay_seconds: 60, channel_ids: ["webhook-1"], severities: ["warning", "error", "critical"], require_unacknowledged: true, repeat: false, repeat_interval_seconds: null, max_repeats: null }], stop_on_acknowledge: true, stop_on_resolve: true, created_at: "2026-08-04T10:00:00Z", updated_at: null };
const delivery = { delivery_id: "delivery-1", alert_id: "alert-1", occurrence_id: "occ-1", action_id: null, policy_id: "policy-1", channel_id: "webhook-1", alert_event: "alert_opened", escalation_step: null, original_delivery_id: null, fingerprint: "fingerprint", status: "dead_letter", scheduled_at: "2026-08-04T10:00:00Z", first_attempt_at: "2026-08-04T10:00:01Z", last_attempt_at: "2026-08-04T10:00:03Z", delivered_at: null, next_attempt_at: null, attempt_count: 3, max_attempts: 3, response_status_code: 500, response_summary: "failed", last_error_code: "http_5xx", last_error_message: "Remote endpoint returned HTTP 500", read_at: null, created_at: "2026-08-04T10:00:00Z", updated_at: "2026-08-04T10:00:03Z" };

beforeEach(() => {
  vi.clearAllMocks();
  api.channels.mockResolvedValue([internal, channel]); api.policies.mockResolvedValue([policy]); api.quiet.mockResolvedValue([{ quiet_hours_id: "quiet-1", name: "Night" }]); api.escalations.mockResolvedValue([escalation]);
  api.deliveries.mockResolvedValue({ items: [delivery], total: 1, limit: 50, offset: 0, has_more: false }); api.delivery.mockResolvedValue({ delivery, channel, policy, attempts: [{ attempt_id: "attempt-1", delivery_id: "delivery-1", attempt_number: 1, started_at: "2026-08-04T10:00:01Z", completed_at: "2026-08-04T10:00:02Z", duration_ms: 1000, result: "retryable_failure", request_method: "POST", target_host: "example.com", response_status_code: 500, response_summary: "failed", error_code: "http_5xx", error_message: "Remote endpoint returned HTTP 500" }] });
  api.summary.mockResolvedValue({ unread_internal: 1, dead_letter: 1, failed: 0, deliveries_last_24h: 1, channels_misconfigured: 0 }); api.inbox.mockResolvedValue({ items: [{ ...delivery, delivery_id: "internal-1", channel_id: "builtin:internal", status: "delivered" }], unread: 1, total: 1 });
  api.createChannel.mockResolvedValue(channel); api.updateChannel.mockResolvedValue(channel); api.testChannel.mockResolvedValue({ delivery_id: "test-delivery", status: "pending" }); api.createPolicy.mockResolvedValue(policy); api.createEscalation.mockResolvedValue(escalation); api.action.mockResolvedValue(delivery); api.read.mockResolvedValue({ ...delivery, read_at: "2026-08-04T11:00:00Z" }); api.readAll.mockResolvedValue({ updated: 1 });
});

describe("notification management", () => {
  it("navigates between alerts, rules, channels, policies and deliveries", async () => {
    render(<MemoryRouter initialEntries={["/notifications/channels"]}><NotificationChannelsPage /></MemoryRouter>);
    expect(await screen.findByRole("link", { name: "Canales" })).toHaveAttribute("aria-current", "page");
    expect(screen.getAllByRole("link", { name: "Alertas" })).toHaveLength(2);
    expect(screen.getByRole("link", { name: "Políticas" })).toHaveAttribute("href", "/notifications/policies");
    expect(screen.getByRole("link", { name: "Entregas" })).toHaveAttribute("href", "/notifications/deliveries");
  });

  it("lists internal and webhook channels without exposing secret values", async () => {
    render(<MemoryRouter><NotificationChannelsPage /></MemoryRouter>);
    expect(await screen.findByText("Operations")).toBeVisible(); expect(screen.getByText("Internal inbox")).toBeVisible();
    expect(screen.getByText("env:OPS_WEBHOOK")).toBeVisible(); expect(screen.queryByText(/secret-value/)).not.toBeInTheDocument();
    expect(screen.getByText(/\[redacted\]/)).toBeVisible();
  });

  it("creates, disables and tests a webhook with explicit confirmation", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true); const user=userEvent.setup(); render(<MemoryRouter><NotificationChannelsPage /></MemoryRouter>); await screen.findByText("Operations");
    await user.click(screen.getByRole("button", { name: /Crear canal/ })); await user.type(screen.getByLabelText("Nombre"), "New webhook"); await user.type(screen.getByLabelText("Secret reference"), "env:NEW_WEBHOOK"); await user.click(screen.getByRole("button", { name: "Guardar" }));
    expect(api.createChannel).toHaveBeenCalledWith(expect.objectContaining({ name: "New webhook", secret_reference: "env:NEW_WEBHOOK" }));
    await user.click(screen.getAllByRole("button", { name: "Desactivar" }).at(-1)!); expect(api.updateChannel).toHaveBeenCalledWith("webhook-1", { enabled: false });
    await user.click(screen.getAllByRole("button", { name: /Probar/ }).at(-1)!); expect(api.testChannel).toHaveBeenCalledWith("webhook-1"); expect(await screen.findByText(/Prueba pending/)).toBeVisible();
  });

  it("reads internal notifications without acknowledging alerts", async () => {
    const user=userEvent.setup();render(<MemoryRouter><NotificationChannelsPage /></MemoryRouter>);expect(await screen.findByText("1 sin leer")).toBeVisible();
    await user.click(screen.getByRole("button", { name: /Marcar leída internal-1/ }));expect(api.read).toHaveBeenCalledWith("internal-1");
    expect(api.action).not.toHaveBeenCalled();
  });

  it("shows policy conditions in natural language and operational metadata", async () => {
    render(<MemoryRouter><NotificationPoliciesPage /></MemoryRouter>); expect(await screen.findByText("Operations failures")).toBeVisible();
    expect(screen.getByText(/error, critical.*WORKFLOW_FAILED y TESTS_FAILED.*Operations/)).toBeVisible(); expect(screen.getByText(/Todos los branches.*quiet hours.*escalamiento/)).toBeVisible();
  });

  it("creates a policy with channels, events, severities and branch scope", async () => {
    const user=userEvent.setup();render(<MemoryRouter><NotificationPoliciesPage /></MemoryRouter>);await screen.findByText("Operations failures");await user.click(screen.getByRole("button", { name: /Crear política/ }));
    await user.type(screen.getByLabelText("Nombre"), "Critical notifications"); await user.click(screen.getByLabelText("Operations")); await user.selectOptions(screen.getByLabelText("Branch"), "all"); await user.click(screen.getByRole("button", { name: "Guardar" }));
    expect(api.createPolicy).toHaveBeenCalledWith(expect.objectContaining({ name: "Critical notifications", channel_ids: ["webhook-1"], branch_scope: "all" }));
  });

  it("renders two escalation steps with builtin and webhook channels", async () => {
    render(<MemoryRouter><NotificationPoliciesPage /></MemoryRouter>);
    expect(await screen.findByText("Critical escalation")).toBeVisible();
    expect(screen.getByText(/Paso 1: 0s.*Internal inbox.*siempre/)).toBeVisible();
    expect(screen.getByText(/Paso 2: 60s.*Operations.*sin acknowledge/)).toBeVisible();
  });

  it("creates an escalation with complete internal and webhook steps", async () => {
    const user=userEvent.setup();render(<MemoryRouter><NotificationPoliciesPage /></MemoryRouter>);await screen.findByText("Critical escalation");
    await user.click(screen.getByRole("button",{name:/Crear escalamiento/}));
    const form=screen.getByRole("form",{name:"Crear política de escalamiento"});
    await user.type(within(form).getByLabelText("Nombre"),"Manual acknowledge cancellation test");
    await user.type(within(form).getByLabelText("Descripción"),"Step 1 internal; step 2 webhook after 60 seconds");
    await user.click(within(form).getByLabelText("Internal inbox"));
    await user.click(within(form).getByLabelText("Requerir alerta sin acknowledge"));
    await user.click(within(form).getByRole("button",{name:/Agregar paso/}));
    await user.clear(within(form).getByLabelText("Espera paso 2"));await user.type(within(form).getByLabelText("Espera paso 2"),"60");
    await user.click(within(form).getAllByLabelText("Operations").at(-1)!);
    await user.click(within(form).getByRole("button",{name:"Guardar"}));
    expect(api.createEscalation).toHaveBeenCalledTimes(1);
    expect(api.createEscalation).toHaveBeenCalledWith(expect.objectContaining({
      stop_on_acknowledge:true,stop_on_resolve:true,
      steps:[
        expect.objectContaining({step:1,channel_ids:["builtin:internal"],require_unacknowledged:false,repeat_interval_seconds:null,max_repeats:null}),
        expect.objectContaining({step:2,delay_seconds:60,channel_ids:["webhook-1"],require_unacknowledged:true,repeat_interval_seconds:null,max_repeats:null}),
      ],
    }));
  });

  it("shows a 422 reference error and never duplicates an escalation POST", async () => {
    let rejectRequest:(error:Error)=>void=()=>undefined;
    api.createEscalation.mockImplementationOnce(()=>new Promise((_,reject)=>{rejectRequest=reject;}));
    const user=userEvent.setup();render(<MemoryRouter><NotificationPoliciesPage /></MemoryRouter>);await screen.findByText("Critical escalation");
    await user.click(screen.getByRole("button",{name:/Crear escalamiento/}));const form=screen.getByRole("form",{name:"Crear política de escalamiento"});
    await user.type(within(form).getByLabelText("Nombre"),"Invalid reference");await user.click(within(form).getByLabelText("Internal inbox"));
    const save=within(form).getByRole("button",{name:"Guardar"});await user.click(save);expect(save).toBeDisabled();await user.click(save);expect(api.createEscalation).toHaveBeenCalledTimes(1);
    rejectRequest(new Error("Notification channel not found: missing-channel"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Notification channel not found: missing-channel");
  });

  it("lists dead letters, preserves URL filters and supports retry", async () => {
    const user=userEvent.setup();render(<MemoryRouter initialEntries={["/notifications/deliveries?status=dead_letter"]}><NotificationDeliveriesPage /></MemoryRouter>);expect(await screen.findByText("dead_letter")).toBeVisible();
    expect(api.deliveries.mock.calls.at(-1)?.[0].get("status")).toBe("dead_letter"); await user.click(screen.getByRole("button", { name: /Reintentar delivery-1/ })); expect(api.action).toHaveBeenCalledWith("delivery-1", "retry");
  });

  it("shows sanitized attempt detail and redelivers with confirmation", async () => {
    vi.spyOn(window,"confirm").mockReturnValue(true);const user=userEvent.setup();render(<MemoryRouter><NotificationDeliveriesPage /></MemoryRouter>);await screen.findByText("dead_letter");await user.click(screen.getByRole("button", { name: /Ver entrega delivery-1/ }));
    expect(await screen.findByRole("dialog")).toHaveTextContent("#1 · retryable_failure · HTTP 500 · 1000 ms");expect(screen.queryByText(/https:\/\//)).not.toBeInTheDocument();await user.click(screen.getByRole("button", { name: "Reenviar" }));expect(api.action).toHaveBeenCalledWith("delivery-1","redeliver");
  });

  it("renders loading, empty and backend error states", async () => {
    api.channels.mockImplementationOnce(() => new Promise(() => {})); const first=render(<MemoryRouter><NotificationChannelsPage /></MemoryRouter>); expect(screen.getByRole("status")).toBeVisible(); first.unmount();
    api.channels.mockResolvedValueOnce([]); api.inbox.mockResolvedValueOnce({ items: [], unread: 0, total: 0 }); const second=render(<MemoryRouter><NotificationChannelsPage /></MemoryRouter>); expect(await screen.findByText("No hay resultados.")).toBeVisible(); second.unmount();
    api.deliveries.mockRejectedValueOnce(new Error("Backend offline")); render(<MemoryRouter><NotificationDeliveriesPage /></MemoryRouter>); expect(await screen.findByRole("alert")).toHaveTextContent("Backend offline");
  });

  it("never uses dangerouslySetInnerHTML", () => { expect(NotificationChannelsPage.toString()).not.toContain("dangerouslySetInnerHTML"); expect(NotificationDeliveriesPage.toString()).not.toContain("dangerouslySetInnerHTML"); });
});
