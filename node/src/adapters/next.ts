/**
 * Next.js adapter.
 *
 * Next.js standalone has no Express-style middleware chain to plug into,
 * so installNext() patches `http.Server.prototype.emit` to capture every
 * request finish — same trick OpenTelemetry's Next instrumentation uses.
 * Combined with `instrumentation.ts` (server-startup hook) for baseline
 * registration and an `app/api/metrics/route.ts` re-export for the
 * scrape endpoint, this gives the full Express-equivalent surface.
 *
 * Cardinality discipline: route labels go through bucketRoute() which
 * strips query strings, percent-decodes segments, normalizes numeric
 * segments to `:id`, UUIDs to `:uuid`, mixed-alphanumeric / slug /
 * percent-decoded-non-ASCII segments to `:str`, and collapses anything
 * > 5 segments to `/<a>/<b>/__deep__`. Paths > 8KB short-circuit to
 * `/__toolong__`. Cardinality is bounded regardless of attacker URL
 * shape — without `:str` collapse, an attacker spraying unique slugs
 * would create one Prometheus time series per slug, exhausting
 * Prometheus memory. Consumers can pass `routeTemplates` for
 * high-fidelity overrides on legitimate dynamic paths.
 *
 * Two more layers on top of bucketRoute() (infra#37576): 404 responses
 * collapse to `/__unmatched__` (scanner wordlists are lowercase-wordish,
 * which SAFE_TEXT_SEGMENT_RE keeps verbatim — the status code is the
 * only reliable "this isn't a real route" signal), and distinct route
 * labels are capped per process (default 300, `maxRoutes` option) with
 * overflow recorded as `/__overflow__`.
 */

import http from "node:http";

import {
  httpRequestsTotal,
  httpRequestDurationSeconds,
  normalizeMethod,
  statusBucket,
  registerNodeDefaultMetrics,
} from "../registry.js";
import {
  registerProcessCollector,
  restoreProcessCollector,
  type ProcessCollectorRollbackState,
} from "../process.js";
import {
  detectCommit,
  startedAtNow,
  registerBuildInfo,
  unregisterBuildInfoIfOwned,
  type BuildInfoLabels,
} from "../buildinfo.js";
import { setService, _peekService } from "../baseline.js";

export interface RouteTemplate {
  /**
   * Regex tested against every request URL on the hot path. Catastrophic-
   * backtracking patterns (e.g. `^(a+)+$`, `^(.*)+$`) will block the
   * event loop on attacker-shaped input — `RegExp.test()` is synchronous
   * and there is no per-call timeout. Use linear-complexity regexes only;
   * if you need permissive matching, anchor the pattern (`^/api/`) and
   * avoid nested quantifiers.
   */
  pattern: RegExp;
  template: string;
}

export interface NextInstallOpts {
  service: string;
  version: string;
  commit?: string;
  /** Path served by the user's `app/api/metrics/route.ts`. Default `/api/metrics`. */
  metricsPath?: string;
  /** Optional regex→template overrides for high-fidelity route labels. */
  routeTemplates?: RouteTemplate[];
  /**
   * Hard cap on distinct route label values minted per process; routes
   * beyond it are recorded as `/__overflow__`. Bounds cardinality even
   * when the app serves 2xx for arbitrary paths. Default 300.
   */
  maxRoutes?: number;
}

const NUMERIC_RE = /^\d+$/;
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// Static segments that stay verbatim when the surrounding path is
// otherwise text — safe lower-case words, not user-controllable. This
// is the allow-list approach: any unrecognised text segment collapses
// to `:str` to bound cardinality. Adding common English path nouns
// here avoids labelling every static API namespace as `:str:str/:str`.
//
// The regex matches segments that look like API namespaces or path
// nouns: lowercase ASCII letters, hyphens, dots; <= 32 chars; no
// digits, no uppercase, no other punctuation. Anything outside this
// shape (mixed alphanumeric, base64, JWTs, slug IDs like
// `ORD-9981`, percent-encoded bytes) collapses to `:str`. This bounds
// cardinality regardless of attacker input shape: an attacker spraying
// `/api/products/<n>` gets one label `/api/products/:str` instead of
// one label per unique `<n>`.
const SAFE_TEXT_SEGMENT_RE = /^[a-z][a-z.-]{0,31}$/;

const MAX_PATH_LENGTH = 8192;

// 404s collapse to one label. SAFE_TEXT_SEGMENT_RE keeps wordish
// segments verbatim, and internet scanner wordlists (`/wp-login.php`,
// `/abantecart/index.php`, …) are exactly wordish — on a public vhost
// they mint thousands of route labels (infra#37576: ~8k routes / 14 MB
// scrape body on bfr-leadership in 10 days). The Express adapter
// already labels router-miss traffic `__unmatched__`; a 404 status is
// the same fact observed after the fact, which is all the raw
// http.Server patch has. Real-route 4xx (401/403 auth denials) keep
// their route label.
const UNMATCHED_ROUTE = "/__unmatched__";

// Defense-in-depth behind the 404 collapse: a hard cap on distinct
// route labels minted per process, for apps that 200 arbitrary paths
// (catch-all routes). Sentinel labels can't collide with bucketRoute
// output — `__` fails SAFE_TEXT_SEGMENT_RE, so attacker-supplied
// `/__overflow__` buckets to `/:str`.
const OVERFLOW_ROUTE = "/__overflow__";
const DEFAULT_MAX_ROUTES = 300;

/**
 * Enforce the distinct-route cap. The seen-set lives on globalThis
 * (the 0.4.2 chunk-split-safety pattern) so every module instance
 * spends from the same budget.
 */
function capRoute(route: string, maxRoutes: number): string {
  const seen = (globalThis.__simsysNextSeenRoutes ??= new Set<string>());
  if (seen.has(route)) return route;
  if (seen.size >= maxRoutes) return OVERFLOW_ROUTE;
  seen.add(route);
  return route;
}

/** Test hook: reset the distinct-route cap state. */
export function _resetRouteCapForTests(): void {
  globalThis.__simsysNextSeenRoutes = undefined;
}

/**
 * Pure function: turn a raw URL path into a bounded-cardinality route label.
 * Exported for testing + consumer introspection.
 *
 * Bucketing rules, in order:
 *   1. Path > 8KB → `/__toolong__` (defensive cap; pathological URLs
 *      shouldn't burn CPU traversing `split("/")`).
 *   2. Empty or `/` → `/`.
 *   3. Any `routeTemplates` whose `pattern.test(path)` returns true →
 *      that template (consumer override).
 *   4. > 5 segments → `/<a>/<b>/__deep__` (depth cap).
 *   5. Per-segment normalization:
 *        a. Numeric (`/^\d+$/`) → `:id`
 *        b. UUID v4-shaped → `:uuid`
 *        c. `..` / `.` → kept verbatim (defends against weird URL shapes)
 *        d. "Safe" namespace tokens (lowercase ASCII <= 32 chars) → kept
 *        e. Anything else (mixed alphanumeric, slug IDs, base64,
 *           percent-encoded bytes, Unicode, …) → `:str`
 *   6. Percent-encoded sequences are decoded BEFORE classification so
 *      `%41` / `A` produce the same label.
 */
export function bucketRoute(
  url: string,
  templates: readonly RouteTemplate[] = [],
): string {
  // Defensive: cap pathological URL length before any work.
  if (typeof url !== "string" || url.length > MAX_PATH_LENGTH) {
    return "/__toolong__";
  }

  // Strip query string + fragment.
  const qsIdx = url.indexOf("?");
  let path = qsIdx >= 0 ? url.slice(0, qsIdx) : url;
  const hashIdx = path.indexOf("#");
  if (hashIdx >= 0) path = path.slice(0, hashIdx);

  if (path === "" || path === "/") return "/";

  // Custom templates win over default bucketing. Tested against the
  // RAW (un-decoded) path so consumers can match on the same string
  // their framework saw.
  for (const t of templates) {
    if (t.pattern.test(path)) return t.template;
  }

  // Split + normalize.
  const parts = path.split("/").slice(1);
  if (parts.length > 5) {
    return `/${classifySegment(parts[0] ?? "")}/${classifySegment(parts[1] ?? "")}/__deep__`;
  }
  const normalized = parts.map(classifySegment);
  return "/" + normalized.join("/");
}

function classifySegment(raw: string): string {
  // Decode percent-encoding before classifying so /%41 == /A. A
  // malformed sequence (e.g. lone `%`) throws — fall through to the
  // raw segment in that case so we still produce SOME bucket.
  let decoded: string;
  try {
    decoded = decodeURIComponent(raw);
  } catch {
    decoded = raw;
  }
  if (decoded === "") return "";
  if (NUMERIC_RE.test(decoded)) return ":id";
  if (UUID_RE.test(decoded)) return ":uuid";
  // Allow `.` / `..` through verbatim — they're rare path tokens and
  // bucketing them as `:str` would obscure attempts at traversal.
  if (decoded === "." || decoded === "..") return decoded;
  if (SAFE_TEXT_SEGMENT_RE.test(decoded)) return decoded;
  return ":str";
}

declare global {
  // eslint-disable-next-line no-var
  var __simsysNextInstalled: boolean | undefined;
  // eslint-disable-next-line no-var
  var __simsysNextSeenRoutes: Set<string> | undefined;
  // eslint-disable-next-line no-var
  var __simsysNextOrigEmit:
    | ((this: http.Server, event: string | symbol, ...args: unknown[]) => boolean)
    | undefined;
}

/**
 * Install simsys baseline metrics + per-request HTTP instrumentation for a
 * Next.js standalone server.
 *
 * Call from `instrumentation.ts` at the project root:
 *
 *   export async function register() {
 *     if (process.env.NEXT_RUNTIME !== "nodejs") return;
 *     const { installNext } = await import("@simsys/metrics");
 *     installNext({ service: "leadership", version: pkg.version });
 *   }
 *
 * Then mount `app/api/metrics/route.ts`:
 *
 *   export { GET } from "@simsys/metrics/next/route";
 *   export const dynamic = "force-dynamic";
 */
export function installNext(opts: NextInstallOpts): void {
  if (!opts || typeof opts !== "object") {
    throw new Error("installNext(): opts { service, version } required.");
  }
  if (!opts.service || typeof opts.service !== "string") {
    throw new Error("installNext(): opts.service must be a non-empty string.");
  }
  if (!opts.version || typeof opts.version !== "string") {
    throw new Error("installNext(): opts.version must be a non-empty string.");
  }

  // Idempotent guard. instrumentation.ts is called once per server start in
  // production; Next dev mode hot-reloads it, so the sentinel keeps repeated
  // calls from double-patching emit.
  if (globalThis.__simsysNextInstalled) {
    return;
  }

  const { service, version } = opts;
  const commit = opts.commit ?? detectCommit();
  const metricsPath = opts.metricsPath ?? "/api/metrics";
  const routeTemplates = opts.routeTemplates ?? [];
  const maxRoutes = opts.maxRoutes ?? DEFAULT_MAX_ROUTES;

  // Snapshot every piece of state install is about to mutate, so partial
  // failure rolls back cleanly. Mirrors Express adapter discipline.
  const preService = _peekService();
  // Resolve the TRUE original `emit` reference. On a fresh process,
  // http.Server.prototype.emit IS the Node built-in. On a hot-reload
  // (Next dev mode re-runs instrumentation.ts; the sentinel above
  // guards repeated calls in the same module-load cycle, but a
  // consumer may have explicitly cleared `__simsysNextInstalled` to
  // re-arm), the live `emit` could already be OUR previous patch.
  // Re-capturing it as origEmit and patching on top would stack two
  // layers of patch — every request's `finalize` would run twice and
  // double-count the counter (Codex F1 shape, but on http.Server).
  //
  // Defence: stash the true original on first install in
  // `globalThis.__simsysNextOrigEmit` and reuse it on subsequent
  // installs so the patch is always layered on the real Node built-in,
  // never on our own prior patch.
  const origEmit = (globalThis.__simsysNextOrigEmit ??
    http.Server.prototype.emit) as typeof http.Server.prototype.emit;
  let buildInfoLabels: BuildInfoLabels | null = null;
  let buildInfoWasNew = false;
  let procCollectorState: ProcessCollectorRollbackState | null = null;
  let emitPatched = false;

  try {
    setService(service);
    procCollectorState = registerProcessCollector(service);
    registerNodeDefaultMetrics(service);
    buildInfoLabels = {
      service,
      version,
      commit,
      started_at: startedAtNow(),
    };
    ({ wasNew: buildInfoWasNew } = registerBuildInfo(buildInfoLabels));

    const patchedEmit = function (
      this: http.Server,
      event: string | symbol,
      ...args: unknown[]
    ): boolean {
      if (event !== "request") {
        return origEmit.apply(this, [event, ...args] as Parameters<
          http.Server["emit"]
        >);
      }
      const req = args[0] as http.IncomingMessage | undefined;
      const res = args[1] as http.ServerResponse | undefined;
      if (!req || !res) {
        return origEmit.apply(this, [event, ...args] as Parameters<
          http.Server["emit"]
        >);
      }

      const url = req.url ?? "/";
      const qsIdx = url.indexOf("?");
      const pathOnly = qsIdx >= 0 ? url.slice(0, qsIdx) : url;
      if (pathOnly === metricsPath) {
        return origEmit.apply(this, [event, ...args] as Parameters<
          http.Server["emit"]
        >);
      }

      const start = process.hrtime.bigint();
      const finalize = () => {
        res.removeListener("finish", finalize);
        res.removeListener("close", finalize);
        const elapsed = Number(process.hrtime.bigint() - start) / 1e9;
        const method = normalizeMethod(req.method);
        const status = res.statusCode ?? 500;
        const route =
          status === 404
            ? UNMATCHED_ROUTE
            : capRoute(bucketRoute(url, routeTemplates), maxRoutes);
        try {
          httpRequestsTotal
            .labels({
              service,
              method,
              route,
              status: statusBucket(status),
            })
            .inc();
          httpRequestDurationSeconds
            .labels({ service, method, route })
            .observe(elapsed);
        } catch {
          /* defensive: bad labels must not crash request finalize */
        }
      };
      res.on("finish", finalize);
      res.on("close", finalize);

      return origEmit.apply(this, [event, ...args] as Parameters<
        http.Server["emit"]
      >);
    };
    // Mark our patch closure so future installs / introspection can
    // detect "is the live emit ours?" without relying on the sentinel
    // (which a consumer can clear to force re-arm).
    (patchedEmit as unknown as { __simsysNextEmitPatch: true }).__simsysNextEmitPatch = true;
    http.Server.prototype.emit = patchedEmit;
    emitPatched = true;

    // Sentinels set LAST — only after every wiring step succeeded.
    // Save the TRUE original emit on first install so subsequent
    // installs (hot-reload + sentinel-clear) layer their patch on
    // the real Node built-in, never on our prior patch. Only write
    // it if it's not already set — preserves the original capture
    // across multiple install/uninstall cycles.
    globalThis.__simsysNextInstalled = true;
    if (globalThis.__simsysNextOrigEmit === undefined) {
      globalThis.__simsysNextOrigEmit = origEmit as unknown as NonNullable<
        typeof globalThis.__simsysNextOrigEmit
      >;
    }
  } catch (err) {
    if (emitPatched) {
      try {
        http.Server.prototype.emit = origEmit;
      } catch {
        /* defensive */
      }
    }
    if (buildInfoLabels !== null) {
      try {
        unregisterBuildInfoIfOwned(buildInfoLabels, buildInfoWasNew);
      } catch {
        /* defensive */
      }
    }
    if (procCollectorState !== null) {
      try {
        restoreProcessCollector(procCollectorState);
      } catch {
        /* defensive */
      }
    }
    setService(preService);
    throw err;
  }
}
