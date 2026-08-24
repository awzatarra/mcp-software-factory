from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MetricEvaluation:
    metric_name: str
    metric_value: float
    metric_type: str
    unit: str | None
    threshold: float | None
    passed: bool
    formula: str
    evidence_type: str
    reference_id: str | None
    summary: str
    evaluation_scope: str = "workflow"
    agent_name: str | None = None
    node: str | None = None
    subgraph: str | None = None
    source_state: str | None = None
    raw_value: float | None = None
    contributes_to_score: bool = True

    @property
    def score(self) -> float:
        return max(0.0, min(1.0, self.metric_value))


def bounded(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 6)


def deterministic(context: dict[str, Any]) -> list[MetricEvaluation]:
    terminal = context["terminal_status"] in {"completed", "tests_failed", "infrastructure_failed", "user_cancelled", "repair_limit_reached"}
    environment_prepared=bool(context["environment_prepared"])
    environment_attempted=bool(context.get("environment_attempted"))
    environment_summary=(
        "Environment preparation completed successfully"
        if environment_prepared else
        "Environment preparation was attempted but the final state was not prepared"
        if environment_attempted else
        "No environment preparation evidence exists"
    )
    unhandled=int(context.get("unhandled_error_count",context.get("unhandled_exceptions",0)))
    checks = [
        ("schema_valid", context["registry_present"], "workflow", context.get("workflow_id"), "Registry and trace contracts are readable"),
        ("tests_passed", context["tests_passed"], "test_run", context.get("test_reference"), "Durable test result passed"),
        ("files_created", context["files_created"] > 0, "artifact", context.get("artifact_reference"), f"{context['files_created']} project files recorded"),
        ("project_created", context["project_created"], "workflow", context.get("workflow_id"), "Project creation evidence exists"),
        ("environment_prepared", environment_prepared, "workflow", context.get("environment_reference"), environment_summary),
        ("no_active_spans_terminal", not terminal or context["active_spans"] == 0, "trace", context.get("trace_id"), f"{context['active_spans']} active spans"),
        ("no_secrets_detected", not context["secrets_detected"], "trace", context.get("trace_id"), "Persisted evaluation evidence contains no secret patterns"),
        ("budget_respected", not context["budget_exceeded"], "budget_event", context.get("budget_reference"), "No durable budget overrun"),
        ("tool_success_rate", context["tool_success_rate"] >= .95, "trace", context.get("trace_id"), f"Tool success rate {context['tool_success_rate']:.3f}"),
        ("no_unhandled_exception", unhandled == 0, "trace", context.get("trace_id"), f"{unhandled} unhandled errors; {context.get('handled_error_count',0)} handled and {context.get('recovered_error_count',0)} recovered"),
        ("approval_completed", not context["approval_pending"], "approval", context.get("approval_reference"), "No approval remains pending"),
    ]
    metrics=[MetricEvaluation(name,1.0 if passed else 0.0,"deterministic","ratio",1.0,passed,"boolean check",evidence,reference,summary,source_state=str(passed).lower()) for name,passed,evidence,reference,summary in checks]
    if context.get("git_evidence_available"):
        for name in (
            "git_repository_initialized", "git_workflow_branch_created",
            "git_changes_committed", "git_commit_after_tests",
            "git_protected_branch_respected", "git_working_tree_clean_terminal",
        ):
            passed=bool(context.get(name))
            metrics.append(MetricEvaluation(
                name, 1.0 if passed else 0.0, "deterministic", "ratio", 1.0,
                passed, "durable Git workflow evidence", "git_operation",
                context.get("git_reference"), name.replace("_", " "),
                source_state=str(passed).lower(), contributes_to_score=False,
            ))
    if context.get("git_promotion_evidence_available"):
        for name in (
            "git_promotion_prepared", "git_promotion_conflict_free",
            "git_promotion_approved", "git_promotion_completed",
            "git_base_branch_advanced_safely",
            "git_no_direct_protected_branch_commit",
        ):
            passed=bool(context.get(name))
            metrics.append(MetricEvaluation(
                name, 1.0 if passed else 0.0, "deterministic", "ratio", 1.0,
                passed, "durable controlled Git promotion evidence", "git_promotion",
                context.get("git_promotion_reference"), name.replace("_", " "),
                source_state=str(passed).lower(), contributes_to_score=False,
            ))
    for name,value in (("handled_error_count",context.get("handled_error_count",0)),("recovered_error_count",context.get("recovered_error_count",0)),("unhandled_error_count",unhandled)):
        metrics.append(MetricEvaluation(name,float(value),"diagnostic","count",None,name!="unhandled_error_count" or value==0,"classified terminal error state","trace",context.get("trace_id"),f"{value} {name.replace('_',' ')}",source_state=str(value),raw_value=float(value),contributes_to_score=False))
    return metrics


def agent_deterministic(context: dict[str, Any], agent: str) -> list[MetricEvaluation]:
    evidence=(context.get("agent_evidence") or {}).get(agent) or {}
    metrics: list[MetricEvaluation]=[]
    spans=evidence.get("spans") or []
    if spans:
        completed=sum(item.get("status")=="completed" for item in spans)
        score=completed/len(spans)
        metrics.append(MetricEvaluation("agent_spans_completed",score,"deterministic","ratio",1.0,score==1,"completed agent spans / agent spans","span",spans[-1].get("span_id"),f"{completed}/{len(spans)} attributable agent spans completed","agent",agent,spans[-1].get("node"),evidence.get("subgraph"),f"{completed}/{len(spans)}"))
    tools=evidence.get("tools") or []
    if tools:
        completed=sum(item.get("status")=="completed" for item in tools);score=completed/len(tools)
        metrics.append(MetricEvaluation("agent_tool_success",score,"deterministic","ratio",1.0,score==1,"completed attributable tool calls / attributable tool calls","tool_call",tools[-1].get("call_id"),f"{completed}/{len(tools)} attributable tool calls completed","agent",agent,evidence.get("node"),evidence.get("subgraph"),f"{completed}/{len(tools)}"))
    llm=evidence.get("llm_calls") or []
    if llm:
        completed=sum(item.get("status")=="completed" for item in llm);score=completed/len(llm)
        metrics.append(MetricEvaluation("agent_llm_success",score,"deterministic","ratio",1.0,score==1,"completed attributable LLM calls / attributable LLM calls","llm_call",llm[-1].get("call_id"),f"{completed}/{len(llm)} attributable LLM calls completed","agent",agent,evidence.get("node"),evidence.get("subgraph"),f"{completed}/{len(llm)}"))
    for check in evidence.get("checks") or []:
        passed=bool(check["passed"])
        metrics.append(MetricEvaluation(check["metric_name"],1.0 if passed else 0.0,"deterministic","ratio",1.0,passed,check.get("formula") or "attributable durable state check",check.get("evidence_type") or "workflow_event",check.get("reference_id"),check["summary"],"agent",agent,check.get("node"),check.get("subgraph"),check.get("source_state")))
    return metrics


def agent_heuristic(context: dict[str, Any], agent: str) -> list[MetricEvaluation]:
    evidence=(context.get("agent_evidence") or {}).get(agent) or {}
    if not any(evidence.get(name) for name in ("spans","tools","llm_calls","checks")):return []
    values=[]
    spans=evidence.get("spans") or []
    if spans:
        duration=sum(float(item.get("duration_ms") or 0) for item in spans)
        values.append(("agent_duration",bounded(1/(1+duration/120000)),duration,"1 / (1 + attributable_agent_duration_ms / 120000)",spans[-1].get("span_id")))
    tools=evidence.get("tools") or []
    if tools:
        errors=sum(item.get("status")!="completed" for item in tools);rate=errors/len(tools)
        values.append(("agent_tool_error_rate",bounded(1-rate),rate,"1 - attributable_tool_errors / attributable_tool_calls",tools[-1].get("call_id")))
    llm=evidence.get("llm_calls") or []
    if llm:
        errors=sum(item.get("status")!="completed" for item in llm);rate=errors/len(llm)
        values.append(("agent_llm_error_rate",bounded(1-rate),rate,"1 - attributable_llm_errors / attributable_llm_calls",llm[-1].get("call_id")))
        retries=sum(int(item.get("retry_attempt") or 0) for item in llm)
        values.append(("agent_retry_count",bounded(1-retries/3),float(retries),"1 - min(attributable_retries / 3, 1)",llm[-1].get("call_id")))
    return [MetricEvaluation(name,score,"heuristic","score",.7,score>=.7,formula,"trace",reference,f"Normalized from attributable {name.replace('_',' ')}","agent",agent,evidence.get("node"),evidence.get("subgraph"),"attributable_observability",float(raw)) for name,score,raw,formula,reference in values]


def heuristic(context: dict[str, Any]) -> list[MetricEvaluation]:
    wall_clock=float(context.get("workflow_wall_clock_duration_ms",context.get("duration_ms",0)))
    active_total=float(context.get("agent_active_duration_total_ms",context.get("agent_duration_ms",0)))
    values = [
        ("retry_count", bounded(1-context["retry_count"]/3), context["retry_count"], "1 - min(retries / 3, 1)","observability_llm_calls.retry_attempt",True),
        ("repair_count", bounded(1-context["repair_count"]/3), context["repair_count"], "1 - min(repairs / 3, 1)","workflow_registry.repair_attempts",True),
        ("tool_error_rate", bounded(1-context["tool_error_rate"]), context["tool_error_rate"], "1 - tool_errors / tool_calls","observability_tool_calls.status",True),
        ("llm_error_rate", bounded(1-context["llm_error_rate"]), context["llm_error_rate"], "1 - llm_errors / llm_calls","observability_llm_calls.status",True),
        ("workflow_wall_clock_duration", bounded(1/(1+wall_clock/300000)), wall_clock, "1 / (1 + workflow_wall_clock_duration_ms / 300000)","observability_traces.duration_ms",True),
        ("cost_efficiency", bounded(1/(1+context["cost_usd"]/.10)), context["cost_usd"], "1 / (1 + cost_usd / 0.10)","llm_cost_calculations.total_cost",True),
        ("token_efficiency", bounded(1/(1+context["total_tokens"]/20000)), context["total_tokens"], "1 / (1 + total_tokens / 20000)","observability_llm_calls.total_tokens",True),
        ("slow_span_ratio", bounded(1-context["slow_span_ratio"]), context["slow_span_ratio"], "1 - slow_spans / spans","observability_spans.is_slow",True),
        ("approval_wait_ratio", bounded(1-context["approval_wait_ratio"]), context["approval_wait_ratio"], "1 - approval_wait_ms / workflow_wall_clock_duration_ms","observability_spans.approval_duration",True),
        ("agent_active_duration_total",1.0,active_total,"diagnostic sum of attributable agent span durations; may overlap","observability_spans.duration_ms_sum",False),
        ("parallelism_factor",1.0,active_total/wall_clock if wall_clock else 0.0,"agent_active_duration_total / workflow_wall_clock_duration","derived_observability",False),
    ]
    return [MetricEvaluation(name,score,"heuristic","score",.7,score>=.7,formula,"trace",context.get("trace_id"),f"Normalized {name.replace('_',' ')}" if contributes else f"Diagnostic {name.replace('_',' ')}; excluded from score",source_state=source,raw_value=float(raw),contributes_to_score=contributes) for name,score,raw,formula,source,contributes in values]


DEFAULT_HEURISTIC_DIMENSION_WEIGHTS={"reliability_score":.30,"latency_score":.20,"cost_efficiency_score":.20,"human_wait_score":.10,"agent_efficiency_score":.20}


def heuristic_dimension_aggregate(
    workflow_metrics: list[MetricEvaluation],
    agent_scores: dict[str,float],
    weights: dict[str,float] | None = None,
) -> tuple[float,dict[str,float],dict[str,float]]:
    metric_scores={item.metric_name:item.score for item in workflow_metrics if item.contributes_to_score}
    groups={
        "reliability_score":[metric_scores[name] for name in ("tool_error_rate","llm_error_rate","retry_count","repair_count") if name in metric_scores],
        "latency_score":[metric_scores[name] for name in ("workflow_wall_clock_duration","slow_span_ratio") if name in metric_scores],
        "cost_efficiency_score":[metric_scores[name] for name in ("cost_efficiency","token_efficiency") if name in metric_scores],
        "human_wait_score":[metric_scores[name] for name in ("approval_wait_ratio",) if name in metric_scores],
        "agent_efficiency_score":[float(value) for value in agent_scores.values()],
    }
    dimensions={name:bounded(sum(values)/len(values)) for name,values in groups.items() if values}
    configured=weights or DEFAULT_HEURISTIC_DIMENSION_WEIGHTS
    selected={name:float(configured.get(name,0)) for name in dimensions if float(configured.get(name,0))>0}
    total=sum(selected.values())
    if not total:return 0.0,dimensions,{}
    normalized={name:weight/total for name,weight in selected.items()}
    return bounded(sum(dimensions[name]*weight for name,weight in normalized.items())),dimensions,normalized


def weighted_score(dimensions: dict[str, float], weights: dict[str, float]) -> float:
    selected=[(max(0,min(1,float(dimensions.get(name,0)))),float(weight)) for name,weight in weights.items() if float(weight)>0]
    total=sum(weight for _,weight in selected)
    return bounded(sum(value*weight for value,weight in selected)/total) if total else 0.0


def verdict(score: float, thresholds: dict[str, float] | None = None) -> str:
    values={"excellent":.90,"good":.80,"acceptable":.70,"needs_improvement":.50,**(thresholds or {})}
    if score>=values["excellent"]:return "excellent"
    if score>=values["good"]:return "good"
    if score>=values["acceptable"]:return "acceptable"
    if score>=values["needs_improvement"]:return "needs_improvement"
    return "failed"


def aggregate_workflow(agent_scores: dict[str, float], efficiency: float) -> float:
    weights={"Planner":.18,"Developer":.32,"QA":.22,"Repair":.08,"Supervisor":.10,"Efficiency":.10}
    values={**agent_scores,"Efficiency":efficiency}
    present={name:weight for name,weight in weights.items() if name in values}
    total=sum(present.values())
    return bounded(sum(values[name]*weight for name,weight in present.items())/total) if total else 0.0
