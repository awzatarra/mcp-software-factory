import {
  CheckCircle2,
  CircleDashed,
  Clock3,
  LoaderCircle,
  XCircle,
} from "lucide-react";

import type { WorkflowListStatus } from "../api/types";

const labels: Record<WorkflowListStatus, string> = {
  waiting: "Esperando aprobación",
  running: "En ejecución",
  pending: "Pendiente",
  completed: "Completado",
  failed: "Fallido",
};

export function WorkflowStatusBadge({ status }: { status: WorkflowListStatus }) {
  const Icon =
    status === "completed"
      ? CheckCircle2
      : status === "failed"
        ? XCircle
        : status === "waiting"
          ? Clock3
          : status === "running"
            ? LoaderCircle
            : CircleDashed;
  return (
    <span className={`workflow-status status-${status}`}>
      <Icon className={status === "running" ? "spin" : ""} aria-hidden="true" />
      {labels[status]}
    </span>
  );
}
