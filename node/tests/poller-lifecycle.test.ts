/**
 * trackQueue / trackPool must release their timer handles.
 *
 * Both used to push every handle into an append-only array on
 * `globalThis.__simsysMetricsBaselineState` and return the raw
 * NodeJS.Timeout. A caller could `clearInterval()` to stop the polling, but
 * nothing could ever remove the handle: the arrays were module-private, never
 * exported, and drained only by `_resetForTests`. Any app re-creating
 * trackers (per tenant, per reconnect, in a factory) leaked one object per
 * creation for the life of the process — Redmine #50318.
 *
 * Python deleted the same structure in 0.3.6 (`_QUEUE_THREADS`, "unbounded
 * leak source"), Go has always returned an idempotent `stop func()`, and this
 * package's own `trackProgress` already returned a tracker whose `stop()`
 * splices itself out. Node was the only laggard, with the correct pattern
 * sitting in a sibling file.
 *
 * The compatibility pin below is the point of the chosen design: `stop()` is
 * ATTACHED to the real Timeout rather than replacing it with a `{stop()}`
 * object, so `clearInterval(handle)` keeps working. Returning a bare object
 * would have made `clearInterval(obj)` a SILENT no-op — no throw, no warning,
 * and the consumer's poller runs forever.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  trackQueue,
  trackPool,
  setService,
  _resetForTests,
  _liveTrackerCounts,
} from "../src/baseline.js";
import { registry } from "../src/registry.js";

describe("poller handle lifecycle", () => {
  beforeEach(() => {
    _resetForTests();
    registry.resetMetrics();
    setService("poller-lifecycle-test");
  });
  afterEach(() => _resetForTests());

  it("returns a handle carrying stop()", () => {
    const h = trackQueue("q", { depthFn: () => 1, intervalMs: 1000 });
    expect(typeof h.stop).toBe("function");
    h.stop();
  });

  it("stop() releases the handle from the live set", () => {
    expect(_liveTrackerCounts().queue).toBe(0);
    const h = trackQueue("q", { depthFn: () => 1, intervalMs: 1000 });
    expect(_liveTrackerCounts().queue).toBe(1);
    h.stop();
    expect(_liveTrackerCounts().queue).toBe(0);
  });

  it("stop() is idempotent", () => {
    const h = trackQueue("q", { depthFn: () => 1, intervalMs: 1000 });
    h.stop();
    h.stop();
    h.stop();
    expect(_liveTrackerCounts().queue).toBe(0);
  });

  it("N create/stop cycles leave nothing retained — the actual leak assertion", () => {
    for (let i = 0; i < 200; i++) {
      trackQueue(`q${i}`, { depthFn: () => i, intervalMs: 1000 }).stop();
      trackPool(`p${i}`, { activeFn: () => 1, idleFn: () => 1, intervalMs: 1000 }).stop();
    }
    expect(_liveTrackerCounts()).toEqual({ queue: 0, pool: 0 });
  });

  it("trackPool handles behave the same way", () => {
    const h = trackPool("p", { activeFn: () => 1, idleFn: () => 2, intervalMs: 1000 });
    expect(_liveTrackerCounts().pool).toBe(1);
    h.stop();
    expect(_liveTrackerCounts().pool).toBe(0);
  });

  it("COMPAT: clearInterval(handle) still stops polling", async () => {
    // Pins the pre-existing call shape (node/tests/track-queue-interval.test.ts
    // does exactly this). If a future refactor returns a plain object, this
    // fails here rather than silently in a consumer's process.
    let calls = 0;
    const h = trackQueue("compat", {
      depthFn: () => {
        calls += 1;
        return 1;
      },
      intervalMs: 10,
    });
    clearInterval(h);
    const seen = calls;
    await new Promise((r) => setTimeout(r, 60));
    expect(calls).toBe(seen); // no further ticks
  });

  it("warns once when live trackers pass the threshold", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      // Deliberately leak: clearInterval only, never stop().
      for (let i = 0; i < 70; i++) {
        clearInterval(trackQueue(`leak${i}`, { depthFn: () => 1, intervalMs: 1000 }));
      }
      expect(warn).toHaveBeenCalledWith(expect.stringContaining("live queue trackers"));
      const before = warn.mock.calls.length;
      for (let i = 70; i < 90; i++) {
        clearInterval(trackQueue(`leak${i}`, { depthFn: () => 1, intervalMs: 1000 }));
      }
      // Once, not once per creation — an unactionable warning storm is how a
      // real signal gets trained out of the reader.
      expect(warn.mock.calls.length).toBe(before);
    } finally {
      warn.mockRestore();
    }
  });

  it("NEGATIVE CONTROL: a live tracker is counted until stopped", () => {
    // Without this, every assertion above would also pass if the counter were
    // hardwired to 0 and stop() did nothing.
    const h = trackQueue("still-running", { depthFn: () => 1, intervalMs: 1000 });
    expect(_liveTrackerCounts().queue).toBe(1);
    expect(_liveTrackerCounts().queue).not.toBe(0);
    h.stop();
  });
});
