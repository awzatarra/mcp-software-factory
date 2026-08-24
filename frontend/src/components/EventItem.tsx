import {
  CheckCircle2,
  ChevronDown,
  Circle,
  CircleAlert,
  Clock3,
  Info,
  LoaderCircle,
} from "lucide-react";

import type { WorkflowEvent } from "../api/types";
import type { EventVisualState } from "../utils/eventVisualState";
import {
  formatStageName,
  formatWorkflowEvent,
} from "../utils/eventFormatting";

export function EventItem({
  event,
  visualState,
  showTechnicalMetadata = false,
  selected = false,
}: {
  event: WorkflowEvent;
  visualState: EventVisualState;
  showTechnicalMetadata?: boolean;
  selected?: boolean;
}) {
  const formatted = formatWorkflowEvent(event);
  const Icon =
    visualState.icon === "failed"
      ? CircleAlert
      : visualState.icon === "completed" ||
          visualState.icon === "resolved"
        ? CheckCircle2
        : visualState.icon === "rejected"
          ? CircleAlert
          : visualState.icon === "waiting"
          ? Clock3
          : visualState.icon === "spinner"
            ? LoaderCircle
            : visualState.icon === "started"
              ? Circle
              : Info;
  const iconLabel =
    visualState.icon === "spinner"
      ? "Evento en ejecución"
      : visualState.icon === "completed"
        ? "Evento completado"
        : visualState.icon === "resolved"
          ? "Evento resuelto"
          : visualState.icon === "rejected"
            ? "Evento rechazado"
        : visualState.icon === "failed"
          ? "Evento fallido"
          : visualState.icon === "waiting"
            ? "Evento en espera"
            : visualState.icon === "started"
              ? "Evento iniciado"
              : "Evento informativo";
  const displayedStatus = showTechnicalMetadata
    ? event.status
    : visualState.icon === "resolved"
      ? "resolved"
      : visualState.icon === "rejected"
        ? "rejected"
        : event.status;
  return (
    <li
      id={`event-${event.event_id}`}
      className={`event-item event-${visualState.icon}${selected ? " event-selected" : ""}`}
      tabIndex={-1}
      aria-current={selected ? "true" : undefined}
    >
      <div className="event-marker">
        <Icon
          className={visualState.icon === "spinner" ? "spin" : ""}
          aria-label={iconLabel}
        />
      </div>
      <div className="event-content">
        <div className="event-heading">
          <span className="event-sequence">
            #{String(event.sequence).padStart(3, "0")}
          </span>
          <strong>{formatted.title}</strong>
          <time dateTime={event.timestamp}>{formatted.time}</time>
        </div>
        <div className="event-meta">
          <span>{formatStageName(event.stage)}</span>
          <span>{displayedStatus}</span>
          {formatted.detail ? <span>{formatted.detail}</span> : null}
          {showTechnicalMetadata ? (
            <>
              <code>{event.source}</code>
              <code>{event.type}</code>
            </>
          ) : null}
        </div>
        {Object.keys(event.data).length ? (
          <details>
            <summary>
              <ChevronDown aria-hidden="true" />
              Detalles
            </summary>
            <pre>{JSON.stringify(event.data, null, 2)}</pre>
          </details>
        ) : null}
      </div>
    </li>
  );
}
