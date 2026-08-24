import type { WorkflowEvent } from "../api/types";

export interface MergeEventsResult {
  events: WorkflowEvent[];
  conflict?: string;
}

export function mergeWorkflowEvents(
  current: WorkflowEvent[],
  incoming: WorkflowEvent[],
  maxVisible = 2_000,
): MergeEventsResult {
  const byId = new Map(current.map((event) => [event.event_id, event]));
  const bySequence = new Map(
    current.map((event) => [event.sequence, event.event_id]),
  );
  let conflict: string | undefined;

  for (const event of incoming) {
    if (byId.has(event.event_id)) continue;
    const sequenceOwner = bySequence.get(event.sequence);
    if (sequenceOwner && sequenceOwner !== event.event_id) {
      conflict = `La secuencia ${event.sequence} identifica eventos incompatibles.`;
      continue;
    }
    byId.set(event.event_id, event);
    bySequence.set(event.sequence, event.event_id);
  }

  const events = [...byId.values()]
    .sort((first, second) => first.sequence - second.sequence)
    .slice(-maxVisible);
  return { events, conflict };
}
