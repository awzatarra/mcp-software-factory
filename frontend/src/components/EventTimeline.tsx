import { Activity, Braces, ListFilter } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import type { WorkflowEvent, WorkflowSnapshot } from "../api/types";
import { shouldShowInSummary } from "../utils/eventClassification";
import { buildEventVisualStateMap } from "../utils/eventVisualState";
import { EventItem } from "./EventItem";

export function EventTimeline({
  events,
  snapshot = null,
  selectedEventId = null,
}: {
  events: WorkflowEvent[];
  snapshot?: WorkflowSnapshot | null;
  selectedEventId?: string | null;
}) {
  const [showTechnical, setShowTechnical] = useState(false);
  const [highlightedEvent, setHighlightedEvent] = useState<string | null>(
    selectedEventId,
  );
  const selectedEvent = useMemo(
    () => events.find((event) => event.event_id === selectedEventId) ?? null,
    [events, selectedEventId],
  );
  const selectedEventIsTechnical = Boolean(
    selectedEvent && !shouldShowInSummary(selectedEvent),
  );
  const effectiveTechnical = showTechnical || selectedEventIsTechnical;
  const visualStates = useMemo(
    () => buildEventVisualStateMap(events, snapshot),
    [events, snapshot],
  );
  const visibleEvents = useMemo(
    () =>
      effectiveTechnical
        ? events
        : events.filter((event) => shouldShowInSummary(event)),
    [effectiveTechnical, events],
  );
  useEffect(() => {
    if (!selectedEventId || !selectedEvent) return;
    const element = document.getElementById(`event-${selectedEventId}`);
    element?.focus({ preventScroll: true });
    element?.scrollIntoView?.({ behavior: "smooth", block: "center" });
    const activate = window.setTimeout(
      () => setHighlightedEvent(selectedEventId),
      0,
    );
    const clear = window.setTimeout(() => setHighlightedEvent(null), 3_000);
    return () => {
      window.clearTimeout(activate);
      window.clearTimeout(clear);
    };
  }, [selectedEvent, selectedEventId, visibleEvents]);
  return (
    <section className="timeline-section" aria-labelledby="timeline-title">
      <div className="section-heading">
        <div>
          <span className="section-kicker">Ejecución</span>
          <h2 id="timeline-title">Timeline de eventos</h2>
        </div>
        <div className="timeline-controls">
          <div className="view-switch" aria-label="Vista de eventos">
            <button
              type="button"
              aria-pressed={!effectiveTechnical}
              onClick={() => setShowTechnical(false)}
            >
              <ListFilter aria-hidden="true" />
              Resumen
            </button>
            <button
              type="button"
              aria-pressed={effectiveTechnical}
              onClick={() => setShowTechnical(true)}
            >
              <Braces aria-hidden="true" />
              Técnico
            </button>
          </div>
          <span className="event-count">
            {visibleEvents.length}
            {visibleEvents.length !== events.length
              ? ` de ${events.length}`
              : ""}{" "}
            eventos
          </span>
        </div>
      </div>
      {visibleEvents.length ? (
        <ol className="event-timeline">
          {visibleEvents.map((event) => (
            <EventItem
              event={event}
              visualState={visualStates.get(event.event_id)!}
              showTechnicalMetadata={showTechnical}
              selected={highlightedEvent === event.event_id}
              key={event.event_id}
            />
          ))}
        </ol>
      ) : (
        <div className="empty-state">
          <Activity aria-hidden="true" />
          <p>Esperando el primer evento del workflow…</p>
        </div>
      )}
      {selectedEventId && !selectedEvent ? (
        <p className="timeline-selection-message" role="status">
          El evento seleccionado ya no está disponible en este historial.
        </p>
      ) : null}
    </section>
  );
}
