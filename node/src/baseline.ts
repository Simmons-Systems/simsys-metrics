/**
 * Process-wide service state + opt-in helpers (trackQueue, trackJob, safeLabel).
 *
 * Matches `simsys_metrics._baseline` and `simsys_metrics.helpers` (Python).
 *
 * Service identity + queue-timer tracking live on `globalThis` so they
 * survive across bundler chunk-splits — see registry.ts header for the
 * full rationale.
 */

import {
  queueDepth,
  jobsTotal,
  jobDurationSeconds,
  poolActive,
  poolIdle,
  poolWaiting,
  poolMax,
  collectorErrorsTotal,
} from "./registry.js";

/**
 * A poller handle. Structurally a NodeJS.Timeout with `stop()` attached, so
 * `clearInterval(handle)` keeps working byte-identically for callers written
 * against the old signature, and existing `NodeJS.Timeout` annotations stay
 * assignable. Prefer `stop()`: it is idempotent AND removes the handle from
 * the live set, which `clearInterval` cannot do.
 */
export type PollerHandle = NodeJS.Timeout & { stop(): void };

/**
 * Count one failed poller tick (#50319).
 *
 * Swallows its own errors on purpose: this runs on the failure path, and a
 * metrics package that throws out of its own error accounting turns a degraded
 * collector into an unhandled rejection inside a timer callback.
 */
function _bumpCollectorError(
  service: string,
  collector: "queue" | "pool",
  name: string,
): void {
  try {
    collectorErrorsTotal.labels({ service, collector, name }).inc();
  } catch {
    /* see docstring */
  }
}

interface SimsysBaselineState {
  service: string | null;
  queueTimers: Set<NodeJS.Timeout>;
  poolTimers: Set<NodeJS.Timeout>;
}

declare global {
  // eslint-disable-next-line no-var
  var __simsysMetricsBaselineState: SimsysBaselineState | undefined;
}

const _state: SimsysBaselineState = (globalThis.__simsysMetricsBaselineState ??= {
  service: null,
  queueTimers: new Set(),
  poolTimers: new Set(),
});

// A caller who only ever calls clearInterval() stops the polling but cannot
// remove the handle from the set -- undetectable from in here. Warn once when
// a set grows past a threshold no legitimate app reaches, naming the tracker
// so the leak is actionable rather than merely reported. This is the exact
// symptom Python's 0.3.6 changelog described when it deleted _QUEUE_THREADS:
// "apps that re-init queue trackers in factories".
const _LIVE_TRACKER_WARN_AT = 64;
const _warnedSets = new WeakSet<Set<NodeJS.Timeout>>();

function _warnEmptyName(kind: "queue" | "pool", name: unknown): void {
  // An empty name emits queue=""/pool="" — a series no dashboard template
  // matches, indistinguishable from a mislabelled one. Python has the same
  // gap and Go validates. Warn now, throw in the next major; the series is
  // still emitted so no deployed consumer breaks on upgrade.
  if (typeof name !== "string" || name.trim() === "") {
    console.warn(
      `[simsys-metrics] track${kind === "queue" ? "Queue" : "Pool"} name must ` +
        `be a non-empty string, got ${JSON.stringify(name)}. The series is ` +
        `still emitted for backward compatibility; this will throw in the ` +
        `next major version.`,
    );
  }
}

function _attachStop(
  timer: NodeJS.Timeout,
  set: Set<NodeJS.Timeout>,
  kind: string,
  name: string,
): PollerHandle {
  set.add(timer);
  if (set.size > _LIVE_TRACKER_WARN_AT && !_warnedSets.has(set)) {
    _warnedSets.add(set);
    console.warn(
      `[simsys-metrics] ${set.size} live ${kind} trackers (most recent: ` +
        `${JSON.stringify(name)}). Handles are only released by calling ` +
        `.stop() on the value ${kind === "queue" ? "trackQueue" : "trackPool"}() ` +
        `returns; clearInterval() stops polling but leaks the handle.`,
    );
  }
  let stopped = false;
  const handle = timer as PollerHandle;
  handle.stop = () => {
    if (stopped) return;
    stopped = true;
    clearInterval(timer);
    set.delete(timer);
  };
  return handle;
}

/** Live tracker counts. Test hook — not part of the public API. */
export function _liveTrackerCounts(): { queue: number; pool: number } {
  return { queue: _state.queueTimers.size, pool: _state.poolTimers.size };
}

/**
 * Set the process-wide service label. Called by install().
 *
 * The value is trimmed. `service` is the join key between this package and
 * simsys-logevent -- an operator pivots from
 * `simsys_http_requests_total{service="x"}` in Prometheus to
 * `{service="x"} | json` in Loki -- and simsys-logevent trims its own copy.
 * Without this, `"  portal  "` here and `"portal"` there are two different
 * identities and the pivot silently returns nothing.
 *
 * Trimming is safe to do now rather than deferring to a major: all 36
 * services reporting `simsys_build_info` were checked against live
 * Prometheus on 2026-08-22 and none carries leading or trailing whitespace,
 * so this is a measured no-op on the deployed fleet. A padded name is a
 * caller typo in every observed case, and the trimmed value is what the
 * caller meant.
 *
 * `install()` already rejects a falsy service, but `"   "` is truthy and
 * reaches here, so an empty-after-trim value is warned about and still set
 * -- consistent with the warn-now/throw-in-the-next-major policy.
 */
export function setService(service: string | null): void {
  if (service !== null) {
    // A second install() with a different service silently re-labels every
    // series (#50321). The per-app guards in adapters/express.ts and
    // adapters/hono.ts are keyed on the app OBJECT, so install(appA, "foo")
    // then install(appB, "bar") never reaches them.
    //
    // console.error, not warn: this corrupts every series in the process.
    // Behaviour is unchanged for now -- the fleet is swept for this marker
    // before it becomes a throw in the next major. Guarded on a non-null
    // prior so first install and rollback (setService(null)) stay quiet.
    const prior = _state.service;
    const next = service.trim();
    if (prior !== null && prior !== next) {
      console.error(
        `[simsys-metrics] SERVICE IDENTITY CHANGE ${JSON.stringify(prior)} -> ` +
          `${JSON.stringify(next)}. One service identity per process is the ` +
          `contract. Trackers already started will now emit under the NEW ` +
          `service, and the prior service's process metrics disappear. If you ` +
          `genuinely need two identities, run two processes. This will throw ` +
          `in the next major version.`,
      );
    }
    const trimmed = service.trim();
    if (trimmed !== service) {
      console.warn(
        `[simsys-metrics] service ${JSON.stringify(service)} has ` +
          `leading/trailing whitespace; using ${JSON.stringify(trimmed)}. ` +
          `\`service\` is the join key with simsys-logevent, which trims its ` +
          `own copy -- an untrimmed value here would not match in Loki.`,
      );
    }
    if (trimmed === "") {
      console.warn(
        `[simsys-metrics] service ${JSON.stringify(service)} is empty after ` +
          `trimming. Every series will carry service="", which no dashboard ` +
          `template will match. This will throw in the next major version.`,
      );
    }
    service = trimmed;
  }
  _state.service = service;
}

/**
 * Return the current service name without throwing. Used by install
 * rollback to capture pre-install state before mutating.
 */
export function _peekService(): string | null {
  return _state.service;
}

export function getService(): string {
  if (_state.service === null) {
    throw new Error(
      "simsys-metrics: no service set. Call install(app, { service, version }) first.",
    );
  }
  return _state.service;
}

// -------- trackQueue --------

export interface TrackQueueOpts {
  depthFn: () => number | Promise<number>;
  intervalMs?: number;
}

/**
 * Poll ``depthFn()`` every ``intervalMs`` (default 5000) and update the
 * ``simsys_queue_depth`` gauge for the given queue name.
 *
 * Returns a {@link PollerHandle}: a NodeJS.Timeout carrying an idempotent
 * `stop()`. Call `stop()` when the queue goes away -- it clears the interval
 * and releases the handle. `clearInterval(handle)` still works but leaks the
 * handle for the life of the process, which is the bug this shape fixes
 * (Redmine #50318). Already `.unref()`ed, so it never holds the process open.
 */
export function trackQueue(
  name: string,
  opts: TrackQueueOpts,
): PollerHandle {
  _warnEmptyName("queue", name);
  const service = getService();
  const intervalMs = opts.intervalMs ?? 5000;
  // Reject intervalMs <= 0: setInterval(..., 0) creates a hot loop that
  // pegs the event loop. Be loud about misconfig instead of silently
  // melting the worker.
  if (typeof intervalMs !== "number" || !Number.isFinite(intervalMs) || intervalMs <= 0) {
    throw new Error(
      `trackQueue: opts.intervalMs must be a positive finite number of milliseconds, got ${String(intervalMs)}`,
    );
  }

  let warnedFailure = false;

  const tick = async () => {
    let depth: number;
    try {
      const raw = await opts.depthFn();
      depth = Math.trunc(Number(raw) || 0);
    } catch (err) {
      // #50319: do NOT write the gauge. Until 2.0.0 this set 0, which is
      // indistinguishable from a genuinely drained queue -- and only one of
      // those two ever gets investigated. Returning early leaves the last
      // known value standing, and leaves the series ABSENT entirely if the
      // very first tick failed, which is the honest representation of "we
      // have never successfully read this queue".
      //
      // This is also what the README claimed all along: it described
      // trackPool's catch-the-whole-tick behaviour and applied it to both.
      _bumpCollectorError(service, "queue", name);
      if (!warnedFailure) {
        warnedFailure = true;
        console.warn(
          `[simsys-metrics] depthFn for queue ${JSON.stringify(name)} failed: ` +
            `${String(err)}. The gauge keeps its last known value (absent if ` +
            `this was the first tick) and simsys_collector_errors_total is ` +
            `incremented. Future failures will be silent.`,
        );
      }
      return;
    }
    try {
      queueDepth.labels({ service, queue: name }).set(depth);
    } catch {
      /* swallow */
    }
  };

  // First sample immediately so the gauge is populated before the first scrape.
  void tick();
  const timer = setInterval(tick, intervalMs);
  // Don't hold the event loop open purely for the metric timer.
  if (typeof timer.unref === "function") {
    timer.unref();
  }
  return _attachStop(timer, _state.queueTimers, "queue", name);
}

// -------- trackJob --------

type AnyFn = (...args: unknown[]) => unknown;

/**
 * Wrap a function to emit ``simsys_jobs_total`` + ``simsys_job_duration_seconds``.
 *
 * Two usage shapes:
 *
 *   // 1. Function-wrapper style (sync or async):
 *   const runInference = trackJob("inference")(async (...args) => { ... });
 *
 *   // 2. Ad-hoc async span (no wrapping):
 *   await trackJob("inference").run(async () => {
 *     // ...the work...
 *   });
 */
export interface JobTracker {
  <F extends AnyFn>(fn: F): F;
  run<T>(fn: () => T | Promise<T>): Promise<T>;
}

export function trackJob(jobName: string): JobTracker {
  const record = (elapsedSec: number, outcome: "success" | "error") => {
    const service = getService();
    jobsTotal.labels({ service, job: jobName, outcome }).inc();
    jobDurationSeconds
      .labels({ service, job: jobName, outcome })
      .observe(elapsedSec);
  };

  const wrapClean = <F extends AnyFn>(fn: F): F => {
    const wrapped = function (this: unknown, ...args: unknown[]): unknown {
      const start = process.hrtime.bigint();
      let settled = false;
      try {
        const out = fn.apply(this, args);
        if (out && typeof (out as Promise<unknown>).then === "function") {
          // Async: record on settle.
          return (out as Promise<unknown>).then(
            (v) => {
              if (!settled) {
                settled = true;
                record(
                  Number(process.hrtime.bigint() - start) / 1e9,
                  "success",
                );
              }
              return v;
            },
            (e) => {
              if (!settled) {
                settled = true;
                record(Number(process.hrtime.bigint() - start) / 1e9, "error");
              }
              throw e;
            },
          );
        }
        // Sync success.
        settled = true;
        record(Number(process.hrtime.bigint() - start) / 1e9, "success");
        return out;
      } catch (e) {
        if (!settled) {
          settled = true;
          record(Number(process.hrtime.bigint() - start) / 1e9, "error");
        }
        throw e;
      }
    };
    return wrapped as unknown as F;
  };

  const tracker = wrapClean as JobTracker;

  tracker.run = async <T,>(fn: () => T | Promise<T>): Promise<T> => {
    const start = process.hrtime.bigint();
    try {
      const out = await fn();
      record(Number(process.hrtime.bigint() - start) / 1e9, "success");
      return out;
    } catch (e) {
      record(Number(process.hrtime.bigint() - start) / 1e9, "error");
      throw e;
    }
  };

  return tracker;
}

// -------- trackPool --------

export interface TrackPoolOpts {
  activeFn: () => number | Promise<number>;
  idleFn: () => number | Promise<number>;
  waitingFn?: () => number | Promise<number>;
  max?: number;
  intervalMs?: number;
}

export function trackPool(
  name: string,
  opts: TrackPoolOpts,
): PollerHandle {
  _warnEmptyName("pool", name);
  const service = getService();
  const intervalMs = opts.intervalMs ?? 5000;
  if (typeof intervalMs !== "number" || !Number.isFinite(intervalMs) || intervalMs <= 0) {
    throw new Error(
      `trackPool: opts.intervalMs must be a positive finite number of milliseconds, got ${String(intervalMs)}`,
    );
  }

  if (opts.max != null && opts.max > 0) {
    poolMax.labels({ service, pool: name }).set(opts.max);
  }

  let warnedFailure = false;

  const tick = async () => {
    // #50319: read EVERY callback before writing ANY gauge. The previous shape
    // interleaved reads and writes inside one try, so if idleFn rejected,
    // poolActive had already been set for this tick -- a pool reporting a
    // fresh `active` beside a stale `idle`, a self-inconsistent snapshot no
    // consumer can detect. Computing first makes the tick all-or-nothing.
    const writes: Array<[typeof poolActive, number]> = [];
    try {
      writes.push([poolActive, Math.max(0, Math.trunc(Number(await opts.activeFn()) || 0))]);
      writes.push([poolIdle, Math.max(0, Math.trunc(Number(await opts.idleFn()) || 0))]);
      if (opts.waitingFn) {
        writes.push([
          poolWaiting,
          Math.max(0, Math.trunc(Number(await opts.waitingFn()) || 0)),
        ]);
      }
    } catch (err) {
      _bumpCollectorError(service, "pool", name);
      if (!warnedFailure) {
        warnedFailure = true;
        console.warn(
          `[simsys-metrics] pool callback for ${JSON.stringify(name)} failed: ` +
            `${String(err)}. The gauges keep their last known values (absent ` +
            `if this was the first tick) and simsys_collector_errors_total is ` +
            `incremented. Future failures will be silent.`,
        );
      }
      return;
    }
    for (const [gauge, value] of writes) {
      gauge.labels({ service, pool: name }).set(value);
    }
  };

  void tick();
  const timer = setInterval(tick, intervalMs);
  if (typeof timer.unref === "function") {
    timer.unref();
  }
  return _attachStop(timer, _state.poolTimers, "pool", name);
}

// -------- safeLabel --------

const OTHER = "other";

/**
 * Coerce any user-facing value into a bounded allow-list.
 *
 *   safeLabel(req.query.ticker, new Set(["AAPL", "GOOG"]))  // -> "AAPL" or "other"
 */
export function safeLabel(
  value: unknown,
  allowed: Iterable<string>,
): string {
  if (value === null || value === undefined) return OTHER;
  const s = typeof value === "string" ? value : String(value);
  const set =
    allowed instanceof Set ? allowed : new Set<string>(allowed as Iterable<string>);
  return set.has(s) ? s : OTHER;
}

// -------- test helpers --------

export function _resetForTests(): void {
  _state.service = null;
  for (const t of _state.queueTimers) clearInterval(t);
  _state.queueTimers.clear();
  for (const t of _state.poolTimers) clearInterval(t);
  _state.poolTimers.clear();
}
