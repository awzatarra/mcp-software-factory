import { Navigate, Route, Routes } from "react-router-dom";

import { HomePage } from "./pages/HomePage";
import { WorkflowPage } from "./pages/WorkflowPage";
import { WorkflowListPage } from "./pages/WorkflowListPage";
import { DashboardPage } from "./pages/DashboardPage";
import { AlertsPage } from "./pages/AlertsPage";
import { NotificationChannelsPage, NotificationDeliveriesPage, NotificationPoliciesPage } from "./pages/NotificationsPage";
import { ObservabilityPage } from "./pages/ObservabilityPage";
import { LlmCostsPage } from "./pages/LlmCostsPage";
import { EvaluationsPage } from "./pages/EvaluationsPage";
import { KnowledgePage } from "./pages/KnowledgePage";

export function App() {
  return (
    <Routes>
      <Route path="/" element={<HomePage />} />
      <Route path="/workflows" element={<WorkflowListPage />} />
      <Route path="/workflows/:threadId" element={<WorkflowPage />} />
      <Route path="/dashboard" element={<DashboardPage />} />
      <Route path="/alerts" element={<AlertsPage />} />
      <Route path="/notifications/channels" element={<NotificationChannelsPage />} />
      <Route path="/notifications/policies" element={<NotificationPoliciesPage />} />
      <Route path="/notifications/deliveries" element={<NotificationDeliveriesPage />} />
      <Route path="/observability" element={<ObservabilityPage />} />
      <Route path="/observability/traces" element={<ObservabilityPage />} />
      <Route path="/observability/traces/:traceId" element={<ObservabilityPage />} />
      <Route path="/observability/errors" element={<ObservabilityPage />} />
      <Route path="/observability/agents" element={<ObservabilityPage />} />
      <Route path="/observability/tools" element={<ObservabilityPage />} />
      <Route path="/observability/llm" element={<ObservabilityPage />} />
      <Route path="/observability/slow-spans" element={<ObservabilityPage />} />
      <Route path="/llm-costs" element={<LlmCostsPage />} />
      <Route path="/llm-costs/calls" element={<LlmCostsPage />} />
      <Route path="/llm-costs/calls/:llmCallId" element={<LlmCostsPage />} />
      <Route path="/llm-costs/workflows/:threadId" element={<LlmCostsPage />} />
      <Route path="/llm-costs/agents" element={<LlmCostsPage />} />
      <Route path="/llm-costs/models" element={<LlmCostsPage />} />
      <Route path="/llm-costs/pricing" element={<LlmCostsPage />} />
      <Route path="/llm-costs/budgets" element={<LlmCostsPage />} />
      <Route path="/llm-costs/budgets/:budgetId" element={<LlmCostsPage />} />
      <Route path="/llm-costs/unpriced" element={<LlmCostsPage />} />
      <Route path="/llm-costs/unavailable-usage" element={<LlmCostsPage />} />
      <Route path="/evaluations" element={<EvaluationsPage />} />
      <Route path="/evaluations/runs" element={<EvaluationsPage />} />
      <Route path="/evaluations/runs/:runId" element={<EvaluationsPage />} />
      <Route path="/evaluations/agents" element={<EvaluationsPage />} />
      <Route path="/evaluations/metrics" element={<EvaluationsPage />} />
      <Route path="/evaluations/rubrics" element={<EvaluationsPage />} />
      <Route path="/evaluations/rubrics/:rubricId" element={<EvaluationsPage />} />
      <Route path="/evaluations/baselines" element={<EvaluationsPage />} />
      <Route path="/evaluations/regressions" element={<EvaluationsPage />} />
      <Route path="/evaluations/recommendations" element={<EvaluationsPage />} />
      <Route path="/knowledge" element={<KnowledgePage />} />
      <Route path="/knowledge/candidates" element={<KnowledgePage />} />
      <Route path="/knowledge/sources" element={<KnowledgePage />} />
      <Route path="/knowledge/retrievals" element={<KnowledgePage />} />
      <Route path="/knowledge/retrievals/:retrievalId" element={<KnowledgePage />} />
      <Route path="/knowledge/:knowledgeId" element={<KnowledgePage />} />
      <Route path="*" element={<Navigate replace to="/" />} />
    </Routes>
  );
}
