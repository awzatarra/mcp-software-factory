from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
import re
import sqlite3
from typing import Any

from api.agent_evaluation_models import EvaluationRunCreate, JudgeOutput
from api.services.agent_evaluation_store import AgentEvaluationStore
from api.services.agent_evaluators import agent_deterministic, agent_heuristic, aggregate_workflow, bounded, deterministic, heuristic, heuristic_dimension_aggregate, verdict, weighted_score
from api.services.observability_context import observability_context
from api.services.strict_json_schema import InvalidStrictJsonSchema, strict_json_schema_for_model


EVALUATOR_VERSION = "6.17.3"
SECRET_PATTERN = re.compile(r"(?:sk-[A-Za-z0-9_-]{8,}|authorization\s*:|api[_-]?key\s*=)", re.I)
HIGHER_IS_BETTER_METRICS={"overall_score","workflow_score","reliability_score","latency_score","cost_efficiency_score","human_wait_score","agent_efficiency_score","tool_success_rate"}
LOWER_IS_BETTER_METRICS={"duration_ms","evaluation_cost","llm_judge_cost","total_tokens","workflow_duration","workflow_wall_clock_duration","tool_error_rate","llm_error_rate","retry_count","repair_count"}


def metric_direction(metric: str) -> str:
    if metric in LOWER_IS_BETTER_METRICS:return "lower_is_better"
    if metric in HIGHER_IS_BETTER_METRICS or metric.endswith("_score"):return "higher_is_better"
    if any(token in metric for token in ("cost","duration","latency_ms","error","retry","token","failure_count")):return "lower_is_better"
    return "higher_is_better"


class AgentEvaluationService:
    def __init__(self, store: AgentEvaluationStore, *, observability=None, llm_costs=None, openai_client=None, default_model: str | None = None, heuristic_dimension_weights: dict[str,float] | None = None) -> None:
        self.store=store;self.observability=observability;self.llm_costs=llm_costs
        self.openai_client=openai_client;self.default_model=default_model
        self.heuristic_dimension_weights=heuristic_dimension_weights

    async def initialize(self): await self.store.initialize()

    async def _context(self, workflow_id: str, branch_id: str, trace_id: str | None = None) -> dict[str, Any]:
        registry=await self.store.fetch_one("SELECT * FROM workflow_registry WHERE thread_id=?",(workflow_id,))
        trace=await self.store.fetch_one("SELECT * FROM observability_traces WHERE workflow_id=? AND branch_id=? ORDER BY started_at DESC LIMIT 1",(workflow_id,branch_id)) if not trace_id else await self.store.fetch_one("SELECT * FROM observability_traces WHERE trace_id=?",(trace_id,))
        selected_trace=trace.get("trace_id") if trace else trace_id
        spans=await self.store.fetch_all("SELECT * FROM observability_spans WHERE trace_id=?",(selected_trace,)) if selected_trace else []
        tools=await self.store.fetch_all("SELECT * FROM observability_tool_calls WHERE trace_id=?",(selected_trace,)) if selected_trace else []
        llm=await self.store.fetch_all("SELECT * FROM observability_llm_calls WHERE workflow_id=? AND branch_id=?",(workflow_id,branch_id))
        costs=await self.store.fetch_all("SELECT * FROM llm_cost_calculations WHERE workflow_id=? AND branch_id=? AND superseded=0",(workflow_id,branch_id))
        files=await self.store.fetch_all("SELECT path FROM workflow_project_files WHERE thread_id=? AND branch_id=? AND change_type!='deleted'",(workflow_id,branch_id))
        events=await self.store.fetch_all("SELECT event_id,event_type,status,stage,message,data_json,created_at FROM workflow_events WHERE thread_id=? AND branch_id=? ORDER BY sequence",(workflow_id,branch_id))
        budget_events=await self.store.fetch_all("SELECT budget_event_id,decision,reason_code FROM llm_budget_events WHERE workflow_id=? AND branch_id=?",(workflow_id,branch_id))
        try:
            git_states=await self.store.fetch_all("SELECT * FROM git_workflow_state WHERE workflow_id=?",(workflow_id,))
            git_operations=await self.store.fetch_all("SELECT * FROM git_operations WHERE workflow_id=? ORDER BY created_at",(workflow_id,))
            git_promotions=await self.store.fetch_all("SELECT * FROM git_promotions WHERE workflow_id=? ORDER BY created_at",(workflow_id,))
        except sqlite3.OperationalError:
            git_states=[]
            git_operations=[]
            git_promotions=[]
        tool_errors=sum(item.get("status") not in {"completed","success"} for item in tools)
        llm_errors=sum(item.get("status")!="completed" for item in llm)
        duration=float(trace.get("duration_ms") or 0) if trace else 0
        approval_spans=[item for item in spans if item.get("category")=="approval"]
        persisted_summaries=" ".join(str(item.get("message") or "") for item in events)
        event_types={str(item["event_type"]) for item in events}
        git_state=git_states[0] if git_states else {}
        git_commits=[item for item in git_operations if item.get("operation")=="git.commit" and item.get("success") and item.get("commit_sha")]
        promotion=git_promotions[-1] if git_promotions else {}
        try:
            promotion_preview=json.loads(promotion.get("preview_json") or "{}")
        except (TypeError,json.JSONDecodeError):
            promotion_preview={}
        test_completed_at=max((str(item.get("created_at") or "") for item in events if item.get("event_type")=="test_run_completed" and item.get("status")=="completed"),default="")
        commits_after_tests=bool(git_commits) and all(str(item.get("created_at") or "")>=test_completed_at for item in git_commits) if test_completed_at else False
        span_by_id={str(item["span_id"]):item for item in spans}

        def span_agent(span_id: str | None) -> str | None:
            seen=set();current=span_by_id.get(str(span_id)) if span_id else None
            while current and current.get("span_id") not in seen:
                seen.add(current.get("span_id"))
                if current.get("agent"):return str(current["agent"])
                current=span_by_id.get(str(current.get("parent_span_id"))) if current.get("parent_span_id") else None
            return None

        def matching_event(event_type: str, *, tool: str | None = None):
            for item in reversed(events):
                if item["event_type"]!=event_type:continue
                data=item.get("data") or {}
                if tool is None or data.get("tool")==tool:return item
            return None

        def event_check(event, metric_name: str, passed: bool, success: str, failure: str, *, node: str | None = None, subgraph: str | None = None):
            return {"metric_name":metric_name,"passed":passed,"reference_id":event.get("event_id") if event else None,"summary":success if passed else failure,"source_state":str((event or {}).get("status") or "missing"),"node":node,"subgraph":subgraph}

        environment_event=matching_event("tool_completed",tool="prepare_test_environment")
        environment_attempt=environment_event is not None or matching_event("tool_started",tool="prepare_test_environment") is not None
        environment_data=(environment_event or {}).get("data") or {}
        environment_prepared=bool(environment_event and environment_event.get("status")=="completed" and environment_data.get("business_success",True))
        failed_spans=[item for item in spans if item.get("status")=="failed" and item.get("error_type")]
        completed_keys={(item.get("name"),item.get("node"),item.get("operation")) for item in spans if item.get("status")=="completed"}
        handled=[item for item in failed_spans if item.get("error_type")=="GraphInterrupt"]
        remaining=[item for item in failed_spans if item not in handled]
        recovered=[item for item in remaining if (item.get("name"),item.get("node"),item.get("operation")) in completed_keys or str((registry or {}).get("terminal_status"))=="completed"]
        unhandled=[item for item in remaining if item not in recovered]

        agent_evidence={name:{"spans":[],"tools":[],"llm_calls":[],"checks":[]} for name in ("Planner","Developer","QA","Repair","Supervisor")}
        for item in spans:
            agent=str(item.get("agent") or "")
            if agent in agent_evidence and item.get("category")=="agent":agent_evidence[agent]["spans"].append(item)
        for item in tools:
            agent=span_agent(item.get("span_id"))
            if agent in agent_evidence:agent_evidence[agent]["tools"].append(item)
        for item in llm:
            agent=str(item.get("agent") or span_agent(item.get("span_id")) or "")
            if agent in agent_evidence:agent_evidence[agent]["llm_calls"].append(item)

        planning_event=matching_event("planning_completed")
        if planning_event:
            planning_valid=bool((planning_event.get("data") or {}).get("valid",planning_event.get("status")=="completed"))
            agent_evidence["Planner"]["checks"].append(event_check(planning_event,"planning_valid",planning_valid,"Planning contract completed and validated","Planning contract did not validate",node="planning",subgraph="planning"))
        implementation_event=matching_event("implementation_validation_completed")
        if implementation_event:
            implementation_valid=bool((implementation_event.get("data") or {}).get("valid",False))
            agent_evidence["Developer"]["checks"].append(event_check(implementation_event,"implementation_valid",implementation_valid,"Implementation validation completed successfully","Implementation validation failed",node="validate_implementation",subgraph="implementation"))
        if environment_attempt:
            agent_evidence["Developer"]["checks"].append(event_check(environment_event,"environment_prepared",environment_prepared,"Environment preparation completed successfully","Environment preparation was attempted but final state was not prepared",node="execute_prepare_environment",subgraph="implementation"))
        test_event=matching_event("test_run_completed")
        if test_event:
            tests_passed=bool((test_event.get("data") or {}).get("passed",False))
            agent_evidence["QA"]["checks"].append(event_check(test_event,"qa_tests_passed",tests_passed,"QA test run completed successfully","QA test run completed without passing",node="execute_tests",subgraph="testing_repair"))
        supervisor_events=[item for item in events if item["event_type"]=="supervisor_decision_completed"]
        if supervisor_events:
            routing_success=all(item.get("status")=="completed" and not (item.get("data") or {}).get("loop_detected") and int((item.get("data") or {}).get("invalid_decision_count") or 0)==0 for item in supervisor_events)
            agent_evidence["Supervisor"]["checks"].append(event_check(supervisor_events[-1],"supervisor_routing_success",routing_success,"Supervisor routing completed without invalid decisions or loops","Supervisor routing contained an invalid decision or loop",node="supervisor",subgraph="supervisor"))
        if int((registry or {}).get("repair_attempts") or 0)>0:
            repair_event=matching_event("repair_completed") or matching_event("testing_completed")
            if repair_event:agent_evidence["Repair"]["checks"].append(event_check(repair_event,"repair_completed",repair_event.get("status")=="completed","Repair completed","Repair did not complete",subgraph="testing_repair"))
        return {
            "workflow_id":workflow_id,"trace_id":selected_trace,"execution_id":trace.get("execution_id") if trace else None,
            "terminal_status":str((registry or {}).get("terminal_status") or (trace or {}).get("status") or "unknown"),
            "registry_present":registry is not None,"tests_passed":bool((registry or {}).get("tests_passed")),
            "files_created":len(files),"project_created":bool(files) or "project_created" in event_types,
            "environment_prepared":environment_prepared,"environment_attempted":environment_attempt,"environment_reference":environment_event.get("event_id") if environment_event else None,
            "active_spans":sum(item.get("status") in {"running","waiting"} for item in spans),
            "secrets_detected":bool(SECRET_PATTERN.search(persisted_summaries)),
            "budget_exceeded":any(item.get("reason_code")=="budget_exceeded" and item.get("decision")=="block" for item in budget_events),
            "tool_success_rate":1-tool_errors/len(tools) if tools else 1.0,
            "handled_error_count":len(handled),"recovered_error_count":len(recovered),"unhandled_error_count":len(unhandled),
            "approval_pending":bool((registry or {}).get("pending_operation")),
            "retry_count":sum(int(item.get("retry_attempt") or 0)>0 for item in llm),
            "repair_count":int((registry or {}).get("repair_attempts") or 0),
            "tool_error_rate":tool_errors/len(tools) if tools else 0.0,"llm_error_rate":llm_errors/len(llm) if llm else 0.0,
            "duration_ms":duration,"workflow_wall_clock_duration_ms":duration,
            "agent_duration_ms":sum(float(item.get("duration_ms") or 0) for item in spans if item.get("agent")),
            "agent_active_duration_total_ms":sum(float(item.get("duration_ms") or 0) for item in spans if item.get("agent")),
            "cost_usd":sum(float(item.get("total_cost") or item.get("estimated_total_cost") or 0) for item in costs),
            "total_tokens":sum(int(item.get("total_tokens") or 0) for item in llm),
            "slow_span_ratio":sum(bool(item.get("is_slow")) for item in spans)/len(spans) if spans else 0.0,
            "approval_wait_ratio":min(1,sum(float(item.get("duration_ms") or 0) for item in approval_spans)/duration) if duration else 0.0,
            "test_reference":next((item["event_id"] for item in reversed(events) if "test" in item["event_type"]),None),
            "artifact_reference":files[0]["path"] if files else None,"budget_reference":budget_events[-1]["budget_event_id"] if budget_events else None,
            "approval_reference":next((item["event_id"] for item in reversed(events) if "approval" in item["event_type"]),None),
            "agents":sorted({str(item["agent"]) for item in spans if item.get("agent")}),"agent_evidence":agent_evidence,
            "git_repository_initialized":bool(git_state),
            "git_evidence_available":bool(git_state),
            "git_workflow_branch_created":str(git_state.get("workflow_branch") or "").startswith("workflow/"),
            "git_changes_committed":bool(git_commits),
            "git_commit_after_tests":commits_after_tests,
            "git_protected_branch_respected":bool(git_state) and str(git_state.get("workflow_branch") or "").startswith("workflow/"),
            "git_working_tree_clean_terminal":bool(git_state.get("working_tree_clean")),
            "git_reference":git_commits[-1].get("operation_id") if git_commits else workflow_id,
            "git_promotion_evidence_available":bool(promotion),
            "git_promotion_prepared":bool(promotion),
            "git_promotion_conflict_free":bool(promotion) and int(promotion.get("conflict_count") or 0)==0,
            "git_promotion_approved":promotion.get("status") in {"approved","completed"},
            "git_promotion_completed":promotion.get("status")=="completed",
            "git_base_branch_advanced_safely":not bool(promotion_preview.get("base_advanced")) or promotion.get("status") in {"completed","awaiting_approval","approved"},
            "git_no_direct_protected_branch_commit":not any(str(item.get("branch") or "") in {"main","master","develop"} for item in git_commits),
            "git_promotion_reference":promotion.get("promotion_id"),
        }

    @staticmethod
    def _idempotency(request: EvaluationRunCreate, trace_id: str | None, rubric_signature: str) -> str:
        raw="|".join((request.workflow_id,trace_id or "none",request.branch_id,rubric_signature,EVALUATOR_VERSION,request.evaluation_type,request.model or "default"))
        return hashlib.sha256(raw.encode()).hexdigest()

    async def evaluate(self, request: EvaluationRunCreate) -> dict[str, Any]:
        context=await self._context(request.workflow_id,request.branch_id,request.trace_id)
        rubrics=await self.store.rubrics();active_rubrics={}
        for item in rubrics:
            if item["enabled"] and item["agent_name"] not in active_rubrics:active_rubrics[item["agent_name"]]=item
        selected=next((item for item in rubrics if item["rubric_id"]==request.rubric_id),None) if request.rubric_id else None
        if selected:active_rubrics[selected["agent_name"]]=selected
        workflow_rubric=active_rubrics.get("Workflow")
        rubric_signature=",".join(f"{agent}:{item['rubric_id']}:{item['version']}" for agent,item in sorted(active_rubrics.items())) or "none"
        legacy_rubric=selected or workflow_rubric
        run,created=await self.store.create_run({"workflow_id":request.workflow_id,"trace_id":context.get("trace_id"),"branch_id":request.branch_id,"execution_id":context.get("execution_id"),"evaluation_type":request.evaluation_type,"evaluator_version":EVALUATOR_VERSION,"rubric_id":(legacy_rubric or {}).get("rubric_id"),"rubric_version":str((legacy_rubric or {}).get("version") or "1.0"),"model":request.model or self.default_model,"idempotency_key":self._idempotency(request,context.get("trace_id"),rubric_signature)},force=request.force)
        if not created:return await self.store.detail(run["evaluation_run_id"])
        run_id=run["evaluation_run_id"]
        try:
            workflow_deterministic=deterministic(context)
            workflow_heuristic=heuristic(context)
            agent_deterministic_metrics={agent:agent_deterministic(context,agent) for agent in ("Planner","Developer","QA","Repair","Supervisor")}
            agent_heuristic_metrics={agent:agent_heuristic(context,agent) for agent in ("Planner","Developer","QA","Repair","Supervisor")}
            selected_metrics=[]
            if request.evaluation_type in {"deterministic","comprehensive"}:
                selected_metrics.extend(workflow_deterministic)
                for items in agent_deterministic_metrics.values():selected_metrics.extend(items)
            if request.evaluation_type in {"heuristic","comprehensive"}:
                selected_metrics.extend(workflow_heuristic)
                for items in agent_heuristic_metrics.values():selected_metrics.extend(items)
            for item in selected_metrics:await self._persist_metric(run_id,item)

            agent_scores={};agent_heuristic_scores={}
            for agent in ("Planner","Developer","QA","Repair","Supervisor"):
                components=[]
                if request.evaluation_type in {"deterministic","comprehensive"} and agent_deterministic_metrics[agent]:
                    components.append(sum(item.score for item in agent_deterministic_metrics[agent] if item.contributes_to_score)/sum(item.contributes_to_score for item in agent_deterministic_metrics[agent]))
                if request.evaluation_type in {"heuristic","comprehensive"} and agent_heuristic_metrics[agent]:
                    heuristic_agent_score=bounded(sum(item.score for item in agent_heuristic_metrics[agent] if item.contributes_to_score)/sum(item.contributes_to_score for item in agent_heuristic_metrics[agent]))
                    components.append(heuristic_agent_score);agent_heuristic_scores[agent]=heuristic_agent_score
                has_agent_evidence=bool(agent_deterministic_metrics[agent] or agent_heuristic_metrics[agent])
                if request.evaluation_type in {"llm_judge","comprehensive"} and self.openai_client is not None and agent in {"Planner","Developer","QA","Repair"} and has_agent_evidence:
                    rubric=active_rubrics.get(agent)
                    judge=await self._judge(agent,context,request.model or self.default_model,rubric)
                    dimensions=self._dimension_scores(judge)
                    judged_score=weighted_score(dimensions,rubric["weights"]) if rubric else judge.score
                    components.append(judged_score)
                    for name,value in dimensions.items():await self._persist_metric(run_id,{"agent_name":agent,"evaluation_scope":"agent","metric_name":name,"metric_value":value,"metric_type":"llm_judge","unit":"score","threshold":.7,"passed":value>=.7,"formula":"structured LLM judge rubric","source_state":"provider_reported"})
                score=bounded(sum(components)/len(components)) if components else None
                if score is None:
                    result_verdict="not_evaluated";reason=f"Insufficient agent-specific {request.evaluation_type.replace('_',' ')} evidence";confidence=0
                else:
                    result_verdict=verdict(score);confidence=.8 if request.evaluation_type=="llm_judge" else 1
                    reason={"deterministic":"Deterministic agent-specific evidence aggregate","heuristic":"Heuristic agent-specific evidence aggregate","llm_judge":"LLM judge agent-specific evidence aggregate","comprehensive":"Deterministic, heuristic and LLM judge aggregate"}[request.evaluation_type]
                    agent_scores[agent]=score
                    rubric=active_rubrics.get(agent)
                    if rubric:await self.store.bind_run_rubric(run_id,rubric,request.evaluation_type)
                await self.store.add_result(run_id,{"agent_name":agent,"evaluator_name":request.evaluation_type,"score":score,"verdict":result_verdict,"confidence":confidence,"reason":reason})

            scoreable_deterministic=[item for item in workflow_deterministic if item.contributes_to_score]
            for items in agent_deterministic_metrics.values():scoreable_deterministic.extend(item for item in items if item.contributes_to_score)
            heuristic_workflow_score,dimension_scores,dimension_weights=heuristic_dimension_aggregate(workflow_heuristic,agent_heuristic_scores,self.heuristic_dimension_weights)
            if request.evaluation_type in {"heuristic","comprehensive"}:
                for name,value in dimension_scores.items():
                    await self._persist_metric(run_id,{"evaluation_scope":"workflow","metric_name":name,"metric_value":value,"metric_type":"heuristic_dimension","unit":"score","threshold":.7,"passed":value>=.7,"formula":f"weighted workflow heuristic dimension; overall weight={dimension_weights.get(name,0):.6f}","raw_value":value,"source_state":"derived_from_normalized_metrics","contributes_to_score":True})
            if request.evaluation_type=="deterministic":
                workflow_score=bounded(sum(item.score for item in scoreable_deterministic)/len(scoreable_deterministic))
                workflow_reason=f"Deterministic evidence aggregate: arithmetic mean of {len(scoreable_deterministic)} attributable checks with equal weight"
            elif request.evaluation_type=="heuristic":
                workflow_score=heuristic_workflow_score
                labels={"reliability_score":"reliability","latency_score":"latency","cost_efficiency_score":"cost efficiency","human_wait_score":"human wait","agent_efficiency_score":"agent efficiency"}
                weights_summary=", ".join(f"{labels[name]} {weight:.0%}" for name,weight in dimension_weights.items())
                workflow_reason=f"Heuristic weighted dimension aggregate: {weights_summary}"
            else:
                workflow_score=aggregate_workflow(agent_scores,heuristic_workflow_score)
                workflow_reason="Deterministic, heuristic and LLM judge aggregate over evaluated agents with renormalized weights"
            if request.evaluation_type in {"llm_judge","comprehensive"} and self.openai_client is not None:
                workflow_judge=await self._judge("Workflow",context,request.model or self.default_model,workflow_rubric)
                dimensions=self._dimension_scores(workflow_judge)
                judged_score=weighted_score(dimensions,workflow_rubric["weights"]) if workflow_rubric else workflow_judge.score
                for name,value in dimensions.items():await self._persist_metric(run_id,{"agent_name":"Workflow","evaluation_scope":"workflow","metric_name":name,"metric_value":value,"metric_type":"llm_judge","unit":"score","threshold":.7,"passed":value>=.7,"formula":"structured LLM judge workflow rubric","source_state":"provider_reported"})
                workflow_score=judged_score if request.evaluation_type=="llm_judge" else bounded(.75*workflow_score+.25*judged_score)
                if request.evaluation_type=="llm_judge":workflow_reason="LLM judge workflow evidence aggregate"
            if workflow_rubric:await self.store.bind_run_rubric(run_id,workflow_rubric,request.evaluation_type)
            await self.store.add_result(run_id,{"agent_name":"Workflow","evaluator_name":"aggregate","score":workflow_score,"verdict":verdict(workflow_score),"confidence":1,"reason":workflow_reason})
            await self.store.finish_run(run_id,score=workflow_score,verdict=verdict(workflow_score))
        except Exception as exc:
            error=f"invalid_judge_schema: {exc}" if isinstance(exc,InvalidStrictJsonSchema) else f"{type(exc).__name__}: {exc}"
            await self.store.finish_run(run_id,score=None,verdict=None,error=error)
        return await self.store.detail(run_id)

    async def _persist_metric(self, run_id: str, item) -> None:
        data=item.__dict__ if hasattr(item,"__dict__") else item
        await self.store.add_metric(run_id,data)
        await self.store.add_evidence(run_id,{"evidence_type":data.get("evidence_type") or "metric","reference_id":data.get("reference_id"),"summary":data.get("summary") or f"{data['metric_name']} evaluation","metadata":{"metric_name":data["metric_name"],"scope":data.get("evaluation_scope") or ("agent" if data.get("agent_name") else "workflow"),"agent_name":data.get("agent_name"),"node":data.get("node"),"subgraph":data.get("subgraph"),"trace_id":data.get("reference_id") if (data.get("evidence_type") or "")=="trace" else None,"span_id":data.get("reference_id") if (data.get("evidence_type") or "")=="span" else None,"source_state":data.get("source_state"),"raw_value":data.get("raw_value"),"contributes_to_score":bool(data.get("contributes_to_score",True))}})

    async def _judge(self, agent: str, context: dict[str, Any], model: str | None, rubric: dict[str,Any] | None=None) -> JudgeOutput:
        if not model:raise RuntimeError("LLM judge model is not configured")
        strict_json_schema_for_model(JudgeOutput)
        if rubric is None:raise RuntimeError(f"No rubric available for {agent}")
        if agent=="Workflow":
            evidence={key:context[key] for key in ("terminal_status","tests_passed","files_created","project_created","environment_prepared","tool_success_rate","repair_count","cost_usd","total_tokens")}
        else:
            attributable=(context.get("agent_evidence") or {}).get(agent) or {}
            evidence={"agent":agent,"span_statuses":[item.get("status") for item in attributable.get("spans") or []],"tool_statuses":[item.get("status") for item in attributable.get("tools") or []],"llm_statuses":[item.get("status") for item in attributable.get("llm_calls") or []],"checks":[{"name":item.get("metric_name"),"passed":bool(item.get("passed"))} for item in attributable.get("checks") or []]}
        payload=json.dumps({"agent":agent,"dimensions":list(rubric["weights"]),"evidence":evidence},separators=(",",":"))
        obs_context=await self.observability.ensure_trace(context["workflow_id"],source="evaluation") if self.observability else None
        if obs_context:
            obs_context=obs_context.child(agent="Evaluator",node="llm_judge",operation="llm_judge")
            with observability_context(obs_context):response=await self.openai_client.responses.parse(model=model,input=payload,text_format=JudgeOutput,_observability_operation="llm_judge")
        else:response=await self.openai_client.responses.parse(model=model,input=payload,text_format=JudgeOutput,_observability_operation="llm_judge")
        judge=JudgeOutput.model_validate(response.output_parsed)
        if judge.agent_name!=agent:raise ValueError("JudgeOutput agent_name does not match the requested agent")
        expected=set(rubric["weights"]);actual={item.name for item in judge.dimensions}
        if len(actual)!=len(judge.dimensions) or actual!=expected:
            raise ValueError("JudgeOutput dimensions do not match the active rubric")
        return judge

    @staticmethod
    def _dimension_scores(judge: JudgeOutput) -> dict[str, float]:
        return {item.name:item.score for item in judge.dimensions}

    async def resolve_current_metric(self, evaluation_run_id: str, metric: str, *, run: dict[str, Any] | None = None) -> dict[str, Any]:
        current_run=run or await self.store.detail(evaluation_run_id)
        direction=metric_direction(metric)
        if current_run is None:return {"metric":metric,"status":"unavailable","reason":"evaluation_run_not_found","direction":direction,"value":None,"source":None}

        run_field={"overall_score":"overall_score","workflow_score":"overall_score","duration_ms":"duration_ms"}.get(metric)
        if run_field:
            value=current_run.get(run_field)
            return {"metric":metric,"status":"available" if value is not None else "unavailable","reason":None if value is not None else "metric_not_available","direction":direction,"value":float(value) if value is not None else None,"source":f"agent_evaluation_runs.{run_field}"}

        derived_queries={
            "evaluation_cost":"""SELECT COUNT(*) AS samples,SUM(COALESCE(x.total_cost,x.estimated_total_cost)) AS value
                FROM llm_cost_calculations x JOIN observability_llm_calls c ON c.call_id=x.llm_call_id
                WHERE x.superseded=0 AND x.workflow_id=? AND x.branch_id=? AND x.agent_name='Evaluator'
                AND c.timestamp>=? AND c.timestamp<=?""",
            "llm_judge_cost":"""SELECT COUNT(*) AS samples,SUM(COALESCE(x.total_cost,x.estimated_total_cost)) AS value
                FROM llm_cost_calculations x JOIN observability_llm_calls c ON c.call_id=x.llm_call_id
                WHERE x.superseded=0 AND x.workflow_id=? AND x.branch_id=? AND x.agent_name='Evaluator'
                AND x.operation='llm_judge' AND c.timestamp>=? AND c.timestamp<=?""",
            "total_tokens":"""SELECT COUNT(*) AS samples,SUM(total_tokens) AS value FROM observability_llm_calls
                WHERE workflow_id=? AND branch_id=? AND agent='Evaluator' AND usage_available=1
                AND timestamp>=? AND timestamp<=?""",
        }
        if metric in derived_queries and current_run.get("completed_at"):
            query=derived_queries[metric]
            row=await self.store.fetch_one(query,(current_run["workflow_id"],current_run["branch_id"],current_run["started_at"],current_run["completed_at"]))
            value=row.get("value") if row and int(row.get("samples") or 0)>0 else None
            return {"metric":metric,"status":"available" if value is not None else "unavailable","reason":None if value is not None else "metric_not_available","direction":direction,"value":float(value) if value is not None else None,"source":"durable_evaluation_aggregate"}

        candidates=[item for item in current_run["metrics"] if item["metric_name"]==metric]
        workflow_candidates=[item for item in candidates if item.get("evaluation_scope")=="workflow"]
        selected=workflow_candidates or candidates
        if not selected:return {"metric":metric,"status":"unavailable","reason":"metric_not_available","direction":direction,"value":None,"source":None}
        value=sum(float(item["metric_value"]) for item in selected)/len(selected)
        source="agent_evaluation_metrics" if len(selected)==1 else "agent_evaluation_metrics_average"
        return {"metric":metric,"status":"available","reason":None,"direction":direction,"value":value,"source":source}

    async def compare(self, run_id: str, baseline_id: str | None, threshold: float):
        run=await self.store.detail(run_id)
        if not run:return None
        baselines=await self.store.baselines()
        selected=[item for item in baselines if item["baseline_id"]==baseline_id] if baseline_id else (baselines if run.get("validity_status","valid")=="valid" else [])
        if not selected:return {"evaluation_run_id":run_id,"baselines":[],"comparisons":[],"regressions":[]}
        regressions=[]
        types={"overall_score":"score_regression","workflow_score":"score_regression","evaluation_cost":"cost_regression","llm_judge_cost":"cost_regression","duration_ms":"latency_regression","workflow_duration":"latency_regression","workflow_wall_clock_duration":"latency_regression","tool_error_rate":"tool_error_regression","retry_count":"retry_regression"}
        comparisons=[]
        for baseline in selected:
            resolved=await self.resolve_current_metric(run_id,baseline["metric"],run=run)
            baseline_value=float(baseline["score"])
            if resolved["status"]!="available":
                comparisons.append({"baseline":baseline,"metric":baseline["metric"],"baseline_value":baseline_value,"current_value":None,"current":None,"delta":None,"direction":resolved["direction"],"threshold":threshold,"regression":False,"status":"unavailable","reason":resolved["reason"],"source":resolved["source"]})
                continue
            current=float(resolved["value"])
            baseline_decimal=Decimal(str(baseline_value));current_decimal=Decimal(str(current))
            degradation=(baseline_decimal-current_decimal) if resolved["direction"]=="higher_is_better" else (current_decimal-baseline_decimal)
            delta=float(degradation)
            is_regression=degradation>=Decimal(str(threshold))
            comparison={"baseline":baseline,"metric":baseline["metric"],"baseline_value":baseline_value,"current_value":current,"current":current,"delta":delta,"direction":resolved["direction"],"threshold":threshold,"regression":is_regression,"status":"available","reason":None,"source":resolved["source"]}
            comparisons.append(comparison)
            if is_regression:regressions.append({"type":types.get(baseline["metric"],"score_regression" if resolved["direction"]=="higher_is_better" else "metric_regression"),"metric":baseline["metric"],"baseline_value":baseline_value,"current_value":current,"delta":delta,"direction":resolved["direction"],"threshold":threshold,"baseline_id":baseline["baseline_id"]})
        for regression in regressions:
            exists=await self.store.fetch_one("SELECT evidence_id FROM agent_evaluation_evidence WHERE evaluation_run_id=? AND evidence_type='regression' AND reference_id=?",(run_id,regression["baseline_id"]))
            if not exists:await self.store.add_evidence(run_id,{"evidence_type":"regression","reference_id":regression["baseline_id"],"summary":f"{regression['type']} detected for {regression['metric']}","metadata":regression})
        return {"evaluation_run_id":run_id,"baselines":selected,"comparisons":comparisons,"regressions":regressions}

    async def dashboard(self, *, include_invalidated: bool=False):
        validity="" if include_invalidated else " AND validity_status='valid'"
        runs=await self.store.fetch_all(f"SELECT * FROM agent_evaluation_runs WHERE status='completed'{validity}")
        result_validity="" if include_invalidated else " WHERE r.validity_status='valid'"
        results=await self.store.fetch_all(f"""SELECT x.* FROM agent_evaluation_results x
            JOIN agent_evaluation_runs r ON r.evaluation_run_id=x.evaluation_run_id{result_validity}""")
        run_costs=[]
        for run in runs:
            resolved=await self.resolve_current_metric(run["evaluation_run_id"],"evaluation_cost",run={**run,"metrics":[]})
            run_costs.append(float(resolved["value"]) if resolved["status"]=="available" else 0.0)
        scores=[float(item["overall_score"]) for item in runs if item.get("overall_score") is not None]
        verdicts={name:sum(item.get("verdict")==name for item in runs) for name in ("excellent","good","acceptable","needs_improvement","failed")}
        average_cost=sum(run_costs)/max(1,len(run_costs))
        agent_groups={name:[float(item["score"]) for item in results if item["agent_name"]==name and item.get("score") is not None] for name in {str(item["agent_name"]) for item in results if item["agent_name"]!="Workflow"}}
        agent_groups={name:values for name,values in agent_groups.items() if values}
        model_groups={name:[float(item["overall_score"]) for item in runs if item.get("model")==name and item.get("overall_score") is not None] for name in {str(item["model"]) for item in runs if item.get("model")}}
        regression_validity="" if include_invalidated else " AND r.validity_status='valid'"
        regressions=await self.store.fetch_all(f"""SELECT x.created_at FROM agent_evaluation_evidence x
            JOIN agent_evaluation_runs r ON r.evaluation_run_id=x.evaluation_run_id
            WHERE x.evidence_type='regression'{regression_validity}""")
        evaluated_agent_scores=[float(item["score"]) for item in results if item["agent_name"]!="Workflow" and item.get("score") is not None]
        return {"workflows_evaluated":len({item["workflow_id"] for item in runs}),"average_workflow_score":sum(scores)/len(scores) if scores else None,"verdicts":verdicts,"regression_count":len(regressions),"average_agent_score":sum(evaluated_agent_scores)/len(evaluated_agent_scores) if evaluated_agent_scores else None,"average_evaluation_cost":average_cost,"average_evaluation_duration_ms":sum(float(item.get("duration_ms") or 0) for item in runs)/max(1,len(runs)),"score_over_time":[{"created_at":item["created_at"],"score":item["overall_score"]} for item in runs if item.get("overall_score") is not None],"score_by_agent":[{"key":name,"score":sum(values)/len(values)} for name,values in agent_groups.items()],"score_by_model":[{"key":name,"score":sum(values)/len(values)} for name,values in model_groups.items()],"score_vs_cost":[{"score":item["overall_score"],"cost":run_costs[index]} for index,item in enumerate(runs) if item.get("overall_score") is not None],"score_vs_duration":[{"score":item["overall_score"],"duration_ms":item["duration_ms"]} for item in runs if item.get("overall_score") is not None],"regressions_over_time":[{"created_at":item["created_at"],"count":1} for item in regressions]}
