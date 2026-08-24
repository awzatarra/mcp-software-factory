from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import uuid4

from api.services.observability_context import get_observability_context
from api.services.observability_sanitizer import error_fingerprint
from api.services.llm_usage_normalizer import normalize_llm_usage


class ObservableResponses:
    def __init__(self, target: Any, observability: Any, llm_costs: Any | None = None) -> None:
        self._target = target
        self._observability = observability
        self._llm_costs = llm_costs

    async def _record_call(self, *, call_id, context, child_context, response, failure,
                           started, operation, kwargs, budget_decision) -> None:
        usage = normalize_llm_usage(
            response, provider="openai",
            provider_request_id=getattr(response, "id", None),
            total_includes_input_output=True,
        )
        usage_record = usage.to_record()
        await self._observability._safe(self._observability.store.add_llm_call, {
            "call_id": call_id, "trace_id": context.trace_id, "span_id": child_context.span_id or context.span_id,
            "workflow_id": context.workflow_id, "branch_id": context.branch_id, "agent": context.agent,
            "node": context.node, "subgraph": context.subgraph, "task_id": context.task_id,
            "retry_attempt": 0, "provider": "openai",
            "model": kwargs.get("model"), "operation": operation, "status": "failed" if failure else "completed",
            "duration_ms": (perf_counter() - started) * 1000,
            "input_tokens": usage.prompt_tokens, "output_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens, "cached_tokens": usage.cached_input_tokens,
            "reasoning_tokens": usage.reasoning_tokens, "audio_input_tokens": usage.audio_input_tokens,
            "audio_output_tokens": usage.audio_output_tokens, "usage_source": usage_record["usage_source"],
            "usage_available": usage.usage_available, "usage_invalid": usage.usage_invalid,
            "usage_error": usage.validation_error, "provider_request_id": usage.provider_request_id,
            "raw_usage_version": usage.raw_usage_version,
            "error_fingerprint": error_fingerprint(type(failure).__name__, str(failure), operation) if failure else None,
            "timestamp": datetime.now(UTC).isoformat(), "completed_at": datetime.now(UTC).isoformat(),
            "attributes": {"budget_decision": budget_decision.get("decision"), "budget_reason_codes": budget_decision.get("reason_codes", [])},
        })
        if self._llm_costs is not None:
            await self._llm_costs.record_and_calculate(call_id)

    async def _call(self, operation: str, method, **kwargs):
        recorded_operation = str(kwargs.pop("_observability_operation", operation))
        context = get_observability_context()
        started = perf_counter(); failure = None; response = None; call_id = uuid4().hex
        provider_invoked = False
        budget_decision: dict[str, Any] = {"decision": "unavailable", "reason_codes": []}
        if not context.trace_id:
            return await method(**kwargs)
        try:
            if self._llm_costs is not None:
                budget_decision = await self._llm_costs.preflight(
                    call_id=call_id, workflow_id=context.workflow_id,
                    branch_id=context.branch_id, agent_name=context.agent,
                    provider="openai", model=str(kwargs.get("model") or "unknown"), kwargs=kwargs,
                )
            async with self._observability.span(f"openai.responses.{operation}", category="llm", kind="client", attributes={"provider": "openai", "model": kwargs.get("model")}) as child:
                provider_invoked = True
                response = await method(**kwargs)
            return response
        except Exception as exc:
            failure = exc
            raise
        finally:
            if provider_invoked:
                await self._record_call(
                    call_id=call_id, context=context,
                    child_context=locals().get("child", context), response=response,
                    failure=failure, started=started, operation=recorded_operation,
                    kwargs=kwargs, budget_decision=budget_decision,
                )

    async def create(self, **kwargs): return await self._call("create", self._target.create, **kwargs)
    async def parse(self, **kwargs): return await self._call("parse", self._target.parse, **kwargs)

    def __getattr__(self, name: str): return getattr(self._target, name)


class ObservableOpenAIClient:
    def __init__(self, client: Any, observability: Any, llm_costs: Any | None = None) -> None:
        self._client = client
        self.responses = ObservableResponses(client.responses, observability, llm_costs)

    def __getattr__(self, name: str): return getattr(self._client, name)
