import {
  Check,
  Circle,
  CircleAlert,
  Clock3,
  LoaderCircle,
} from "lucide-react";

import type { WorkflowStage } from "../utils/stageDerivation";

export function StageProgress({ stages }: { stages: WorkflowStage[] }) {
  return (
    <ol className="stage-progress" aria-label="Progreso del workflow">
      {stages.map((stage) => {
        const Icon =
          stage.status === "completed"
            ? Check
            : stage.status === "failed"
              ? CircleAlert
              : stage.status === "waiting"
                ? Clock3
                : stage.status === "running"
                  ? LoaderCircle
                  : Circle;
        return (
          <li key={stage.key} className={`stage stage-${stage.status}`}>
            <Icon
              className={stage.status === "running" ? "spin" : ""}
              aria-hidden="true"
            />
            <span>{stage.label}</span>
            <small>{stage.status}</small>
          </li>
        );
      })}
    </ol>
  );
}
