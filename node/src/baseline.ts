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
} from "./registry.js";

interface SimsysBaselineState {
  service: string | null;
  queueTimers: NodeJS.Timeout[];
  poolTimers: NodeJS.Timeout[];
}

declare global {
  // eslint-disable-next-line no-var
  var __simsysMetricsBaselineState: SimsysBaselineState | undefined;
}

const _state: SimsysBaselineState = (globalThis.__simsysMetricsBaselineState ??= {
  service: null,
  queueTimers: [],
  poolTimers: [],
});

export function setService(service: string | null): void {
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
 * Returns the interval handle. Safe to ignore; call `.unref()` yourself if
 * you need the process to exit while the timer is pending.
 */
export function trackQueue(
  name: string,
  opts: TrackQueueOpts,
): NodeJS.Timeout {
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

  const tick = async () => {
    let depth = 0;
    try {
      const raw = await opts.depthFn();
      depth = Math.trunc(Number(raw) || 0);
    } catch {
      depth = 0;
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
  _state.queueTimers.push(timer);
  return timer;
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
): NodeJS.Timeout {
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

  const tick = async () => {
    try {
      const active = Math.max(0, Math.trunc(Number(await opts.activeFn()) || 0));
      const idle = Math.max(0, Math.trunc(Number(await opts.idleFn()) || 0));
      poolActive.labels({ service, pool: name }).set(active);
      poolIdle.labels({ service, pool: name }).set(idle);
      if (opts.waitingFn) {
        const waiting = Math.max(0, Math.trunc(Number(await opts.waitingFn()) || 0));
        poolWaiting.labels({ service, pool: name }).set(waiting);
      }
    } catch {
      /* swallow — misbehaving callbacks shouldn't crash the timer */
    }
  };

  void tick();
  const timer = setInterval(tick, intervalMs);
  if (typeof timer.unref === "function") {
    timer.unref();
  }
  _state.poolTimers.push(timer);
  return timer;
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
  while (_state.queueTimers.length) {
    const t = _state.queueTimers.pop();
    if (t) clearInterval(t);
  }
  while (_state.poolTimers.length) {
    const t = _state.poolTimers.pop();
    if (t) clearInterval(t);
  }
}
