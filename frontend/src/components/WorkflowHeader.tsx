import { ArrowLeft, Check, Copy, Factory, Plus } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import type { WorkflowSnapshot } from "../api/types";
import type { ConnectionStatus as Status } from "../stores/workflowStore";
import { ConnectionStatus } from "./ConnectionStatus";

interface WorkflowHeaderProps {
  threadId: string;
  snapshot: WorkflowSnapshot | null;
  connectionStatus: Status;
}

export function WorkflowHeader({
  threadId,
  snapshot,
  connectionStatus,
}: WorkflowHeaderProps) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    await navigator.clipboard.writeText(window.location.href);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1_500);
  };
  return (
    <header className="workflow-header">
      <div className="header-brand">
        <Link className="icon-button back-button" to="/workflows" title="Volver a workflows">
          <ArrowLeft aria-hidden="true" />
          <span className="sr-only">Volver a workflows</span>
        </Link>
        <Factory aria-hidden="true" />
        <div>
          <span className="product-name">Software Factory</span>
          <h1>{snapshot?.project_name ?? "Workflow en preparación"}</h1>
        </div>
      </div>
      <div className="header-actions">
        <Link className="header-new-workflow" to="/">
          <Plus aria-hidden="true" />
          Nuevo workflow
        </Link>
        <ConnectionStatus status={connectionStatus} />
        <button
          className="thread-copy"
          type="button"
          onClick={() => void copy()}
          title="Copiar enlace del workflow"
        >
          <code>{threadId.slice(0, 8)}…</code>
          {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
          <span className="sr-only">
            {copied ? "Enlace copiado" : "Copiar enlace"}
          </span>
        </button>
      </div>
    </header>
  );
}
