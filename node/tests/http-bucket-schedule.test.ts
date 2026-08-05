/**
 * HTTP latency bucket schedule is a cross-language contract.
 *
 * Python, Node and Go are kept in sync by convention rather than codegen, and
 * nothing pinned this schedule — which is how the ceiling came to matter
 * without anyone noticing.
 *
 * The schedule used to stop at 10.0, so any request slower than that landed in
 * +Inf and histogram_quantile could never return more than 10. On the fleet
 * that pinned voicestudio's p95 to exactly 10.00 with 23.3% of its requests
 * above the ceiling, and left a 15.0s alert threshold on another service
 * structurally unable to fire.
 *
 * Assertions read the buckets the registry actually emits, never a literal
 * declared in this file — otherwise they would only be testing themselves.
 */

import { describe, it, expect, beforeEach } from "vitest";
import { registry, httpRequestDurationSeconds } from "../src/registry.js";

const EXPECTED = [
  0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 30.0, 60.0,
];

/** Finite `le` bounds actually emitted for a service, in order. */
async function observedBounds(service: string): Promise<number[]> {
  const all = await registry.getMetricsAsJSON();
  const m = all.find(
    (x: { name: string }) => x.name === "simsys_http_request_duration_seconds",
  ) as { values?: Array<{ labels: Record<string, string | number> }> } | undefined;
  if (!m?.values) return [];
  return m.values
    .filter((v) => v.labels.service === service && v.labels.le !== undefined)
    .map((v) => Number(v.labels.le))
    .filter((n) => Number.isFinite(n));
}

describe("HTTP latency bucket schedule", () => {
  beforeEach(() => {
    registry.resetMetrics();
  });

  it("matches the cross-language contract", async () => {
    httpRequestDurationSeconds
      .labels({ service: "bucket_schedule_svc", method: "GET", route: "/x" })
      .observe(0.1);
    expect(await observedBounds("bucket_schedule_svc")).toEqual(EXPECTED);
  });

  it("extends past ten seconds", async () => {
    httpRequestDurationSeconds
      .labels({ service: "ceiling_svc", method: "GET", route: "/x" })
      .observe(0.1);
    const bounds = await observedBounds("ceiling_svc");
    expect(Math.max(...bounds)).toBeGreaterThan(10.0);
    expect(bounds.filter((b) => b > 10.0)).toEqual([15.0, 30.0, 60.0]);
  });

  it("is strictly increasing with no duplicates", async () => {
    httpRequestDurationSeconds
      .labels({ service: "monotonic_svc", method: "GET", route: "/x" })
      .observe(0.1);
    const bounds = await observedBounds("monotonic_svc");
    expect(bounds).toEqual([...bounds].sort((a, b) => a - b));
    expect(new Set(bounds).size).toBe(bounds.length);
  });

  it("resolves a 12s request below the top bucket, not only in +Inf", async () => {
    httpRequestDurationSeconds
      .labels({ service: "slow_tail_svc", method: "GET", route: "/slow" })
      .observe(12.0);

    const all = await registry.getMetricsAsJSON();
    const m = all.find(
      (x: { name: string }) => x.name === "simsys_http_request_duration_seconds",
    ) as { values?: Array<{ labels: Record<string, string | number>; value: number }> };

    const at = (le: string) =>
      m.values?.find(
        (v) => v.labels.service === "slow_tail_svc" && String(v.labels.le) === le,
      )?.value;

    expect(at("10")).toBe(0);
    expect(at("15")).toBe(1);
  });
});
