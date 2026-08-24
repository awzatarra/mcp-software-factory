import { AlertTriangle } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getAlerts, type WorkflowAlert } from "../../api/alerts";

export function ActiveWorkflowAlerts({ threadId, branchId }: { threadId: string; branchId: string }) {
  const [alerts, setAlerts] = useState<WorkflowAlert[]>([]);
  useEffect(() => { const params = new URLSearchParams({ thread_id: threadId, branch_id: branchId, status: "open,acknowledged", limit: "10" }); void getAlerts(params).then((response) => setAlerts(response.items)).catch(() => undefined); }, [threadId, branchId]);
  if (!alerts.length) return null;
  const severity = alerts.reduce((highest, item) => ["info", "warning", "error", "critical"].indexOf(item.severity) > ["info", "warning", "error", "critical"].indexOf(highest) ? item.severity : highest, "info");
  return <aside className="workflow-alert-strip" data-severity={severity}><AlertTriangle aria-hidden="true" /><strong>Alertas activas: {alerts.length}</strong><span>Severidad máxima: {severity}</span><Link to={`/alerts?thread_id=${encodeURIComponent(threadId)}&branch_id=${encodeURIComponent(branchId)}`}>Ver alertas</Link></aside>;
}
