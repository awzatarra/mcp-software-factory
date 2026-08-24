from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.agent_evaluation_models import EvaluationRunCreate, JudgeOutput
from api.app import create_app
from api.services.agent_evaluation_service import AgentEvaluationService, EVALUATOR_VERSION, metric_direction
from api.services.agent_evaluation_store import AgentEvaluationStore
from api.services.agent_evaluators import agent_deterministic, agent_heuristic, aggregate_workflow, bounded, deterministic, heuristic, heuristic_dimension_aggregate, verdict, weighted_score
from api.services.llm_cost_service import LLMCostService
from api.services.llm_cost_store import LLMCostStore
from api.services.llm_observability import ObservableOpenAIClient
from api.services.observability_service import ObservabilityService
from api.services.observability_store import ObservabilityStore
from api.services.strict_json_schema import InvalidStrictJsonSchema, strict_json_schema_for_model, validate_strict_json_schema
from streaming.migrations import WORKFLOW_EVENT_TABLES


@pytest.fixture
async def evaluations(tmp_path):
    database=tmp_path/"evaluations.sqlite"
    observability=ObservabilityStore(database);await observability.initialize()
    costs=LLMCostStore(database);await costs.initialize()
    store=AgentEvaluationStore(database);await store.initialize()
    now=datetime.now(UTC).isoformat()
    with sqlite3.connect(database) as connection:
        connection.executescript(WORKFLOW_EVENT_TABLES)
        connection.execute("INSERT INTO workflow_registry(thread_id,project_name,workflow_intent,terminal_status,tests_executed,tests_passed,repair_phase,created_at) VALUES(?,?,?,?,?,?,?,?)",("workflow-1","demo","create","completed",1,1,"not_started",now))
        connection.execute("INSERT INTO workflow_project_files VALUES(?,?,?,?,?,?,?)",("workflow-1","original","demo/main.py","created","create_project",now,now))
        for sequence,event_type in enumerate(("project_created","environment_prepared","tests_completed"),1):
            connection.execute("INSERT INTO workflow_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(f"event-{sequence}","workflow-1","original",sequence,event_type,now,"test","workflow","completed",event_type,"{}",None,"original",None,now))
    await observability.create_trace({"trace_id":"trace-1","workflow_id":"workflow-1","branch_id":"original","name":"workflow","status":"completed","started_at":now})
    for index,agent in enumerate(("Planner","Developer","QA","Supervisor"),1):
        await observability.start_span({"span_id":f"span-{index}","trace_id":"trace-1","name":agent,"category":"agent","kind":"internal","status":"running","workflow_id":"workflow-1","branch_id":"original","agent":agent,"started_at":now})
        await observability.end_span(f"span-{index}",status="completed",ended_at=now,duration_ms=100)
    service=AgentEvaluationService(store)
    return service,store,database


def context(**updates):
    values={"workflow_id":"w","trace_id":"t","terminal_status":"completed","registry_present":True,"tests_passed":True,"files_created":2,"project_created":True,"environment_prepared":True,"environment_attempted":True,"environment_reference":"environment-event","active_spans":0,"secrets_detected":False,"budget_exceeded":False,"tool_success_rate":1.0,"handled_error_count":0,"recovered_error_count":0,"unhandled_error_count":0,"approval_pending":False,"retry_count":0,"repair_count":0,"tool_error_rate":0.0,"llm_error_rate":0.0,"duration_ms":1000.0,"workflow_wall_clock_duration_ms":1000.0,"agent_duration_ms":500.0,"agent_active_duration_total_ms":500.0,"cost_usd":0.01,"total_tokens":1000,"slow_span_ratio":0.0,"approval_wait_ratio":0.0,"test_reference":"test","artifact_reference":"file","budget_reference":None,"approval_reference":None,"agent_evidence":{name:{"spans":[],"tools":[],"llm_calls":[],"checks":[]} for name in ("Planner","Developer","QA","Repair","Supervisor")}}
    values.update(updates);return values


def test_deterministic_and_heuristic_evaluators_are_bounded_and_explain_formulas():
    deterministic_metrics=deterministic(context())
    heuristic_metrics=heuristic(context(retry_count=2,tool_error_rate=.25,duration_ms=300000))
    assert {item.metric_name for item in deterministic_metrics}>={"schema_valid","tests_passed","no_secrets_detected","budget_respected"}
    assert all(item.passed for item in deterministic_metrics)
    assert all(0<=item.score<=1 and item.formula for item in heuristic_metrics)
    assert next(item for item in heuristic_metrics if item.metric_name=="retry_count").score==pytest.approx(1/3,abs=1e-6)
    assert bounded(-1)==0 and bounded(2)==1


def test_git_workflow_evidence_is_deterministic_and_does_not_change_scoring():
    metrics=deterministic(context(
        git_evidence_available=True,
        git_repository_initialized=True,
        git_workflow_branch_created=True,
        git_changes_committed=True,
        git_commit_after_tests=True,
        git_protected_branch_respected=True,
        git_working_tree_clean_terminal=True,
        git_reference="git-operation-1",
    ))
    git_metrics=[item for item in metrics if item.metric_name.startswith("git_")]
    assert len(git_metrics)==6
    assert all(item.passed and not item.contributes_to_score for item in git_metrics)


def test_git_promotion_evidence_is_diagnostic_and_non_scoring():
    metrics=deterministic(context(
        git_promotion_evidence_available=True,
        git_promotion_prepared=True,
        git_promotion_conflict_free=True,
        git_promotion_approved=True,
        git_promotion_completed=True,
        git_base_branch_advanced_safely=True,
        git_no_direct_protected_branch_commit=True,
        git_promotion_reference="promotion-1",
    ))
    promotion=[item for item in metrics if item.metric_name.startswith("git_promotion_") or item.metric_name in {"git_base_branch_advanced_safely","git_no_direct_protected_branch_commit"}]
    assert len(promotion)==6
    assert all(item.passed and not item.contributes_to_score for item in promotion)


def test_environment_and_unhandled_error_evidence_are_consistent():
    attempted=deterministic(context(environment_prepared=False,environment_attempted=True,handled_error_count=2,recovered_error_count=1))
    environment=next(item for item in attempted if item.metric_name=="environment_prepared")
    no_unhandled=next(item for item in attempted if item.metric_name=="no_unhandled_exception")
    assert environment.passed is False and "attempted" in environment.summary and "not prepared" in environment.summary
    assert no_unhandled.passed is True and "2 handled" in no_unhandled.summary and "1 recovered" in no_unhandled.summary
    truly_unhandled=next(item for item in deterministic(context(unhandled_error_count=1)) if item.metric_name=="no_unhandled_exception")
    assert truly_unhandled.passed is False and "1 unhandled" in truly_unhandled.summary


def test_agent_metric_isolation_and_different_scores():
    evidence=context()["agent_evidence"]
    evidence["Planner"]["spans"]=[{"span_id":"planner-1","status":"completed","node":"planning","duration_ms":10}]
    evidence["Developer"]["spans"]=[{"span_id":"developer-1","status":"completed","node":"implementation","duration_ms":10},{"span_id":"developer-2","status":"failed","node":"implementation","duration_ms":10}]
    planner=agent_deterministic(context(agent_evidence=evidence),"Planner")
    developer=agent_deterministic(context(agent_evidence=evidence),"Developer")
    qa=agent_deterministic(context(agent_evidence=evidence),"QA")
    assert planner[0].score==1 and developer[0].score==.5 and not qa
    assert all(item.agent_name=="Planner" and item.evaluation_scope=="agent" for item in planner)
    assert all(item.agent_name=="Developer" and item.evaluation_scope=="agent" for item in developer)


def test_parallel_agent_time_is_diagnostic_and_wall_clock_drives_latency():
    metrics=heuristic(context(workflow_wall_clock_duration_ms=1000,agent_active_duration_total_ms=5000))
    wall=next(item for item in metrics if item.metric_name=="workflow_wall_clock_duration")
    active=next(item for item in metrics if item.metric_name=="agent_active_duration_total")
    parallelism=next(item for item in metrics if item.metric_name=="parallelism_factor")
    assert wall.raw_value==1000 and wall.score==pytest.approx(1/(1+1000/300000),abs=1e-6)
    assert active.raw_value==5000 and active.contributes_to_score is False
    assert parallelism.raw_value==5 and parallelism.contributes_to_score is False


def test_agent_duration_uses_only_attributable_spans():
    evidence=context()["agent_evidence"]
    evidence["Developer"]["spans"]=[{"span_id":"a","status":"completed","duration_ms":40},{"span_id":"b","status":"completed","duration_ms":60}]
    metric=next(item for item in agent_heuristic(context(agent_evidence=evidence),"Developer") if item.metric_name=="agent_duration")
    assert metric.raw_value==100 and metric.agent_name=="Developer" and metric.evaluation_scope=="agent"


def test_heuristic_dimension_aggregation_and_configurable_weights():
    metrics=heuristic(context(retry_count=0,repair_count=0,approval_wait_ratio=.25,cost_usd=.05))
    overall,dimensions,weights=heuristic_dimension_aggregate(metrics,{"Developer":.8,"QA":.6})
    expected=sum(dimensions[name]*weights[name] for name in dimensions)
    assert overall==pytest.approx(expected,abs=1e-6)
    assert set(dimensions)=={"reliability_score","latency_score","cost_efficiency_score","human_wait_score","agent_efficiency_score"}
    custom,_,custom_weights=heuristic_dimension_aggregate(metrics,{"Developer":.8},{"reliability_score":1})
    assert custom==dimensions["reliability_score"] and custom_weights=={"reliability_score":1}
    assert next(item for item in metrics if item.metric_name=="agent_active_duration_total").contributes_to_score is False


def test_heuristic_raw_values_are_separate_from_normalized_scores():
    metrics={item.metric_name:item for item in heuristic(context(retry_count=0,repair_count=0,approval_wait_ratio=.25,cost_usd=.05))}
    assert metrics["retry_count"].raw_value==0 and metrics["retry_count"].metric_value==1
    assert metrics["repair_count"].raw_value==0 and metrics["repair_count"].metric_value==1
    assert metrics["approval_wait_ratio"].raw_value==.25 and metrics["approval_wait_ratio"].metric_value==.75
    assert metrics["cost_efficiency"].raw_value==.05 and metrics["cost_efficiency"].metric_value==pytest.approx(2/3,abs=1e-6)
    assert all(item.source_state for item in metrics.values())


def test_weighted_rubric_verdicts_and_missing_repair_renormalization():
    score=weighted_score({"a":1,"b":.5},{"a":.75,"b":.25})
    assert score==.875 and verdict(.91)=="excellent" and verdict(.81)=="good"
    assert verdict(.71)=="acceptable" and verdict(.51)=="needs_improvement" and verdict(.49)=="failed"
    without=aggregate_workflow({"Planner":.8,"Developer":.8,"QA":.8},.8)
    with_repair=aggregate_workflow({"Planner":.8,"Developer":.8,"QA":.8,"Repair":0},.8)
    assert without==.8 and with_repair<without


@pytest.mark.asyncio
async def test_durable_evaluation_is_idempotent_and_force_creates_new_run(evaluations):
    service,store,_=evaluations
    request=EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="comprehensive")
    first=await service.evaluate(request);second=await service.evaluate(request)
    forced=await service.evaluate(request.model_copy(update={"force":True}))
    assert first["status"]=="completed" and 0<=first["overall_score"]<=1
    assert first["evaluation_run_id"]==second["evaluation_run_id"]
    assert forced["evaluation_run_id"]!=first["evaluation_run_id"]
    assert {item["agent_name"] for item in first["results"]}>={"Planner","Developer","QA","Supervisor","Workflow"}
    assert all("sk-" not in item["summary"] for item in first["evidence"])
    assert (await store.list_runs(workflow_id="workflow-1"))["total"]==2


@pytest.mark.asyncio
async def test_workflow_only_metrics_do_not_create_fake_agent_scores(evaluations):
    _service,store,_=evaluations
    service=AgentEvaluationService(store)
    synthetic=context(agent_evidence={name:{"spans":[],"tools":[],"llm_calls":[],"checks":[]} for name in ("Planner","Developer","QA","Repair","Supervisor")})
    async def supplied_context(*_args,**_kwargs):return synthetic
    service._context=supplied_context
    result=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-only",evaluation_type="deterministic",force=True))
    agents={item["agent_name"]:item for item in result["results"]}
    assert all(agents[name]["score"] is None and agents[name]["verdict"]=="not_evaluated" for name in ("Planner","Developer","QA","Repair","Supervisor"))
    assert agents["Planner"]["reason"]=="Insufficient agent-specific deterministic evidence"
    assert all(item["agent_name"] is None and item["evaluation_scope"]=="workflow" for item in result["metrics"])
    scoreable=[item for item in deterministic(synthetic) if item.contributes_to_score]
    assert result["overall_score"]==pytest.approx(sum(item.score for item in scoreable)/len(scoreable),abs=1e-6)
    assert f"arithmetic mean of {len(scoreable)}" in agents["Workflow"]["reason"]


@pytest.mark.asyncio
async def test_agent_specific_results_use_only_attributable_metrics(evaluations):
    _service,store,_=evaluations
    evidence=context()["agent_evidence"]
    evidence["Developer"]["spans"]=[{"span_id":"dev-ok","status":"completed","node":"implementation","duration_ms":20},{"span_id":"dev-failed","status":"failed","node":"implementation","duration_ms":10}]
    evidence["Developer"]["checks"]=[{"metric_name":"implementation_valid","passed":True,"reference_id":"implementation-event","summary":"Implementation validation completed successfully","source_state":"completed","node":"validate_implementation","subgraph":"implementation"}]
    service=AgentEvaluationService(store)
    async def supplied_context(*_args,**_kwargs):return context(agent_evidence=evidence)
    service._context=supplied_context
    result=await service.evaluate(EvaluationRunCreate(workflow_id="developer-only",evaluation_type="deterministic",force=True))
    agents={item["agent_name"]:item for item in result["results"]}
    assert agents["Developer"]["score"]==.75 and agents["Developer"]["verdict"]=="acceptable"
    assert agents["Developer"]["reason"]=="Deterministic agent-specific evidence aggregate"
    assert agents["Planner"]["verdict"]==agents["QA"]["verdict"]==agents["Repair"]["verdict"]=="not_evaluated"
    developer_metrics=[item for item in result["metrics"] if item["agent_name"]=="Developer"]
    assert developer_metrics and all(item["evaluation_scope"]=="agent" for item in developer_metrics)
    assert not [item for item in result["metrics"] if item["agent_name"] in {"Planner","QA","Repair"}]
    evidence_rows={item["metadata"]["metric_name"]:item["metadata"] for item in result["evidence"] if item["metadata"].get("agent_name")=="Developer"}
    assert all(item["scope"]=="agent" and item["agent_name"]=="Developer" for item in evidence_rows.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(("evaluation_type","expected"),[("heuristic","Heuristic agent-specific evidence aggregate"),("comprehensive","Deterministic, heuristic and LLM judge aggregate")])
async def test_evaluation_reason_matches_requested_type(evaluations,evaluation_type,expected):
    _service,store,_=evaluations
    evidence=context()["agent_evidence"]
    evidence["Developer"]["spans"]=[{"span_id":"dev","status":"completed","node":"implementation","duration_ms":20}]
    service=AgentEvaluationService(store)
    async def supplied_context(*_args,**_kwargs):return context(agent_evidence=evidence)
    service._context=supplied_context
    result=await service.evaluate(EvaluationRunCreate(workflow_id=f"reason-{evaluation_type}",evaluation_type=evaluation_type,force=True))
    developer=next(item for item in result["results"] if item["agent_name"]=="Developer")
    assert developer["reason"]==expected


@pytest.mark.asyncio
async def test_heuristic_run_persists_dimensions_raw_values_and_score_roles(evaluations):
    _service,store,_=evaluations
    evidence=context()["agent_evidence"]
    evidence["Developer"]["spans"]=[{"span_id":"dev","status":"completed","node":"implementation","duration_ms":200}]
    service=AgentEvaluationService(store)
    async def supplied_context(*_args,**_kwargs):return context(workflow_wall_clock_duration_ms=1000,agent_active_duration_total_ms=5000,agent_evidence=evidence)
    service._context=supplied_context
    result=await service.evaluate(EvaluationRunCreate(workflow_id="dimension-run",evaluation_type="heuristic",force=True))
    workflow=next(item for item in result["results"] if item["agent_name"]=="Workflow")
    assert workflow["reason"].startswith("Heuristic weighted dimension aggregate")
    assert set(result["dimension_scores"])=={"reliability_score","latency_score","cost_efficiency_score","human_wait_score","agent_efficiency_score"}
    active=next(item for item in result["metrics"] if item["metric_name"]=="agent_active_duration_total")
    wall=next(item for item in result["metrics"] if item["metric_name"]=="workflow_wall_clock_duration")
    developer=next(item for item in result["metrics"] if item["metric_name"]=="agent_duration" and item["agent_name"]=="Developer")
    assert active["raw_value"]==5000 and active["contributes_to_score"] is False
    assert wall["raw_value"]==1000 and developer["raw_value"]==200
    evidence_item=next(item for item in result["evidence"] if item["metadata"]["metric_name"]=="approval_wait_ratio")
    assert evidence_item["metadata"]["raw_value"]==0 and evidence_item["metadata"]["contributes_to_score"] is True


@pytest.mark.asyncio
async def test_concurrent_idempotency_creates_one_non_forced_run(evaluations):
    _service,store,_=evaluations
    data={"workflow_id":"w","trace_id":"t","branch_id":"original","evaluation_type":"deterministic","evaluator_version":EVALUATOR_VERSION,"rubric_version":"1","idempotency_key":"same"}
    results=await asyncio.gather(*[store.create_run(data) for _ in range(8)])
    assert sum(created for _,created in results)==1
    assert len({item["evaluation_run_id"] for item,_ in results})==1


@pytest.mark.asyncio
async def test_legacy_result_score_is_migrated_to_nullable(tmp_path):
    database=tmp_path/"legacy-evaluations.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TABLE agent_evaluation_results (
        evaluation_result_id TEXT PRIMARY KEY,evaluation_run_id TEXT NOT NULL,agent_name TEXT NOT NULL,
        node TEXT,subgraph TEXT,evaluator_name TEXT NOT NULL,score REAL NOT NULL,verdict TEXT NOT NULL,
        confidence REAL NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL)""")
    store=AgentEvaluationStore(database);await store.initialize()
    with sqlite3.connect(database) as connection:
        score_column=next(row for row in connection.execute("PRAGMA table_info(agent_evaluation_results)") if row[1]=="score")
    assert score_column[3]==0


@pytest.mark.asyncio
async def test_llm_judge_uses_structured_output_and_sanitized_evidence(evaluations):
    _service,store,_=evaluations
    calls=[]
    class Responses:
        async def parse(self,**kwargs):
            calls.append(kwargs)
            payload=json.loads(kwargs["input"])
            return SimpleNamespace(output_parsed={
                "agent_name":payload["agent"],"score":.9,"verdict":"excellent",
                "confidence":.9,"reason":"Sanitized evidence is complete",
                "dimensions":[{"name":name,"score":.9,"reason":"Evidence supports this score","confidence":.9} for name in payload["dimensions"]],
            })
    service=AgentEvaluationService(store,openai_client=SimpleNamespace(responses=Responses()),default_model="judge-model")
    result=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="llm_judge",force=True))
    assert result["status"]=="completed" and calls
    assert all(call["model"]=="judge-model" and call["text_format"].__name__=="JudgeOutput" for call in calls)
    assert all(call["_observability_operation"]=="llm_judge" for call in calls)
    assert any(json.loads(call["input"])["agent"]=="Workflow" for call in calls)
    assert {"goal_completion","overall_quality","efficiency","reliability"}.issubset({item["metric_name"] for item in result["metrics"] if item.get("agent_name")=="Workflow"})
    assert list(result["dimension_scores"])==["Planner","Developer","QA","Workflow"]
    assert list(result["dimension_scores"]["Planner"])==["completeness","feasibility","decomposition_quality","technical_consistency"]
    assert list(result["dimension_scores"]["Developer"])==["requirement_coverage","implementation_quality","maintainability","architectural_alignment"]
    assert list(result["dimension_scores"]["QA"])==["test_coverage_quality","defect_detection_quality","validation_completeness"]
    assert list(result["dimension_scores"]["Workflow"])==["goal_completion","overall_quality","efficiency","reliability"]
    assert "Repair" not in result["dimension_scores"] and "Supervisor" not in result["dimension_scores"]
    assert {item["agent_name"] for item in result["rubrics_used"]}=={"Planner","Developer","QA","Workflow"}
    assert all(item["evaluation_type"]=="llm_judge" and item["binding_source"]=="runtime" for item in result["rubrics_used"])
    persisted={(item["agent_name"],item["metric_name"]):item["metric_value"] for item in result["metrics"] if item["metric_type"]=="llm_judge" and item["contributes_to_score"]}
    for agent,dimensions in result["dimension_scores"].items():
        for name,value in dimensions.items():assert value==persisted[(agent,name)]
    serialized=json.dumps(calls,default=str).lower()
    assert "api_key" not in serialized and "authorization" not in serialized and "main.py" not in serialized


@pytest.mark.asyncio
async def test_dimension_scores_reload_is_durable_idempotent_and_provider_free(evaluations):
    _service,store,database=evaluations
    calls=[]
    class Responses:
        async def parse(self,**kwargs):
            calls.append(kwargs)
            payload=json.loads(kwargs["input"])
            return SimpleNamespace(output_parsed={
                "agent_name":payload["agent"],"score":.8,"verdict":"good","confidence":.9,
                "reason":"Durable judge output","dimensions":[{"name":name,"score":.8,"reason":"Durable dimension","confidence":.9} for name in payload["dimensions"]],
            })
    service=AgentEvaluationService(store,openai_client=SimpleNamespace(responses=Responses()),default_model="judge-model")
    created=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="llm_judge",force=True))
    call_count=len(calls)
    counts_before=(
        (await store.fetch_one("SELECT COUNT(*) AS total FROM agent_evaluation_metrics WHERE evaluation_run_id=?",(created["evaluation_run_id"],)))["total"],
        (await store.fetch_one("SELECT COUNT(*) AS total FROM agent_evaluation_evidence WHERE evaluation_run_id=?",(created["evaluation_run_id"],)))["total"],
    )
    restarted=AgentEvaluationStore(database);await restarted.initialize()
    first=await restarted.detail(created["evaluation_run_id"]);second=await restarted.detail(created["evaluation_run_id"])
    read_service=AgentEvaluationService(restarted,openai_client=SimpleNamespace(responses=Responses()),default_model="judge-model")
    @asynccontextmanager
    async def factory():yield SimpleNamespace(agent_evaluations=read_service)
    with TestClient(create_app(factory)) as client:
        api_detail=client.get(f"/api/evaluations/runs/{created['evaluation_run_id']}")
    counts_after=(
        (await restarted.fetch_one("SELECT COUNT(*) AS total FROM agent_evaluation_metrics WHERE evaluation_run_id=?",(created["evaluation_run_id"],)))["total"],
        (await restarted.fetch_one("SELECT COUNT(*) AS total FROM agent_evaluation_evidence WHERE evaluation_run_id=?",(created["evaluation_run_id"],)))["total"],
    )
    assert api_detail.status_code==200
    assert api_detail.json()["dimension_scores"]==first["dimension_scores"]==created["dimension_scores"]==second["dimension_scores"]
    assert api_detail.json()["rubrics_used"]==first["rubrics_used"]==created["rubrics_used"]==second["rubrics_used"]
    assert len(calls)==call_count and counts_before==counts_after


@pytest.mark.asyncio
async def test_comprehensive_dimension_scores_are_grouped_by_origin(evaluations):
    _service,store,_=evaluations
    evidence=context()["agent_evidence"]
    evidence["Developer"]["spans"]=[{"span_id":"dev","status":"completed","node":"implementation","duration_ms":20}]
    class Responses:
        async def parse(self,**kwargs):
            payload=json.loads(kwargs["input"])
            return SimpleNamespace(output_parsed={
                "agent_name":payload["agent"],"score":.85,"verdict":"good","confidence":.9,
                "reason":"Comprehensive judge output","dimensions":[{"name":name,"score":.85,"reason":"Comprehensive dimension","confidence":.9} for name in payload["dimensions"]],
            })
    service=AgentEvaluationService(store,openai_client=SimpleNamespace(responses=Responses()),default_model="judge-model")
    async def supplied_context(*_args,**_kwargs):return context(agent_evidence=evidence)
    service._context=supplied_context
    result=await service.evaluate(EvaluationRunCreate(workflow_id="comprehensive-dimensions",evaluation_type="comprehensive",force=True))
    assert list(result["dimension_scores"])==["deterministic","heuristic","llm_judge"]
    assert "Developer" in result["dimension_scores"]["deterministic"]
    assert "Developer" in result["dimension_scores"]["heuristic"]
    assert "Developer" in result["dimension_scores"]["llm_judge"]
    assert "Workflow" in result["dimension_scores"]["llm_judge"]


def test_judge_output_final_schema_is_strict_and_nested():
    schema=strict_json_schema_for_model(JudgeOutput)
    assert set(schema["properties"])==set(schema["required"])=={"agent_name","score","verdict","confidence","reason","dimensions"}
    assert schema["additionalProperties"] is False
    assert schema["properties"]["dimensions"]["type"]=="array"
    dimension=schema["$defs"]["JudgeDimension"]
    assert set(dimension["properties"])==set(dimension["required"])=={"name","score","reason","confidence"}
    assert dimension["additionalProperties"] is False
    assert schema["properties"]["verdict"]["enum"]==["excellent","good","acceptable","needs_improvement","failed"]
    for target in (schema["properties"]["score"],schema["properties"]["confidence"],dimension["properties"]["score"],dimension["properties"]["confidence"]):
        assert target["minimum"]==0 and target["maximum"]==1


@pytest.mark.parametrize("schema,match",[
    ({"type":"object","properties":{"name":{"type":"string"}},"required":["missing"],"additionalProperties":False},"missing properties"),
    ({"type":"object","properties":{"name":{"type":"string"}},"required":["name","missing"],"additionalProperties":False},"unknown properties"),
    ({"type":"object","properties":{},"required":[],"additionalProperties":True},"additionalProperties"),
    ({"type":"array"},"items"),
    ({"anyOf":[{"type":"string"}],"oneOf":[{"type":"null"}]},"ambiguous"),
])
def test_strict_schema_validator_rejects_malformed_contracts(schema,match):
    with pytest.raises(InvalidStrictJsonSchema,match=match):validate_strict_json_schema(schema)


@pytest.mark.asyncio
async def test_invalid_judge_schema_fails_locally_without_provider_call(evaluations,monkeypatch):
    _service,store,database=evaluations
    calls=[]
    class Responses:
        async def parse(self,**kwargs):calls.append(kwargs)
    def invalid_schema(_model):
        validate_strict_json_schema({"type":"object","properties":{},"required":["dimensions"],"additionalProperties":False})
    monkeypatch.setattr("api.services.agent_evaluation_service.strict_json_schema_for_model",invalid_schema)
    service=AgentEvaluationService(store,openai_client=SimpleNamespace(responses=Responses()),default_model="judge-model")
    result=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="llm_judge",force=True))
    reservations=await store.fetch_all("SELECT * FROM llm_budget_reservations")
    assert result["status"]=="failed" and result["error"].startswith("invalid_judge_schema:")
    assert calls==[] and reservations==[]


@pytest.mark.asyncio
async def test_malformed_provider_output_fails_open(evaluations):
    _service,store,_=evaluations
    class Responses:
        async def parse(self,**kwargs):return SimpleNamespace(output_parsed={"score":2})
    service=AgentEvaluationService(store,openai_client=SimpleNamespace(responses=Responses()),default_model="judge-model")
    result=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="llm_judge",force=True))
    registry=await store.fetch_one("SELECT terminal_status FROM workflow_registry WHERE thread_id='workflow-1'")
    assert result["status"]=="failed" and "ValidationError" in result["error"]
    assert registry["terminal_status"]=="completed"


async def configured_judge_finops(database, response_factory):
    observability_store=ObservabilityStore(database);await observability_store.initialize()
    observability=ObservabilityService(observability_store);await observability.initialize()
    cost_store=LLMCostStore(database);costs=LLMCostService(cost_store,observability_store);await costs.initialize()
    await cost_store.create_pricing({
        "provider":"openai","model_pattern":"judge-model","currency":"USD",
        "input_price_per_million":Decimal("1"),"cached_input_price_per_million":Decimal("0.25"),
        "output_price_per_million":Decimal("4"),"reasoning_price_per_million":Decimal("4"),
        "effective_from":datetime(2020,1,1,tzinfo=UTC),"source_type":"test_fixture",
        "enabled":True,"priority":0,"reasoning_in_completion":True,"metadata":{"tests_only":True},
    })
    budget=await cost_store.create_budget({
        "name":"judge budget","scope_type":"global","currency":"USD",
        "limit_amount":Decimal("0.05"),"warning_percent":Decimal("80"),
        "enforcement_mode":"hard_limit","enabled":True,
    })
    class Responses:
        async def parse(self,**kwargs):return response_factory(kwargs)
    client=ObservableOpenAIClient(SimpleNamespace(responses=Responses()),observability,costs)
    return client,observability,costs,budget


@pytest.mark.asyncio
async def test_successful_judge_usage_creates_cost_and_consumes_budget(evaluations):
    _service,store,database=evaluations
    def response(kwargs):
        payload=json.loads(kwargs["input"])
        return SimpleNamespace(
            id="judge-response",usage=SimpleNamespace(input_tokens=100,output_tokens=50,total_tokens=150,input_tokens_details=SimpleNamespace(cached_tokens=0),output_tokens_details=SimpleNamespace(reasoning_tokens=0)),
            output_parsed={"agent_name":payload["agent"],"score":.9,"verdict":"excellent","confidence":.9,"reason":"Evidence is strong","dimensions":[{"name":name,"score":.9,"reason":"Evidence is strong","confidence":.9} for name in payload["dimensions"]]},
        )
    client,observability,costs,budget=await configured_judge_finops(database,response)
    service=AgentEvaluationService(store,observability=observability,llm_costs=costs,openai_client=client,default_model="judge-model")
    result=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="llm_judge",force=True))
    usage=await costs.store.budget_usage(budget["budget_id"])
    calls=await store.fetch_all("SELECT * FROM observability_llm_calls WHERE agent='Evaluator'")
    calculations=await store.fetch_all("SELECT * FROM llm_cost_calculations WHERE agent_name='Evaluator' AND superseded=0")
    assert result["status"]=="completed" and result["metrics"] and result["evidence"]
    assert calls and all(item["usage_source"]=="provider_reported" for item in calls)
    assert calculations and all(item["cost_status"]=="calculated" and Decimal(item["total_cost"])>0 for item in calculations)
    assert Decimal(usage["consumed"])>0 and Decimal(usage["reserved"])==0


@pytest.mark.asyncio
async def test_failed_judge_releases_reservation_without_cost_or_usage(evaluations):
    _service,store,database=evaluations
    def response(_kwargs):raise RuntimeError("provider schema failure")
    client,observability,costs,budget=await configured_judge_finops(database,response)
    service=AgentEvaluationService(store,observability=observability,llm_costs=costs,openai_client=client,default_model="judge-model")
    result=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="llm_judge",force=True))
    usage=await costs.store.budget_usage(budget["budget_id"])
    call=await store.fetch_one("SELECT * FROM observability_llm_calls WHERE agent='Evaluator'")
    calculation=await store.fetch_one("SELECT * FROM llm_cost_calculations WHERE agent_name='Evaluator' AND superseded=0")
    assert result["status"]=="failed" and call["usage_available"]==0
    assert calculation["cost_status"]=="unavailable" and calculation["total_cost"] is None
    assert Decimal(usage["consumed"])==0 and Decimal(usage["reserved"])==0


@pytest.mark.asyncio
async def test_failed_judge_is_fail_open_for_evaluation_and_workflow(evaluations):
    _service,store,_=evaluations
    class Responses:
        async def parse(self,**kwargs):raise RuntimeError("judge unavailable")
    service=AgentEvaluationService(store,openai_client=SimpleNamespace(responses=Responses()),default_model="judge")
    result=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="llm_judge",force=True))
    registry=await store.fetch_one("SELECT terminal_status FROM workflow_registry WHERE thread_id='workflow-1'")
    assert result["status"]=="failed" and "judge unavailable" in result["error"]
    assert registry["terminal_status"]=="completed"


@pytest.mark.asyncio
async def test_baseline_comparison_detects_regression(evaluations):
    service,store,_=evaluations
    run=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="deterministic"))
    baseline=await store.create_baseline({"scope":{"workflow_type":"create"},"metric":"workflow_score","score":1.0,"sample_count":10})
    compared=await service.compare(run["evaluation_run_id"],baseline["baseline_id"],.05)
    assert compared["baselines"][0]["sample_count"]==10
    assert (bool(compared["regressions"]) is (run["overall_score"]<.95))


@pytest.mark.asyncio
async def test_comparable_metric_resolver_supports_run_persisted_and_unavailable(evaluations):
    service,_,_=evaluations
    run=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="heuristic",force=True))
    overall=await service.resolve_current_metric(run["evaluation_run_id"],"overall_score")
    duration=await service.resolve_current_metric(run["evaluation_run_id"],"duration_ms")
    persisted=await service.resolve_current_metric(run["evaluation_run_id"],"reliability_score")
    unavailable=await service.resolve_current_metric(run["evaluation_run_id"],"does_not_exist")
    assert overall=={"metric":"overall_score","status":"available","reason":None,"direction":"higher_is_better","value":run["overall_score"],"source":"agent_evaluation_runs.overall_score"}
    assert duration["value"]==run["duration_ms"] and duration["direction"]=="lower_is_better"
    expected=next(item["metric_value"] for item in run["metrics"] if item["metric_name"]=="reliability_score")
    assert persisted["value"]==expected and persisted["source"]=="agent_evaluation_metrics"
    assert unavailable["status"]=="unavailable" and unavailable["reason"]=="metric_not_available"


@pytest.mark.asyncio
@pytest.mark.parametrize(("degradation","expected"),[(0,False),(.049999,False),(.05,True),(.050001,True)])
async def test_higher_is_better_absolute_threshold_boundaries(evaluations,degradation,expected):
    service,store,_=evaluations
    run=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="deterministic",force=True))
    baseline=await store.create_baseline({"scope":{"case":str(degradation)},"metric":"overall_score","score":run["overall_score"]+degradation,"sample_count":1})
    result=await service.compare(run["evaluation_run_id"],baseline["baseline_id"],.05)
    comparison=result["comparisons"][0]
    assert comparison["direction"]=="higher_is_better" and comparison["regression"] is expected
    assert comparison["delta"]==pytest.approx(degradation)
    assert bool(result["regressions"]) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(("degradation","expected"),[(0,False),(.049999,False),(.05,True),(.050001,True)])
async def test_lower_is_better_absolute_threshold_boundaries(evaluations,degradation,expected):
    service,store,_=evaluations
    run=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="deterministic",force=True))
    baseline=await store.create_baseline({"scope":{"case":str(degradation)},"metric":"duration_ms","score":run["duration_ms"]-degradation,"sample_count":1})
    result=await service.compare(run["evaluation_run_id"],baseline["baseline_id"],.05)
    comparison=result["comparisons"][0]
    assert comparison["direction"]=="lower_is_better" and comparison["regression"] is expected
    assert comparison["delta"]==pytest.approx(degradation)


@pytest.mark.asyncio
async def test_compare_reports_unavailable_metric_explicitly(evaluations):
    service,store,_=evaluations
    run=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="deterministic",force=True))
    baseline=await store.create_baseline({"scope":{"case":"unavailable"},"metric":"unknown_metric","score":1,"sample_count":1})
    result=await service.compare(run["evaluation_run_id"],baseline["baseline_id"],.05)
    assert result["regressions"]==[]
    assert result["comparisons"][0]["status"]=="unavailable"
    assert result["comparisons"][0]["reason"]=="metric_not_available"


@pytest.mark.asyncio
async def test_compare_multiple_directions_is_restart_safe_and_idempotent(evaluations):
    service,store,database=evaluations
    run=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="heuristic",force=True))
    await store.create_baseline({"scope":{"kind":"score"},"metric":"overall_score","score":run["overall_score"]+0.1,"sample_count":1})
    await store.create_baseline({"scope":{"kind":"duration"},"metric":"duration_ms","score":run["duration_ms"]-0.1,"sample_count":1})
    metrics_before=(await store.fetch_one("SELECT COUNT(*) AS total FROM agent_evaluation_metrics WHERE evaluation_run_id=?",(run["evaluation_run_id"],)))["total"]
    first=await service.compare(run["evaluation_run_id"],None,.05)
    evidence_after_first=(await store.fetch_one("SELECT COUNT(*) AS total FROM agent_evaluation_evidence WHERE evaluation_run_id=? AND evidence_type='regression'",(run["evaluation_run_id"],)))["total"]
    restarted_store=AgentEvaluationStore(database);await restarted_store.initialize()
    second=await AgentEvaluationService(restarted_store).compare(run["evaluation_run_id"],None,.05)
    metrics_after=(await restarted_store.fetch_one("SELECT COUNT(*) AS total FROM agent_evaluation_metrics WHERE evaluation_run_id=?",(run["evaluation_run_id"],)))["total"]
    evidence_after_second=(await restarted_store.fetch_one("SELECT COUNT(*) AS total FROM agent_evaluation_evidence WHERE evaluation_run_id=? AND evidence_type='regression'",(run["evaluation_run_id"],)))["total"]
    assert {item["direction"] for item in first["comparisons"]}=={"higher_is_better","lower_is_better"}
    assert len(first["regressions"])==2 and first["comparisons"]==second["comparisons"]
    assert metrics_before==metrics_after and evidence_after_first==evidence_after_second==2


def test_metric_direction_configuration_is_explicit():
    assert metric_direction("overall_score")==metric_direction("reliability_score")=="higher_is_better"
    assert metric_direction("duration_ms")==metric_direction("evaluation_cost")==metric_direction("total_tokens")=="lower_is_better"


@pytest.mark.asyncio
async def test_agent_evaluation_api_contract(evaluations):
    service,_,_=evaluations
    @asynccontextmanager
    async def factory():yield SimpleNamespace(agent_evaluations=service)
    with TestClient(create_app(factory)) as client:
        created=client.post("/api/evaluations/runs",json={"workflow_id":"workflow-1","evaluation_type":"deterministic","force":True})
        listed=client.get("/api/evaluations/runs?workflow=workflow-1&min_score=0")
        detail=client.get(f"/api/evaluations/runs/{created.json()['evaluation_run_id']}")
        agents=client.get("/api/evaluations/agents");metrics=client.get("/api/evaluations/metrics")
        rubrics=client.get("/api/evaluations/rubrics");dashboard=client.get("/api/evaluations/dashboard")
        regressions=client.get("/api/evaluations/regressions")
    assert all(response.status_code==200 for response in (created,listed,detail,agents,metrics,rubrics,dashboard,regressions))
    assert listed.json()["total"]>=1 and rubrics.json()["items"]
    assert "workflows_evaluated" in dashboard.json()


@pytest.mark.asyncio
async def test_rubric_detail_versions_preserve_historical_evaluation(evaluations):
    service,store,_=evaluations
    planner=next(item for item in await store.rubrics() if item["agent_name"]=="Planner")
    run=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="deterministic",rubric_id=planner["rubric_id"],force=True))
    weights={**planner["weights"],"completeness":.29,"technical_consistency":.21}
    created,is_new,error=await store.create_rubric_version(planner["rubric_id"],{"version":"1.1","dimensions":planner["dimensions"],"weights":weights,"thresholds":planner["thresholds"],"enabled":True})
    detail=await store.rubric_detail(created["rubric_id"]);old=await store.rubric_detail(planner["rubric_id"]);persisted=await store.detail(run["evaluation_run_id"])
    assert is_new and error is None and created["version_status"]=="current"
    assert old["version"]=="1.0" and old["version_status"]=="superseded" and old["weights"]==planner["weights"]
    assert persisted["rubric_id"]==planner["rubric_id"] and persisted["rubric_version"]=="1.0"
    assert {item["version"] for item in detail["history"]}=={"1.0","1.1"}
    assert next(item for item in detail["history"] if item["version"]=="1.0")["usage_count"]==1


@pytest.mark.asyncio
async def test_rubric_version_creation_is_concurrent_and_idempotent(evaluations):
    _,store,_=evaluations
    planner=next(item for item in await store.rubrics() if item["agent_name"]=="Planner")
    payload={"version":"1.1","dimensions":planner["dimensions"],"weights":planner["weights"],"thresholds":planner["thresholds"],"enabled":True}
    first,second=await asyncio.gather(store.create_rubric_version(planner["rubric_id"],payload),store.create_rubric_version(planner["rubric_id"],payload))
    rows=await store.fetch_all("SELECT * FROM agent_evaluation_rubrics WHERE name=? AND agent_name=? AND version='1.1'",(planner["name"],planner["agent_name"]))
    assert len(rows)==1 and {first[0]["rubric_id"],second[0]["rubric_id"]}=={rows[0]["rubric_id"]}
    assert sorted((first[1],second[1]))==[False,True]


@pytest.mark.asyncio
async def test_rubric_api_validates_weights_versions_and_disable(evaluations):
    service,store,_=evaluations
    planner=next(item for item in await store.rubrics() if item["agent_name"]=="Planner")
    @asynccontextmanager
    async def factory():yield SimpleNamespace(agent_evaluations=service)
    payload={"version":"1.1","dimensions":planner["dimensions"],"weights":planner["weights"],"thresholds":planner["thresholds"],"enabled":True}
    invalid={**payload,"weights":{**planner["weights"],"completeness":.4}}
    with TestClient(create_app(factory)) as client:
        detail=client.get(f"/api/evaluations/rubrics/{planner['rubric_id']}")
        rejected=client.post(f"/api/evaluations/rubrics/{planner['rubric_id']}/versions",json=invalid)
        created=client.post(f"/api/evaluations/rubrics/{planner['rubric_id']}/versions",json=payload)
        history=client.get(f"/api/evaluations/rubrics/{created.json()['rubric_id']}/versions")
        disabled=client.post(f"/api/evaluations/rubrics/{created.json()['rubric_id']}/disable")
        old=client.get(f"/api/evaluations/rubrics/{planner['rubric_id']}")
    assert detail.status_code==200 and detail.json()["verdict_thresholds"]["excellent"]==.9
    assert rejected.status_code==422 and created.status_code==201
    assert [item["version"] for item in history.json()["items"]]==["1.1","1.0"]
    assert disabled.json()["version_status"]=="disabled" and disabled.json()["enabled"] is False
    assert old.status_code==200 and old.json()["version"]=="1.0"


@pytest.mark.asyncio
async def test_used_current_rubric_patch_versions_and_historical_api_mutation_is_rejected(evaluations):
    service,store,_=evaluations
    planner=next(item for item in await store.rubrics() if item["agent_name"]=="Planner")
    run=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="deterministic",rubric_id=planner["rubric_id"],force=True))
    score_before=run["overall_score"]
    weights={**planner["weights"],"completeness":.29,"technical_consistency":.21}
    @asynccontextmanager
    async def factory():yield SimpleNamespace(agent_evaluations=service)
    with TestClient(create_app(factory)) as client:
        versioned=client.patch(f"/api/evaluations/rubrics/{planner['rubric_id']}",json={"version":"1.1","weights":weights})
        historical_patch=client.patch(f"/api/evaluations/rubrics/{planner['rubric_id']}",json={"weights":planner["weights"]})
        historical_disable=client.post(f"/api/evaluations/rubrics/{planner['rubric_id']}/disable")
        identity_mutation=client.patch(f"/api/evaluations/rubrics/{planner['rubric_id']}",json={"agent_name":"Other","version":"9.0"})
        metadata_only=client.patch(f"/api/evaluations/rubrics/{versioned.json()['rubric_id']}",json={"enabled":True})
    persisted=await store.detail(run["evaluation_run_id"])
    planner_binding=next(item for item in persisted["rubrics_used"] if item["agent_name"]=="Planner")
    assert versioned.status_code==200 and versioned.json()["version"]=="1.1" and versioned.json()["rubric_id"]!=planner["rubric_id"]
    assert historical_patch.status_code==409 and historical_disable.status_code==409
    assert identity_mutation.status_code==422 and metadata_only.status_code==422
    assert planner_binding["rubric_id"]==planner["rubric_id"] and planner_binding["rubric_version"]=="1.0"
    assert persisted["overall_score"]==score_before


@pytest.mark.asyncio
async def test_runs_store_exact_multiple_rubric_bindings_and_versions_remain_immutable(evaluations):
    service,store,_=evaluations
    planner_v1=next(item for item in await store.rubrics() if item["agent_name"]=="Planner")
    historical=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="comprehensive",force=True))
    historical_bindings={item["agent_name"]:item for item in historical["rubrics_used"]}
    assert set(historical_bindings)=={"Planner","Developer","QA","Workflow"}
    assert historical_bindings["Planner"]["rubric_id"]==planner_v1["rubric_id"]
    assert "Repair" not in historical_bindings
    planner_v11,_,_=await store.create_rubric_version(planner_v1["rubric_id"],{"version":"1.1","dimensions":planner_v1["dimensions"],"weights":planner_v1["weights"],"thresholds":planner_v1["thresholds"],"enabled":True})
    current=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="deterministic",force=True))
    current_planner=next(item for item in current["rubrics_used"] if item["agent_name"]=="Planner")
    assert current_planner["rubric_id"]==planner_v11["rubric_id"] and current_planner["rubric_version"]=="1.1"
    await store.create_rubric_version(planner_v11["rubric_id"],{"version":"1.2","dimensions":planner_v11["dimensions"],"weights":planner_v11["weights"],"thresholds":planner_v11["thresholds"],"enabled":True})
    restarted=AgentEvaluationStore(store.database_path);await restarted.initialize()
    persisted=await restarted.detail(historical["evaluation_run_id"])
    assert next(item for item in persisted["rubrics_used"] if item["agent_name"]=="Planner")["rubric_id"]==planner_v1["rubric_id"]
    with pytest.raises(sqlite3.IntegrityError,match="rubric_version_is_immutable"):
        with restarted.connect() as connection:connection.execute("UPDATE agent_evaluation_rubrics SET weights_json='{}' WHERE rubric_id=?",(planner_v1["rubric_id"],))
    with pytest.raises(sqlite3.IntegrityError,match="rubric_version_is_immutable"):
        with restarted.connect() as connection:connection.execute("UPDATE agent_evaluation_rubrics SET agent_name='Other',version='9.0' WHERE rubric_id=?",(planner_v1["rubric_id"],))


@pytest.mark.asyncio
async def test_rubric_history_counts_valid_and_total_without_losing_invalidated(evaluations):
    service,store,_=evaluations
    first=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="deterministic",force=True))
    second=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="heuristic",force=True))
    await store.set_validity(first["evaluation_run_id"],validity_status="invalidated",superseded_by_run_id=None,reason="audit preservation test")
    planner=next(item for item in await store.rubrics() if item["agent_name"]=="Planner")
    detail=await store.rubric_detail(planner["rubric_id"])
    assert detail["evaluations_total"]==2 and detail["evaluations_valid"]==1 and detail["usage_count"]==2
    assert await store.detail(first["evaluation_run_id"])
    assert await store.detail(second["evaluation_run_id"])


@pytest.mark.asyncio
async def test_backfill_uses_historical_effective_time_is_idempotent_and_skips_repair(evaluations):
    _,store,database=evaluations
    with store.connect() as connection:
        connection.execute("UPDATE agent_evaluation_rubrics SET created_at='2026-01-01T00:00:00+00:00' WHERE version='1.0'")
    planner_v1=next(item for item in await store.rubrics() if item["agent_name"]=="Planner")
    planner_v11,_,_=await store.create_rubric_version(planner_v1["rubric_id"],{"version":"1.1","dimensions":planner_v1["dimensions"],"weights":planner_v1["weights"],"thresholds":planner_v1["thresholds"],"enabled":True})
    with store.connect() as connection:connection.execute("UPDATE agent_evaluation_rubrics SET created_at='2026-02-01T00:00:00+00:00' WHERE rubric_id=?",(planner_v11["rubric_id"],))
    run,_=await store.create_run({"workflow_id":"historical","trace_id":None,"branch_id":"original","execution_id":None,"evaluation_type":"comprehensive","evaluator_version":"6.17.0","rubric_id":None,"rubric_version":"1.0","model":None,"idempotency_key":"historical-backfill"},force=True)
    with store.connect() as connection:connection.execute("UPDATE agent_evaluation_runs SET started_at='2026-01-15T00:00:00+00:00',created_at='2026-01-15T00:00:00+00:00' WHERE evaluation_run_id=?",(run["evaluation_run_id"],))
    await store.add_result(run["evaluation_run_id"],{"agent_name":"Planner","evaluator_name":"comprehensive","score":.9,"verdict":"excellent","confidence":1,"reason":"historical"})
    await store.add_result(run["evaluation_run_id"],{"agent_name":"Repair","evaluator_name":"comprehensive","score":None,"verdict":"not_evaluated","confidence":0,"reason":"no repair"})
    await store.add_result(run["evaluation_run_id"],{"agent_name":"Workflow","evaluator_name":"aggregate","score":.9,"verdict":"excellent","confidence":1,"reason":"historical"})
    preview=await store.reconcile_run_rubrics(dry_run=True,run_ids=[run["evaluation_run_id"]])
    first=await store.reconcile_run_rubrics(dry_run=False,run_ids=[run["evaluation_run_id"]]);second=await store.reconcile_run_rubrics(dry_run=False,run_ids=[run["evaluation_run_id"]])
    detail=await AgentEvaluationStore(database).detail(run["evaluation_run_id"])
    assert preview["bindings_candidates"]==2 and first["bindings_created"]==2 and second["bindings_created"]==0
    assert detail["rubric_binding_status"]=="reconciled"
    assert {item["agent_name"] for item in detail["rubrics_used"]}=={"Planner","Workflow"}
    assert next(item for item in detail["rubrics_used"] if item["agent_name"]=="Planner")["rubric_id"]==planner_v1["rubric_id"]


@pytest.mark.asyncio
async def test_backfill_does_not_fabricate_ambiguous_binding_and_concurrent_binding_is_unique(evaluations):
    _,store,_=evaluations
    planner=next(item for item in await store.rubrics() if item["agent_name"]=="Planner")
    alternate=await store.create_rubric({"name":"Planner alternate","version":"1.0","agent_name":"Planner","dimensions":planner["dimensions"],"weights":planner["weights"],"thresholds":planner["thresholds"],"enabled":False})
    with store.connect() as connection:connection.execute("UPDATE agent_evaluation_rubrics SET created_at='2020-01-01T00:00:00+00:00' WHERE rubric_id IN (?,?)",(planner["rubric_id"],alternate["rubric_id"]))
    run,_=await store.create_run({"workflow_id":"ambiguous","trace_id":None,"branch_id":"original","execution_id":None,"evaluation_type":"deterministic","evaluator_version":"6.17.0","rubric_id":None,"rubric_version":"1.0","model":None,"idempotency_key":"ambiguous-backfill"},force=True)
    await store.add_result(run["evaluation_run_id"],{"agent_name":"Planner","evaluator_name":"deterministic","score":1,"verdict":"excellent","confidence":1,"reason":"ambiguous"})
    report=await store.reconcile_run_rubrics(dry_run=False,run_ids=[run["evaluation_run_id"]]);detail=await store.detail(run["evaluation_run_id"])
    assert report["unreconciled_runs"]==1 and detail["rubrics_used"]==[]
    assert detail["rubric_binding_status"]=="unreconciled_rubric_binding" and "ambiguous_effective_rubric" in detail["rubric_binding_reason"]
    await asyncio.gather(*(store.bind_run_rubric(run["evaluation_run_id"],planner,"deterministic") for _ in range(4)))
    count=await store.fetch_one("SELECT COUNT(*) AS total FROM agent_evaluation_run_rubrics WHERE evaluation_run_id=? AND rubric_id=?",(run["evaluation_run_id"],planner["rubric_id"]))
    assert count["total"]==1


@pytest.mark.asyncio
async def test_concurrent_evaluation_persists_one_durable_binding_set(evaluations):
    service,store,_=evaluations
    first,second=await asyncio.gather(
        service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="deterministic")),
        service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="deterministic")),
    )
    assert first["evaluation_run_id"]==second["evaluation_run_id"]
    detail=await store.detail(first["evaluation_run_id"])
    agents=[item["agent_name"] for item in detail["rubrics_used"]]
    assert agents==sorted(set(agents)) and set(agents)=={"Planner","Developer","QA","Workflow"}


@pytest.mark.asyncio
async def test_validity_filters_analytics_but_preserves_audit_and_not_evaluated(evaluations):
    service,store,database=evaluations
    valid=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="deterministic",force=True))
    invalid=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="deterministic",force=True))
    superseded=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="deterministic",force=True))
    replacement=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="deterministic",force=True))
    await store.set_validity(invalid["evaluation_run_id"],validity_status="invalidated",superseded_by_run_id=None,reason="known invalid attribution")
    await store.set_validity(superseded["evaluation_run_id"],validity_status="superseded",superseded_by_run_id=replacement["evaluation_run_id"],reason="explicit replacement")
    before={table:(await store.fetch_one(f"SELECT COUNT(*) AS total FROM {table} WHERE evaluation_run_id=?",(invalid["evaluation_run_id"],)))["total"] for table in ("agent_evaluation_results","agent_evaluation_metrics","agent_evaluation_evidence")}
    @asynccontextmanager
    async def factory():yield SimpleNamespace(agent_evaluations=service)
    with TestClient(create_app(factory)) as client:
        runs=client.get("/api/evaluations/runs").json()
        invalid_detail=client.get(f"/api/evaluations/runs/{invalid['evaluation_run_id']}").json()
        agents=client.get("/api/evaluations/agents").json()["items"]
        agents_all=client.get("/api/evaluations/agents?include_invalidated=true").json()["items"]
        metrics=client.get("/api/evaluations/metrics").json()["items"]
        metrics_all=client.get("/api/evaluations/metrics?include_invalidated=true").json()["items"]
        dashboard=client.get("/api/evaluations/dashboard").json()
        dashboard_all=client.get("/api/evaluations/dashboard?include_invalidated=true").json()
    planner=next(item for item in agents if item["agent"]=="Planner")
    planner_all=next(item for item in agents_all if item["agent"]=="Planner")
    repair=next(item for item in agents if item["agent"]=="Repair")
    assert runs["total"]==4 and invalid_detail["validity_status"]=="invalidated"
    assert invalid_detail["results"] and invalid_detail["metrics"] and invalid_detail["evidence"]
    assert planner["evaluations_attempted"]==planner["evaluations_scored"]==2
    assert planner_all["evaluations_attempted"]==planner_all["evaluations_scored"]==4
    assert repair["evaluations_attempted"]==2 and repair["evaluations_scored"]==0 and repair["not_evaluated_count"]==2
    assert repair["average_score"] is repair["min_score"] is repair["max_score"] is None
    assert sum(item["samples"] for item in metrics)<sum(item["samples"] for item in metrics_all)
    assert len(dashboard["score_over_time"])==2 and len(dashboard_all["score_over_time"])==4
    restarted=AgentEvaluationStore(database);await restarted.initialize()
    persisted=await restarted.detail(superseded["evaluation_run_id"])
    after={table:(await restarted.fetch_one(f"SELECT COUNT(*) AS total FROM {table} WHERE evaluation_run_id=?",(invalid["evaluation_run_id"],)))["total"] for table in before}
    assert persisted["validity_status"]=="superseded" and persisted["superseded_by_run_id"]==replacement["evaluation_run_id"]
    assert before==after


@pytest.mark.asyncio
async def test_validity_admin_action_and_invalid_baseline_auto_selection(evaluations):
    service,store,_=evaluations
    source=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="deterministic",force=True))
    target=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="deterministic",force=True))
    baseline=await store.create_baseline({"scope":{"validity":"test"},"metric":"overall_score","score":1,"sample_count":1})
    @asynccontextmanager
    async def factory():yield SimpleNamespace(agent_evaluations=service)
    with TestClient(create_app(factory)) as client:
        response=client.patch(f"/api/evaluations/runs/{source['evaluation_run_id']}/validity",json={"validity_status":"superseded","superseded_by_run_id":target["evaluation_run_id"],"reason":"corrected evaluator"})
    assert response.status_code==200 and response.json()["superseded_by_run_id"]==target["evaluation_run_id"]
    automatic=await service.compare(source["evaluation_run_id"],None,.05)
    explicit=await service.compare(source["evaluation_run_id"],baseline["baseline_id"],.05)
    assert automatic["baselines"]==[] and automatic["comparisons"]==[]
    assert explicit["comparisons"]


@pytest.mark.asyncio
async def test_known_invalid_run_migration_is_durable(evaluations):
    _service,store,database=evaluations
    run,_=await store.create_run({"workflow_id":"legacy","trace_id":None,"branch_id":"original","execution_id":None,"evaluation_type":"deterministic","evaluator_version":"6.17.0","rubric_id":None,"rubric_version":"1.0","model":None,"idempotency_key":"known-invalid"},force=True)
    with store.connect() as connection:
        connection.execute("UPDATE agent_evaluation_runs SET evaluation_run_id=? WHERE evaluation_run_id=?",("e1c9254e7c304063b3e4326b3c3d2269",run["evaluation_run_id"]))
    restarted=AgentEvaluationStore(database);await restarted.initialize()
    migrated=await restarted.detail("e1c9254e7c304063b3e4326b3c3d2269")
    assert migrated["validity_status"]=="invalidated"
    assert migrated["invalidation_reason"]=="Agent scores were derived from workflow-global deterministic metrics before agent-scope attribution fix"


@pytest.mark.asyncio
async def test_metrics_analytics_exposes_raw_diagnostics_and_excludes_legacy(evaluations):
    service,store,_=evaluations
    run=await service.evaluate(EvaluationRunCreate(workflow_id="workflow-1",evaluation_type="comprehensive",force=True))
    await store.add_metric(run["evaluation_run_id"],{"evaluation_scope":"workflow","metric_name":"workflow_duration","metric_value":.5,"metric_type":"heuristic","unit":"score","threshold":.7,"passed":False,"raw_value":1234,"source_state":"legacy","contributes_to_score":True})
    @asynccontextmanager
    async def factory():yield SimpleNamespace(agent_evaluations=service)
    with TestClient(create_app(factory)) as client:
        current=client.get("/api/evaluations/metrics").json()["items"]
        with_legacy=client.get("/api/evaluations/metrics?include_legacy=true").json()["items"]
    by_name={item["metric_name"]:item for item in current}
    assert "workflow_duration" not in by_name
    assert next(item for item in with_legacy if item["metric_name"]=="workflow_duration")["analytics_role"]=="legacy"
    for name in ("handled_error_count","recovered_error_count","unhandled_error_count"):
        assert by_name[name]["analytics_role"]=="diagnostic"
        assert by_name[name]["average_value"] is None and by_name[name]["passed_count"] is None
        assert by_name[name]["average_raw_value"] is not None and by_name[name]["raw_unit"]=="count"
    active=by_name["agent_active_duration_total"];parallel=by_name["parallelism_factor"]
    assert active["analytics_role"]==parallel["analytics_role"]=="diagnostic"
    assert active["average_value"] is None and active["raw_unit"]=="ms"
    assert parallel["average_value"] is None and parallel["raw_unit"]=="x"
    assert by_name["workflow_wall_clock_duration"]["analytics_role"]=="score"
