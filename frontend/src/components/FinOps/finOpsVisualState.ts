import type { LlmCall, Pricing } from "../../api/llmCosts";

export type CostVisualState = "real" | "estimated" | "no-pricing" | "no-usage" | "partial" | "invalid";

export function normalizeWarnings(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(String);
  if (typeof value !== "string" || !value.trim()) return [];
  try {
    const parsed: unknown = JSON.parse(value);
    return Array.isArray(parsed) ? parsed.map(String) : [value];
  } catch {
    return [value];
  }
}

export function callVisualState(call: Partial<LlmCall>): CostVisualState {
  const warnings = normalizeWarnings(call.warnings);
  if (call.usage_invalid || call.cost_status === "invalid_pricing" || warnings.some((item) => item.includes("invalid_usage"))) return "invalid";
  if (!call.usage_available || call.usage_source === "unavailable") return "no-usage";
  if (call.cost_source === "estimated" || call.usage_source === "tokenizer_estimated") return "estimated";
  if (warnings.includes("pricing_not_found") || call.cost_source === "unavailable") return "no-pricing";
  return call.cost_source === "calculated" ? "real" : "partial";
}

export function pricingLabels(pricing: Pricing): string[] {
  const now = Date.now();
  const from = Date.parse(pricing.effective_from);
  const to = pricing.effective_to ? Date.parse(pricing.effective_to) : null;
  const source = pricing.source_type.toLowerCase();
  const result = [source === "official" || source === "provider_official" ? "OFFICIAL" : source === "test_fixture" ? "TEST FIXTURE" : source === "manual" ? "MANUAL" : "UNVERIFIED"];
  if (!pricing.enabled) result.push("DISABLED");
  else if (from > now) result.push("FUTURE");
  else if (to !== null && to <= now) result.push("EXPIRED");
  return result;
}
