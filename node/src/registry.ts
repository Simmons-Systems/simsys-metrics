/**
 * Prefix-guarded metric definitions and the shared prom-client registry.
 *
 * Every simsys metric is forced to start with `simsys_` — the guards make it
 * impossible to accidentally ship a metric under a bare name when using this
 * package. Matches `simsys_metrics._registry` (Python).
 *
 * Identity stability across bundler chunk-splitting:
 * Next.js / webpack standalone bundling can inline this module into multiple
 * server chunks (e.g. `instrumentation.ts` and `app/api/metrics/route.ts`
 * land in different chunks). Each chunk gets its own module-instance scope,
 * so plain `export const registry = new Registry()` would create one
 * Registry per chunk — `installNext()` writes samples to one, the route
 * handler reads from the other, and `/metrics` returns HELP/TYPE only.
 *
 * Defence: every stateful singleton (the registry, every baseline metric,
 * the default-metrics-registered flag, the missing-service warning Set)
 * is pinned to `globalThis` via `??=`. A second module-instance import
 * skips construction and re-exports the singletons stored by the first
 * load. Consumers get identity-stable references regardless of how the
 * bundler chunked them. Belt-and-suspenders alongside Next's
 * `serverExternalPackages: ["@simsys/metrics", "prom-client"]` config.
 */

import {
  Counter,
  Gauge,
  Histogram,
  Registry,
  collectDefaultMetrics,
} from "prom-client";

export const PREFIX = "simsys_";

interface SimsysRegistryState {
  registry: Registry;
  httpRequestsTotal: Counter;
  httpRequestDurationSeconds: Histogram;
  buildInfo: Gauge;
  queueDepth: Gauge;
  jobsTotal: Counter;
  jobDurationSeconds: Histogram;
  progressProcessedTotal: Counter;
  progressRemaining: Gauge;
  progressRatePerSecond: Gauge;
  progressEstimatedCompletionTimestamp: Gauge;
  poolActive: Gauge;
  poolIdle: Gauge;
  poolWaiting: Gauge;
  poolMax: Gauge;
  defaultMetricsRegistered: boolean;
  warnedMissingService: Set<string>;
}

declare global {
  // eslint-disable-next-line no-var
  var __simsysMetricsRegistryState: SimsysRegistryState | undefined;
}

function guardName(name: string): string {
  if (typeof name !== "string" || !name.startsWith(PREFIX)) {
    throw new Error(
      `simsys-metrics refuses to register metric '${name}': all metric names must start with '${PREFIX}'.`,
    );
  }
  return name;
}

function initRegistryState(): SimsysRegistryState {
  const reg = new Registry();
  return {
    registry: reg,
    httpRequestsTotal: new Counter({
      name: guardName("simsys_http_requests_total"),
      help: "Total HTTP requests handled, bucketed by status class.",
      labelNames: ["service", "method", "route", "status"],
      registers: [reg],
    }),
    httpRequestDurationSeconds: new Histogram({
      name: guardName("simsys_http_request_duration_seconds"),
      help: "HTTP request duration in seconds, labelled by route template.",
      labelNames: ["service", "method", "route"],
      registers: [reg],
      buckets: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    }),
    buildInfo: new Gauge({
      name: guardName("simsys_build_info"),
      help: "Service build information. Always equal to 1; read labels for actual data.",
      labelNames: ["service", "version", "commit", "started_at"],
      registers: [reg],
    }),
    queueDepth: new Gauge({
      name: guardName("simsys_queue_depth"),
      help: "Current depth of an application-owned queue.",
      labelNames: ["service", "queue"],
      registers: [reg],
    }),
    jobsTotal: new Counter({
      name: guardName("simsys_jobs_total"),
      help: "Jobs completed, labelled by name and outcome (success/error).",
      labelNames: ["service", "job", "outcome"],
      registers: [reg],
    }),
    jobDurationSeconds: new Histogram({
      name: guardName("simsys_job_duration_seconds"),
      help: "Job duration in seconds.",
      labelNames: ["service", "job", "outcome"],
      registers: [reg],
      buckets: [0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300],
    }),
    progressProcessedTotal: new Counter({
      name: guardName("simsys_progress_processed_total"),
      help: "Items completed in a batch operation (monotonic counter).",
      labelNames: ["service", "operation"],
      registers: [reg],
    }),
    progressRemaining: new Gauge({
      name: guardName("simsys_progress_remaining"),
      help: "Items not yet completed in a batch operation.",
      labelNames: ["service", "operation"],
      registers: [reg],
    }),
    progressRatePerSecond: new Gauge({
      name: guardName("simsys_progress_rate_per_second"),
      help: "EWMA-smoothed processing rate in items per second.",
      labelNames: ["service", "operation"],
      registers: [reg],
    }),
    progressEstimatedCompletionTimestamp: new Gauge({
      name: guardName("simsys_progress_estimated_completion_timestamp"),
      help: "Estimated completion time as a Unix timestamp (0 when unknown).",
      labelNames: ["service", "operation"],
      registers: [reg],
    }),
    poolActive: new Gauge({
      name: guardName("simsys_pool_active"),
      help: "Number of active (checked-out) connections in a pool.",
      labelNames: ["service", "pool"],
      registers: [reg],
    }),
    poolIdle: new Gauge({
      name: guardName("simsys_pool_idle"),
      help: "Number of idle connections in a pool.",
      labelNames: ["service", "pool"],
      registers: [reg],
    }),
    poolWaiting: new Gauge({
      name: guardName("simsys_pool_waiting"),
      help: "Number of requests waiting for a pool connection.",
      labelNames: ["service", "pool"],
      registers: [reg],
    }),
    poolMax: new Gauge({
      name: guardName("simsys_pool_max"),
      help: "Maximum pool size.",
      labelNames: ["service", "pool"],
      registers: [reg],
    }),
    defaultMetricsRegistered: false,
    warnedMissingService: new Set<string>(),
  };
}

const _state: SimsysRegistryState = (globalThis.__simsysMetricsRegistryState ??=
  initRegistryState());

/**
 * The simsys-owned Prometheus registry. We do NOT use prom-client's default
 * global registry so consumer apps can host their own metrics side-by-side
 * without collisions, and so tests can reset cleanly.
 */
export const registry: Registry = _state.registry;

/**
 * Warn (don't error) when a custom metric's labelNames omit "service".
 * The shared $service-templated Grafana dashboards filter on the
 * service label; metrics without it can't participate in the
 * cross-service contract.
 */
function warnIfMissingService(name: string, labelNames: readonly string[]): void {
  if (labelNames.includes("service")) return;
  if (_state.warnedMissingService.has(name)) return;
  _state.warnedMissingService.add(name);
  // eslint-disable-next-line no-console
  console.warn(
    `[simsys-metrics] metric "${name}" registered without 'service' in ` +
      `labelNames=[${labelNames.join(", ")}] — it will NOT participate in ` +
      `$service-templated dashboards. Add 'service' to labelNames if you ` +
      `want cross-service queries to work for this metric.`,
  );
}

export function makeCounter(
  name: string,
  help: string,
  labelNames: readonly string[] = [],
): Counter {
  warnIfMissingService(name, labelNames);
  return new Counter({
    name: guardName(name),
    help,
    labelNames: [...labelNames],
    registers: [registry],
  });
}

export function makeGauge(
  name: string,
  help: string,
  labelNames: readonly string[] = [],
): Gauge {
  warnIfMissingService(name, labelNames);
  return new Gauge({
    name: guardName(name),
    help,
    labelNames: [...labelNames],
    registers: [registry],
  });
}

export function makeHistogram(
  name: string,
  help: string,
  labelNames: readonly string[] = [],
  buckets?: readonly number[],
): Histogram {
  warnIfMissingService(name, labelNames);
  return new Histogram({
    name: guardName(name),
    help,
    labelNames: [...labelNames],
    registers: [registry],
    ...(buckets ? { buckets: [...buckets] } : {}),
  });
}

// -------- HTTP metrics (baseline) --------

export const httpRequestsTotal: Counter = _state.httpRequestsTotal;
export const httpRequestDurationSeconds: Histogram = _state.httpRequestDurationSeconds;

export function statusBucket(statusCode: number | string): string {
  let code = statusCode as number;
  if (typeof code !== "number") {
    code = Number(code);
    if (!Number.isFinite(code)) return "5xx";
  }
  if (code >= 100 && code < 600) {
    // ⚡ Bolt: Fast path for valid HTTP status codes using math operation
    // rather than branching logic.
    return Math.floor(code / 100) + "xx";
  }
  return "5xx";
}

// Allow-list of HTTP methods that get their own series. Anything outside
// this set (case-insensitive) collapses to "OTHER" so attacker-controlled
// garbage methods (e.g. "X_AUDIT_1", "ASDF") cannot blow out the label
// space. Includes RFC 9110 standard methods plus PATCH (RFC 5789).
const ALLOWED_METHODS: ReadonlySet<string> = new Set([
  "GET",
  "HEAD",
  "POST",
  "PUT",
  "DELETE",
  "CONNECT",
  "OPTIONS",
  "TRACE",
  "PATCH",
]);

export function normalizeMethod(method: unknown): string {
  if (typeof method !== "string") return "OTHER";
  // ⚡ Bolt: Fast path avoids allocating a new uppercase string if the input
  // method is already correctly capitalized and allowed.
  if (ALLOWED_METHODS.has(method)) return method;
  const upper = method.toUpperCase();
  return ALLOWED_METHODS.has(upper) ? upper : "OTHER";
}

// -------- Build info --------

export const buildInfo: Gauge = _state.buildInfo;

// -------- Queue + job (opt-in) --------

export const queueDepth: Gauge = _state.queueDepth;
export const jobsTotal: Counter = _state.jobsTotal;
export const jobDurationSeconds: Histogram = _state.jobDurationSeconds;

// -------- Progress tracking (opt-in) --------

export const progressProcessedTotal: Counter = _state.progressProcessedTotal;
export const progressRemaining: Gauge = _state.progressRemaining;
export const progressRatePerSecond: Gauge = _state.progressRatePerSecond;
export const progressEstimatedCompletionTimestamp: Gauge =
  _state.progressEstimatedCompletionTimestamp;

// -------- Pool tracking (opt-in) --------

export const poolActive: Gauge = _state.poolActive;
export const poolIdle: Gauge = _state.poolIdle;
export const poolWaiting: Gauge = _state.poolWaiting;
export const poolMax: Gauge = _state.poolMax;

// -------- Default runtime metrics (opt-in wrapper) --------

export function registerNodeDefaultMetrics(service: string): void {
  // Always refresh the default labels — `setDefaultLabels({service})`
  // governs the static `service` label on the prom-client default
  // metrics (`nodejs_*`, `process_*`). On a service-swap (e.g. Next
  // dev-mode hot-reload installs a different service into the same
  // process, or any adapter is re-initialised), the prior service's
  // label would otherwise persist on every default metric forever.
  // Refreshing on every call keeps the label consistent with the
  // adapter's current `service` identity.
  registry.setDefaultLabels({ service });
  if (_state.defaultMetricsRegistered) return;
  _state.defaultMetricsRegistered = true;
  // prom-client's default metrics cover GC, event loop lag, and heap details
  // with the `nodejs_` / `process_` prefixes. We register them to OUR registry
  // so they're served by the same /metrics endpoint. They won't carry the
  // `service` label but they're useful enough to include — the static label
  // applied via `registry.setDefaultLabels` above propagates to every sample.
  collectDefaultMetrics({ register: registry });
}

/**
 * Test-only: clear the default-metrics-registered sentinel and the
 * missing-service warning Set. Does NOT drop the metric instances or
 * the registry itself — those are identity-stable singletons.
 */
export function _resetRegistryStateForTests(): void {
  _state.defaultMetricsRegistered = false;
  _state.warnedMissingService.clear();
}
