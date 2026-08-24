from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from api.llm_cost_models import UsageSource


@dataclass(frozen=True, slots=True)
class NormalizedLLMUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    uncached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    audio_input_tokens: int | None = None
    audio_output_tokens: int | None = None
    image_input_units: int | None = None
    usage_source: UsageSource = UsageSource.UNAVAILABLE
    usage_available: bool = False
    usage_invalid: bool = False
    validation_error: str | None = None
    provider_request_id: str | None = None
    raw_usage_version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        result = asdict(self)
        result["usage_source"] = self.usage_source.value
        return result


def _integer(value: Any) -> int | None:
    if value is None:
        return None
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def normalize_llm_usage(
    source: Any,
    *,
    usage_source: UsageSource | None = None,
    provider_request_id: str | None = None,
    provider: str | None = None,
    total_includes_input_output: bool | None = None,
) -> NormalizedLLMUsage:
    missing = object()
    response_usage = getattr(source, "usage", missing)
    provider_usage_evidence = response_usage is not missing and response_usage is not None
    usage = response_usage if response_usage is not missing else source
    if usage is None:
        return NormalizedLLMUsage(provider_request_id=provider_request_id)
    getter = usage.get if isinstance(usage, dict) else lambda key, default=None: getattr(usage, key, default)
    prompt = _integer(getter("input_tokens", getter("prompt_tokens")))
    completion = _integer(getter("output_tokens", getter("completion_tokens")))
    total = _integer(getter("total_tokens"))
    input_details = getter("input_tokens_details") or {}
    output_details = getter("output_tokens_details") or {}
    detail_get = input_details.get if isinstance(input_details, dict) else lambda key, default=None: getattr(input_details, key, default)
    output_get = output_details.get if isinstance(output_details, dict) else lambda key, default=None: getattr(output_details, key, default)
    cached = _integer(getter("cached_tokens", detail_get("cached_tokens")))
    reasoning = _integer(getter("reasoning_tokens", output_get("reasoning_tokens")))
    audio_input = _integer(getter("audio_input_tokens", detail_get("audio_tokens")))
    audio_output = _integer(getter("audio_output_tokens", output_get("audio_tokens")))
    values = [prompt, completion, total, cached, reasoning, audio_input, audio_output]
    error = next(("tokens_must_be_non_negative" for value in values if value is not None and value < 0), None)
    if error is None and cached is not None and prompt is not None and cached > prompt:
        error = "cached_tokens_exceed_prompt_tokens"
    expected_total = prompt + completion if prompt is not None and completion is not None else None
    validates_total = total_includes_input_output
    if validates_total is None:
        validates_total = provider_usage_evidence and (provider or "openai").casefold() == "openai"
    if error is None and validates_total and total is not None and expected_total is not None and total != expected_total:
        error = "total_tokens_inconsistent"
    available = any(value is not None for value in (prompt, completion, total))
    selected_source = usage_source or (
        UsageSource.PROVIDER_REPORTED if available and provider_usage_evidence
        else UsageSource.IMPORTED if available
        else UsageSource.UNAVAILABLE
    )
    return NormalizedLLMUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total if total is not None else expected_total,
        cached_input_tokens=cached,
        uncached_input_tokens=max(prompt - (cached or 0), 0) if prompt is not None else None,
        reasoning_tokens=reasoning,
        audio_input_tokens=audio_input,
        audio_output_tokens=audio_output,
        usage_source=selected_source,
        usage_available=available and error is None and selected_source != UsageSource.UNAVAILABLE,
        usage_invalid=error is not None,
        validation_error=error,
        provider_request_id=provider_request_id or getattr(source, "id", None),
        raw_usage_version="openai-responses-v1" if provider_usage_evidence and (provider or "openai").casefold() == "openai" else None,
    )
