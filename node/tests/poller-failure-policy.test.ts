/**
 * #50319 -- a failing poller must not fabricate a value.
 *
 * Until 2.0.0 `trackQueue` set the gauge to 0 whenever `depthFn` threw, with
 * no warning at all, while `node/README.md` claimed "the gauge stays at its
 * last successful value or 0". That description belonged to `trackPool`,
 * whose catch wrapped the whole tick body. The README was documenting one
 * function's behaviour and applying it to both.
 *
 * Reporting 0 makes a broken callback indistinguishable from a genuinely
 * drained queue, and only one of those two ever gets investigated -- a queue
 * at 0 is the state everyone is hoping for.
 *
 * WHY THIS FILE IS NEW. Before it, the Node lane had NO test of poller
 * failure behaviour whatsoever: 138 tests passed both before and after the
 * behaviour change, so the suite's green carried no information about the one
 * thing this release alters. The Python lane had `test_track_queue.py`
 * asserting the defect outright; Node simply never looked.
 */

import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { trackQueue, trackPool, setService } from "../src/baseline.js";
import { _resetForTests as _resetBase } from "../src/baseline.js";
import { registry } from "../src/registry.js";

/** Current value of a labelled sample, or undefined when the series is absent. */
async function sample(
  metric: string,
  labels: Record<string, string>,
): Promise<number | undefined> {
  for (const m of await registry.getMetricsAsJSON()) {
    if (m.name !== metric) continue;
    for (const v of (m as { values: Array<{ labels: Record<string, string>; value: number }> })
      .values) {
      if (Object.entries(labels).every(([k, val]) => v.labels[k] === val)) {
        return v.value;
      }
    }
  }
  return undefined;
}

const tick = () => new Promise((r) => setTimeout(r, 30));

describe("poller failure policy (#50319)", () => {
  const handles: Array<{ stop(): void }> = [];

  beforeEach(() => {
    _resetBase();
    registry.resetMetrics();
    setService("poller-failure-test");
    vi.spyOn(console, "warn").mockImplementation(() => {});
  });

  afterEach(() => {
    for (const h of handles.splice(0)) h.stop();
    vi.restoreAllMocks();
  });

  it("leaves simsys_queue_depth ABSENT when the first tick fails", async () => {
    handles.push(
      trackQueue("broken", {
        depthFn: () => {
          throw new Error("boom");
        },
        intervalMs: 10,
      }),
    );
    await tick();

    expect(
      await sample("simsys_queue_depth", { queue: "broken" }),
    ).toBeUndefined();

    const errors = await sample("simsys_collector_errors_total", {
      collector: "queue",
      name: "broken",
    });
    expect(errors).toBeGreaterThanOrEqual(1);
  });

  it("preserves the last known depth when a LATER tick fails", async () => {
    let ok = true;
    handles.push(
      trackQueue("flaky", {
        depthFn: () => {
          if (!ok) throw new Error("boom");
          return 42;
        },
        intervalMs: 10,
      }),
    );
    await tick();
    expect(await sample("simsys_queue_depth", { queue: "flaky" })).toBe(42);

    ok = false;
    await tick();

    // The value stands rather than resetting to 0 -- this is the half a
    // long-running service actually hits.
    expect(await sample("simsys_queue_depth", { queue: "flaky" })).toBe(42);
    expect(
      await sample("simsys_collector_errors_total", {
        collector: "queue",
        name: "flaky",
      }),
    ).toBeGreaterThanOrEqual(1);
  });

  it("warns exactly once per tracker, not once per tick", async () => {
    handles.push(
      trackQueue("noisy", {
        depthFn: () => {
          throw new Error("boom");
        },
        intervalMs: 10,
      }),
    );
    await tick();
    await tick();

    const warns = (console.warn as unknown as { mock: { calls: unknown[][] } }).mock.calls;
    const ours = warns.filter((c) => String(c[0]).includes("depthFn for queue"));
    expect(ours).toHaveLength(1);

    // ...while the error COUNTER keeps counting every tick. One is for a
    // human reading logs, the other is for a query.
    const errors = await sample("simsys_collector_errors_total", {
      collector: "queue",
      name: "noisy",
    });
    expect(errors).toBeGreaterThan(1);
  });

  it("leaves pool gauges ABSENT when the first tick fails", async () => {
    handles.push(
      trackPool("brokenpool", {
        activeFn: () => {
          throw new Error("boom");
        },
        idleFn: () => 1,
        intervalMs: 10,
      }),
    );
    await tick();

    expect(await sample("simsys_pool_active", { pool: "brokenpool" })).toBeUndefined();
    expect(await sample("simsys_pool_idle", { pool: "brokenpool" })).toBeUndefined();
    expect(
      await sample("simsys_collector_errors_total", {
        collector: "pool",
        name: "brokenpool",
      }),
    ).toBeGreaterThanOrEqual(1);
  });

  it("never writes a PARTIAL pool snapshot when a later callback fails", async () => {
    // The pre-2.0.0 shape interleaved reads and writes inside one try, so a
    // throwing idleFn left poolActive already updated for that tick: a fresh
    // `active` beside a stale `idle`, which no consumer can detect. This is
    // the assertion that pins the all-or-nothing tick.
    let ok = true;
    let active = 1;
    handles.push(
      trackPool("partial", {
        activeFn: () => active,
        idleFn: () => {
          if (!ok) throw new Error("boom");
          return 10;
        },
        intervalMs: 10,
      }),
    );
    await tick();
    expect(await sample("simsys_pool_active", { pool: "partial" })).toBe(1);
    expect(await sample("simsys_pool_idle", { pool: "partial" })).toBe(10);

    ok = false;
    active = 99; // would be written first under the old interleaved shape
    await tick();

    expect(await sample("simsys_pool_active", { pool: "partial" })).toBe(1);
    expect(await sample("simsys_pool_idle", { pool: "partial" })).toBe(10);
  });
});
