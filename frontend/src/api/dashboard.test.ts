import { describe, expect, it, vi } from "vitest";
import { normalizeDashboardTimeSeries, type DashboardTimeSeries } from "./dashboard";

const response = (points: DashboardTimeSeries["points"], timezone = "UTC"): DashboardTimeSeries => ({
  metric: "workflow_count",
  interval: "day",
  timezone,
  points,
  source_updated_at: null,
});

describe("dashboard timeseries contract", () => {
  it.each(["hour", "day", "week", "month"])("keeps bucket_start and bucket_end for %s", (interval) => {
    const series = normalizeDashboardTimeSeries({
      ...response([{ bucket_start: "2026-08-01T00:00:00Z", bucket_end: "2026-08-02T00:00:00Z", value: 0, count: 0, numerator: null, denominator: null }]),
      interval,
    });
    expect(series.points[0]).toMatchObject({
      bucket_start: "2026-08-01T00:00:00Z",
      bucket_end: "2026-08-02T00:00:00Z",
      value: 0,
    });
  });

  it.each(["America/Lima", "UTC"])("accepts ISO offsets for %s", (timezone) => {
    const offset = timezone === "UTC" ? "+00:00" : "-05:00";
    const series = normalizeDashboardTimeSeries(response([{ bucket_start: `2026-08-01T00:00:00${offset}`, bucket_end: `2026-08-02T00:00:00${offset}`, value: null, count: 0, numerator: null, denominator: null }], timezone));
    expect(series.points).toHaveLength(1);
  });

  it("discards an invalid point without creating an empty label", () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const valid = { bucket_start: "2026-08-01T00:00:00Z", bucket_end: "2026-08-02T00:00:00Z", value: 1, count: 1, numerator: null, denominator: null };
    const invalid = { ...valid, bucket_start: "" };
    expect(normalizeDashboardTimeSeries(response([invalid, valid])).points).toEqual([valid]);
    expect(error).toHaveBeenCalledOnce();
    error.mockRestore();
  });

  it("raises a controlled error when every point is invalid", () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const invalid = { bucket_start: "", bucket_end: "", value: 0, count: 0, numerator: null, denominator: null };
    expect(() => normalizeDashboardTimeSeries(response([invalid]))).toThrow("fechas incompletas");
    error.mockRestore();
  });
});
