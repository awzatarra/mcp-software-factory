import {
  CheckCircle2,
  CircleAlert,
  LoaderCircle,
  Radio,
  Unplug,
} from "lucide-react";

import type { ConnectionStatus as Status } from "../stores/workflowStore";

const LABELS: Record<Status, string> = {
  idle: "Sin conexión",
  connecting: "Conectando…",
  connected: "Eventos en vivo",
  reconnecting: "Reconectando…",
  disconnected: "Desconectado",
  completed: "Workflow completado",
  error: "Error de conexión",
};

export function ConnectionStatus({ status }: { status: Status }) {
  const Icon =
    status === "connected"
      ? Radio
      : status === "completed"
        ? CheckCircle2
        : status === "error"
          ? CircleAlert
          : status === "disconnected" || status === "idle"
            ? Unplug
            : LoaderCircle;
  return (
    <div
      className={`connection-status connection-${status}`}
      role="status"
      aria-live="polite"
    >
      <Icon
        className={status === "connecting" ? "spin" : ""}
        aria-hidden="true"
      />
      <span>{LABELS[status]}</span>
    </div>
  );
}
