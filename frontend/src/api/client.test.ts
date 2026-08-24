import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiClientError, apiFetch, normalizeBaseUrl } from "./client";

describe("api client", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("normalizes the configured base URL", () => {
    expect(normalizeBaseUrl("http://localhost:8000///")).toBe(
      "http://localhost:8000",
    );
  });

  it("parses successful JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    await expect(apiFetch<{ ok: boolean }>("/test")).resolves.toEqual({
      ok: true,
    });
  });

  it("preserves 409 responses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Already resolved" }), {
          status: 409,
        }),
      ),
    );
    await expect(apiFetch("/test")).rejects.toMatchObject({
      statusCode: 409,
      message: "Already resolved",
    });
  });

  it("reports API offline as retryable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    try {
      await apiFetch("/test");
      throw new Error("Expected API request to fail.");
    } catch (error) {
      expect(error).toBeInstanceOf(ApiClientError);
      expect((error as ApiClientError).retryable).toBe(true);
    }
  });
});
