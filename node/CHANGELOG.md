# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [node-v2.0.0] — 2026-08-25

### Changed — BREAKING: a failing poller no longer reports `0` (#50319)

A queue `depth_fn`/`depthFn` or pool stat callback that raises, rejects or
panics now leaves its gauge **untouched**. The last successful value stands,
and if the **first** tick fails the series is **absent** rather than `0`.

Seeding `0` was the defect: an empty queue and a broken callback are
operationally opposite, and only one of them ever gets investigated.

**Operational note.** If a consumer's callback has been failing silently, its
`simsys_queue_depth` / `simsys_pool_*` series will **disappear** on upgrade
rather than read `0`. That is the intended signal, not a regression. Alert on
the pair rather than the gauge alone:

```promql
rate(simsys_collector_errors_total[5m]) > 0
```

There were **six** poller paths carrying **four** policies, not the three
#50319 recorded:

| path | before 2.0.0 |
|---|---|
| Python `track_queue` | reported 0, warned once per tracker |
| Python `track_pool` | preserved previous values, but a tick could be PARTIAL |
| Node `trackQueue` | reported 0, **no warning at all** |
| Node `trackPool` | preserved previous values |
| Go `TrackQueue` | recovered, warned — then wrote **0 anyway** |
| Go `TrackPool` | preserved previous values |

Go's queue path had the identical defect the ticket attributed only to Python
and Node: the `Set` call sat *outside* the recovering closure, so every
panicking tick fell through to a confident zero.

All six now read every callback **before** writing any gauge, so a partial
tick is impossible — no more fresh `active` beside stale `idle`.

### Added — `simsys_collector_errors_total` (#50319)

`counter{service, collector, name}`, `collector` ∈ `{queue, pool}`.

Incremented once per failed poller tick. It is what makes the new flat line
legible: without it, "gauge stopped moving" and "gauge is genuinely steady"
look the same. Cardinality is (queues + pools) per service.

### Fixed

`node/README.md`'s failure-behaviour note now describes reality again. It had
claimed the gauge "stays at its last successful value or 0" — true of
`trackPool`, false of `trackQueue` — and #85 corrected it to document the
defect. As of 2.0.0 the original claim is true of both.

### Added

A `poller-failure-policy` test file. Before it this lane had **no test of
poller failure behaviour at all**: 138 tests passed both before and after
the behaviour change, so the suite's green carried no information about the
one thing this release alters.


## [1.0.0] — 2026-08-22

**All three packages align on 1.0.0 from this release. The version number is
now the CONTRACT version, and it means the same thing in every lane.**

Before this, the lanes drifted independently — Python 0.4.0, Node 0.5.0, Go
0.3.1 — so "which version am I on" had three different answers and none of
them told you which metric catalogue you were getting. Now `1.0.0` in any
lane means the same `spec/metrics-contract.json`: the same metric names,
types, label sets, enums and bucket schedules.

Aligned at the first registry publication deliberately. Version numbers on a
package index cannot be reused, so this is the one moment the renumbering is
free.

### Why 1.0.0 rather than another 0.x

The package has ~25 consumers in production, a machine-readable contract, and
cross-language conformance tests. 0.x signals "the shape may still move
arbitrarily", which stopped being true. 1.0.0 is the accurate signal, and it
makes the next breaking change say so in the version rather than hiding in a
minor bump.

**The pending poller-behaviour change (#50319) and the GC label-schema fix
(#50345) are therefore 2.0.0, not 1.1.0.** Both change an emitted series, and
under semver that is a major.

### Added

- **Published to npm.** `npm install @simsys/metrics`
- `spec/metrics-contract.json` — machine-readable catalogue, with per-lane
  conformance tests asserting against a live registry.
- Shipped Grafana dashboard and Prometheus alert pack under `dashboards/`.

### Changed

- No behavioural change from the previous release in this lane. This is a
  renumbering plus packaging; every emitted series is byte-identical.

## [0.5.0] — 2026-07-05 — Node only

### Changed

- **Engines floor raised: `node>=18` → `node>=20`.** Node 18 has been
  end-of-life since April 2025, and the dev toolchain (vitest@4) already
  required Node 20+ to run the test suite. With the floor at 20, CI's
  Node 20 matrix lane exercises the floor directly, so the build-only
  `build-node18` workflow job was removed. Consumers still on Node 18
  should stay on `0.4.x`. No API or runtime behavior changes.

## [0.4.4] — 2026-06-10 (`node-v0.4.4`)

Route cardinality hardening for the Next.js adapter (infra#37576), plus
the `trackPool()` helper and a CJS dual-build that landed in the same
release window. (Entry backfilled 2026-07-30 from the
`node-v0.4.3..node-v0.4.4` diff — release notes were skipped at tag
time; Redmine #47681.)

### Added

- **`trackPool()` opt-in helper for connection pool monitoring.** Polls
  consumer-supplied `activeFn`/`idleFn` (and optional `waitingFn`)
  callbacks on an interval (default 5000 ms) and sets the
  `simsys_pool_active` / `simsys_pool_idle` / `simsys_pool_waiting` /
  `simsys_pool_max` gauges, labelled `{service, pool}`. The interval
  timer is `unref()`ed so it never holds the process open; callback
  errors are swallowed rather than crashing the poller; non-finite or
  non-positive `intervalMs` throws at call time. Exported from the
  package root alongside `TrackPoolOpts`.
- **CJS dual-build.** The package now ships `dist/cjs/` alongside the
  ESM build: `main` points at `dist/cjs/index.js` and the `exports` map
  gains `require` conditions for `.` and `./next/route`, so
  `require("@simsys/metrics")` works from CommonJS consumers. Built via
  a second `tsc -p tsconfig.cjs.json` pass (`module: Node16`) with a
  `{"type": "commonjs"}` stub emitted into `dist/cjs/`.

### Fixed

- **Route-label cardinality explosion on public-facing Next.js apps**
  (infra#37576). Internet-scanner wordlist paths (`/wp-login.php`,
  `/abantecart/index.php`, …) are lowercase-wordish, which the
  `bucketRoute()` allow-list kept verbatim — ~8k distinct route labels
  and a 14 MB scrape body on bfr-leadership in 10 days. Two new layers
  on top of `bucketRoute()`:
  - **404 collapse**: responses with status 404 record
    `route="/__unmatched__"`, mirroring the Express adapter's
    router-miss label. Real-route 4xx (401/403 auth denials) keep their
    bucketed label.
  - **Distinct-route cap**: a per-process cap on minted route labels
    (new `maxRoutes` install option, default 300); overflow records as
    `/__overflow__`. The seen-set is pinned to `globalThis` per the
    0.4.2 chunk-split-safety pattern so every webpack chunk copy spends
    from the same budget. Sentinel labels can't be spoofed —
    attacker-supplied `/__overflow__` buckets to `/:str`.

  Five regression tests cover scanner wordlists, auth-denial fidelity,
  cap overflow, seen-route continuity, and sentinel impersonation
  (`tests/route-cardinality.test.ts`).

### Changed

- Dev-dependency floors bumped (dev-only, no runtime effect):
  `typescript` ^6.0.3, `vitest` ^4.1.8, `hono` ^4.12.23, `@types/node`
  ^25.9.2.

## [0.4.3] — 2026-04-29 (`node-v0.4.3`)

### Changed

- Repository transferred from `Avicennasis/simsys-metrics` to
  `Simmons-Systems/simsys-metrics`. GitHub redirects keep old tarball
  URLs working; new owner is canonical. `repository.url`, `homepage`,
  `bugs.url`, README badges, and install snippets all updated to the
  new owner. No functional changes.

## [0.4.2] — 2026-04-27 — Node only

Patch release fixing the Phase 3 Next.js empty-`/metrics`-body bug. Closes
the gap that v0.4.0 introduced and v0.4.1 didn't address.

### Fixed
- **F25 — Next.js `/metrics` endpoint returned HELP/TYPE only, no
  samples** (HIGH). Webpack standalone bundling inlines this package
  into multiple server chunks (e.g. `instrumentation.ts` and
  `app/api/metrics/route.ts` each get their own copy of `registry.ts`).
  Each chunk's module-instance constructed its own `new Registry()` and
  its own `Counter`/`Gauge`/`Histogram` instances at module top-level,
  so `installNext()` wrote samples to one registry while the route
  handler's `registry.metrics()` read from another. The metric
  *definitions* (HELP/TYPE) appeared in the response because the
  registration side-effect runs in every chunk's module-load; the
  *samples* didn't because the `set()`/`inc()` calls all targeted the
  instrumentation chunk's registry. `simsys_build_info` was therefore
  also missing in Prometheus, even though Next's `up=1` made the scrape
  look healthy.

  Fix: every stateful singleton — the `Registry`, all baseline metric
  instances, the default-metrics-registered flag, the missing-`service`
  warning Set, the `_service` and queue-timer state in `baseline.ts`,
  the `_ownedLabelKeys` Set in `buildinfo.ts`, and the
  `registered`/`service`/metric refs/`lastCpuSeconds`/`cpuCollectMutex`
  state in `process.ts` — is now pinned to `globalThis` via `??=`. A
  second module-instance import (whatever caused the duplication)
  short-circuits state construction and re-exports the singletons stored
  by the first load. Identity-stable references regardless of bundler
  chunking. Belt-and-suspenders alongside Next's
  `serverExternalPackages: ["@simsys/metrics", "prom-client"]` config.

  New regression test `tests/registry-chunk-split.test.ts` verifies that
  `vi.resetModules()` followed by re-import yields the same `registry`,
  `buildInfo`, `httpRequestsTotal` instances; that baseline `_service`
  state survives reloads; that `_ownedLabelKeys` ownership tracking is
  shared; and that samples written via the first import appear in
  `metrics()` output read via the second.

### Verified
- All four BFR Next.js apps (leadership, line-portal, board-portal,
  roster) had previously been unblocked by adding `@simsys/metrics` and
  `prom-client` to each `next.config.js`'s `serverExternalPackages`.
  This release lets future Next.js consumers skip that step (though it
  remains the recommended best practice).

## [0.4.1] — 2026-04-26 — Node only

Patch release closing six self-audit findings on top of v0.4.0. Three
parallel reviews (focused review of the new Next adapter, regression
check that v0.3.10 fixes still hold, security/cardinality audit of
`bucketRoute` against attacker-controlled URLs) found four HIGH issues
introduced or exposed by v0.4.0 plus two MED issues. Python and Go are
unaffected (no changes to those packages).

### Fixed
- **F17 — Hot-reload + sentinel-clear no longer double-patches
  `http.Server.prototype.emit`** (HIGH). Pre-fix, when a consumer
  cleared `globalThis.__simsysNextInstalled` to force a re-arm
  without ALSO restoring `http.Server.prototype.emit`, the second
  `installNext()` call captured the LIVE `emit` (which was our
  prior patch) as `origEmit` and stacked a second patch on top.
  Every request's `finalize` then ran twice, doubling
  `simsys_http_requests_total`. Fix: cache the TRUE original emit
  in `globalThis.__simsysNextOrigEmit` on first install and reuse
  it on subsequent installs so the patch always layers on the Node
  built-in, never on our own prior patch. The patch closure is
  also marked with a `__simsysNextEmitPatch = true` property for
  introspection.
- **F18 — `globalThis.__simsysNextOrigEmit` is now wired up**
  (HIGH). Pre-fix, the global was written but never read — dead
  state on `globalThis`. The F17 fix above puts it to use as the
  durable record of the true Node built-in `emit`.
- **F19 — `registerNodeDefaultMetrics` now refreshes
  `setDefaultLabels` on every call** (HIGH). Pre-fix, the
  process-global `defaultMetricsRegistered` boolean short-
  circuited on second-and-later calls, including the
  `registry.setDefaultLabels({service})` call that controls the
  static `service` label on prom-client default metrics
  (`process_*`, `nodejs_*`). After a hot-reload to a new service
  name, those default metrics kept the original service label
  forever. Now `setDefaultLabels` is called every time;
  `collectDefaultMetrics` is still gated by the boolean (registers
  exactly once).
- **F20 / F21 — `bucketRoute` cardinality and percent-encoding
  hardening** (HIGH, security). Pre-fix, ANY non-numeric and
  non-UUID segment was passed through verbatim. An attacker
  spraying `/api/foo/abc1`, `/api/foo/abc2`, ... or
  `/api/data/%41`, `/api/data/%42`, ... could create one
  Prometheus time series per unique segment, exhausting Prometheus
  memory. The README explicitly claimed cardinality was bounded;
  it wasn't. Fix:
  - Segments are percent-decoded BEFORE classification, so `/%41`
    and `/A` produce the same label.
  - Only segments matching the safe-text-segment shape (lowercase
    ASCII, dot, hyphen, ≤ 32 chars, no digits) pass through
    verbatim. Anything else (mixed alphanumeric, slug IDs like
    `ORD-9981`, base64, JWTs, percent-encoded non-ASCII bytes,
    Unicode) collapses to `:str`.
  - `routeTemplates` still wins for high-fidelity overrides on
    legitimate dynamic paths (e.g. usernames in
    `/api/users/<u>/profile`).
  - Residual: pure-lowercase slugs of typical word shape (e.g. an
    attacker-controllable username field) still pass through —
    consumers MUST use `routeTemplates` for those if they're
    user-controlled. Documented in JSDoc.
- **F22 — `RouteTemplate.pattern` ReDoS warning in JSDoc** (LOW).
  Catastrophic-backtracking patterns block the event loop on the
  hot path; consumers must avoid them. Documentation only.
- **F24 — `bucketRoute` short-circuits paths > 8KB to
  `/__toolong__`** (LOW). Defensive cap against pathological
  input.

### Changed
- `bucketRoute(url, templates)` label format change. Mixed
  alphanumeric / slug / percent-decoded-non-ASCII segments that
  used to pass through verbatim now collapse to `:str`. Grafana
  dashboards filtering on raw slug labels need to switch to
  `:str` or use `routeTemplates` to preserve the prior label.
  This is a meaningful security improvement (cardinality is now
  bounded against attacker URL shapes), but it IS a label-format
  change visible to dashboards, so it bumps the package patch
  version with an explicit CHANGELOG note rather than being a
  silent fix.
- `installNext()` patch closure now carries a
  `__simsysNextEmitPatch` marker property for introspection.
- `registry.setDefaultLabels({service})` is now called on every
  `registerNodeDefaultMetrics` invocation, not just the first.

### Tests
- 6 new vitest cases in
  `tests/install-self-audit-v041.test.ts` (F17 hot-reload
  double-patch, F19 default-labels refresh on swap, F20 slug
  collapse, F21 percent-decode, F24 length cap, F20 backwards-
  compat regression guard).
- All 69 Node tests pass; tsc clean; `npm pack --dry-run` 43
  files; `npm audit` 0 vulns; conformance smoke green.

## [0.4.0] — 2026-04-26

Minor release: Next.js standalone adapter. Same simsys baseline + per-request
HTTP instrumentation as the Express + Hono adapters, suitable for the BFR
internal-app cohort and any other Next.js consumer. No breaking changes;
Express + Hono adapters untouched.

### Added
- **`installNext(opts)`** — Next.js adapter for App Router / standalone
  builds. Patches `http.Server.prototype.emit` to capture every request
  finish (the same trick OpenTelemetry's Next instrumentation uses) so
  every authenticated route, RSC stream, and API handler is recorded
  through one mechanism. Registers the simsys baseline on call:
  `setService`, `registerProcessCollector`, `registerNodeDefaultMetrics`,
  `registerBuildInfo`. Idempotent via a process-global sentinel that
  survives Next dev-mode hot-reload (resetting the sentinel re-arms a
  clean install).
- **`@simsys/metrics/next/route`** — drop-in `GET` handler for
  `app/api/metrics/route.ts`, mountable as `export { GET } from
  "@simsys/metrics/next/route";`. New `package.json` `exports` subpath.
- **`bucketRoute(url, templates?)`** — pure function exported for
  consumer introspection / unit tests. Strips query strings + hash,
  collapses numeric segments to `:id`, UUID segments to `:uuid`, and
  paths > 5 segments to `/<a>/<b>/__deep__`. Accepts an optional
  `routeTemplates: Array<{pattern: RegExp, template: string}>` for
  high-fidelity overrides — templates win over default bucketing.

### Tests
- 20 vitest cases in `tests/next.test.ts` covering: build_info
  registration, HTTP request recording on a vanilla `http.Server`,
  metrics-path exemption (`/api/metrics` not recorded), numeric-id +
  UUID + deep-path bucketing, query-string stripping, custom
  `routeTemplates` precedence, idempotency, status / method
  normalization, hot-reload simulation, opts validation, route handler
  text-format response.

### Notes
- The adapter targets BFR's 4 Next.js standalone apps (leadership,
  line-portal, board-portal, roster) plus any other Next.js consumer
  on the fleet. Roster uses `next-auth` instead of Firebase — the
  baseline metric coverage is identical regardless.

## [0.3.10] — 2026-04-26

Patch release closing four Node-side findings from a v0.3.9 self-audit:

### Fixed
- **F8 / F11 — Service-swap no longer creates a Prometheus
  counter-reset artifact** (HIGH/MED). v0.3.9's swap path called
  `cpuTotal.reset()` (zeroing every labelset, observed by Prometheus
  as a counter reset) and zeroed `lastCpuSeconds` (making the next
  collect inc by full process-cumulative CPU — visible counter
  spike). Replaced with `metric.remove({service: priorService})`
  which drops ONLY the swapped-out labelset and leaves the live
  service's accumulator monotonic. `lastCpuSeconds` is now
  preserved so the new service's first collect inc by a small
  delta-since-prior-collect. The rollback path uses the same
  per-labelset remove for symmetry.
- **F9 — Hono `app.basePath()` now respected** (MED). Pre-fix, the
  metrics-path exemption compared `c.req.path === livePath` where
  `livePath` was the install-time `metricsPath` argument
  ("/metrics"). With `new Hono().basePath("/api")`, requests
  arrive at `/api/metrics` and the equality failed — `/metrics`
  scrapes were counted as instrumented requests. Fix reads
  `app._basePath` at install time and stores the absolute URL
  ("/api/metrics") in `appProps.simsysMetricsPath`; the runtime
  exemption check now matches Hono's actual routing.
- **F12 — Idempotency warning includes `metricsPath` mismatch**
  (LOW). Pre-fix, second-install with a different metricsPath but
  same service/version was a silent no-op (route is stuck at the
  first install's path). The warning now compares all three.

### Changed
- `registerProcessCollector(svc)` swap path now uses
  `metric.remove({service: priorService, ...})` instead of
  `metric.reset()`. No counter-reset artifact in Prometheus.
- `installHono(app, opts)` now reads `app._basePath` (Hono
  internal) at install time and stores the absolute metrics URL.
  No API change for callers — the published interface is
  unchanged.

### Tests
- 4 new vitest cases in `tests/install-self-audit-v0310.test.ts`
  (F8/F11 counter monotonicity across swap, F9 basePath
  exemption, F12 metricsPath-mismatch warning, F12
  silent-when-all-match).

## [0.3.9] — 2026-04-26

Patch release closing four Node-side findings from a follow-up Codex
audit (one MED/HIGH, three MED). All four regressed pre-fix on Codex's
exact reproductions; v0.3.9 ships regression coverage for each in
`tests/install-codex-findings.test.ts`.

### Fixed
- **F1 — Hono partial install double-stacks middleware on retry**
  (MED/HIGH). Pre-fix, when `app.get(metricsPath, ...)` threw during
  install, rollback cleared app props but left the wildcard middleware
  (`app.use("*", ...)`) wired. A retry then added a SECOND middleware
  on top, so every subsequent request was counted twice (Codex repro:
  `simsys_http_requests_total{route="/hello"} = 2` after fail-then-retry).
  Hono offers no public API to remove a previously-registered route or
  middleware, so the fix wires both AT MOST ONCE per app and gates them
  on the live `simsysMetricsInstalled` flag. A failed install leaves
  the wiring in place but INERT — the recording middleware short-
  circuits and the `/metrics` route returns 404 — until a retry sets
  `simsysMetricsInstalled = true` again. Handlers also read service /
  metrics-path from `appProps` at request time, so a retry with
  same-shape values (the only retry case the idempotent guard
  permits) re-arms cleanly without stale closure references.
- **F2 — Express 5 rollback snapshots the wrong router stack** (MED).
  Express 5 exposes the router at `app.router`, not `app._router`.
  Pre-fix snapshot read `app._router?.stack?.length` (always undefined
  in Express 5) so rollback no-opped — leaving a `/metrics` route layer
  in the stack on failure. Codex repro: stack `["/metrics"]` after
  fail, `["/metrics", "/metrics", "mw"]` after retry. Fix prefers
  `app.router.stack` and falls back to `app._router.stack` so both
  Express 4 (lazy `_router`) and Express 5 (`router`) truncate cleanly.
- **F3 — `build_info` rollback could delete a pre-existing sample it
  did not own** (MED). `started_at` is second-precision, so two
  installs of the same `service+version+commit` started in the same
  wall-clock second produce identical labelsets. Pre-fix, install B's
  rollback unconditionally called `buildInfo.remove(labels)` and
  silently deleted install A's still-live sample. Fix: new
  `registerBuildInfo(labels)` helper returns `{labels, wasNew}` tracked
  via a module-scoped ownership set; rollback uses
  `unregisterBuildInfoIfOwned(labels, wasNew)` which is a no-op when
  `wasNew = false`.
- **F4 — Process-collector rollback did not restore the prior
  service** (MED). Pre-fix, `registerProcessCollector(svc)` mutated
  the static `service` global in place during a swap; adapter rollback
  only restored the baseline `_SERVICE` global, leaving the process
  collector tagging samples with the failed install's service. Fix:
  `registerProcessCollector` now returns a `ProcessCollectorRollbackState`
  capturing the action (`reused` / `registered` / `service-swap`) plus
  the prior service label; rollback's new `restoreProcessCollector(state)`
  flips the label back. The swap path also `.reset()`s every collector
  metric so the swapped-out service's stale gauge samples don't linger
  between scrapes — prom-client otherwise retains per-labelset entries
  indefinitely after the labels stop being written.

### Changed
- `registerProcessCollector(svc)` now returns
  `ProcessCollectorRollbackState` (was `void`). Adapter install code
  pairs it with the new `restoreProcessCollector(state)` to undo
  cleanly on partial install.
- New helpers: `registerBuildInfo(labels)`,
  `unregisterBuildInfoIfOwned(labels, wasNew)`,
  `_resetBuildInfoOwnershipForTests()` exported from `buildinfo.ts`.

### Tests
- 4 new vitest cases in `tests/install-codex-findings.test.ts`
  reproducing each Codex finding pre-fix, then verifying the fix.
- All 38 tests pass; `tsc` clean; `npm pack --dry-run` 43 files;
  `npm audit --omit=dev --audit-level=moderate` zero vulns.

## [0.3.8] — 2026-04-25

### Fixed
- **install() partial-failure rollback** (MED). Pre-fix, the sentinel
  `app.locals.simsysMetricsInstalled` (Express) /
  `app.simsysMetricsInstalled` (Hono) was set BEFORE the framework
  wiring (`app.get(metricsPath, ...)`, `app.use(...)`). If any of
  those threw, the sentinel stayed `true` and a retry no-op'd via
  the idempotent guard — leaving `/metrics` and the HTTP middleware
  unwired. The sentinel is now set LAST, inside a try/catch that
  also drops the `simsys_build_info` label-set, restores the
  pre-install service global, and clears the discovery
  attributes (`simsysExemptPaths` / `simsysService` /
  `simsysVersion`) on failure. Backed by
  `node/tests/install-partial-failure.test.ts` (Express + Hono).

## [0.3.7] — 2026-04-25

### Fixed
- **`trackProgress` rejects NaN / Infinity / -Infinity intervals**
  (MED). The previous `intervalMs <= 0` check let `NaN` through
  vacuously, and `setInterval(fn, Infinity)` was clamped by Node to
  a 1ms hot loop with `TimeoutOverflowWarning`. Both windowMs and
  intervalMs now use `Number.isFinite` checks.

### Changed
- `makeCounter` / `makeGauge` / `makeHistogram` now `console.warn`
  (don't throw) at registration time when `'service'` is missing
  from `labelNames`. Once-per-metric-name. Custom metrics without a
  `service` label can't participate in cross-service `$service`
  Grafana dashboards; the warning makes the omission visible
  instead of letting the metric silently break the contract.
  Backed by `node/tests/registry-service-warning.test.ts`.

## [0.3.6] — 2026-04-25

### Fixed
- **HTTP method label allow-list** (HIGH). `req.method` (Express) and
  `c.req.method` (Hono) were passed straight into the
  `simsys_http_requests_total` label, so a hostile client sending
  arbitrary methods (`X_AUDIT_1`, `BREW`, `MKCALENDAR`, …) forced one
  new series per distinct value. Both adapters now run the method
  through `normalizeMethod()` which collapses anything outside the RFC
  9110 + PATCH allow-list to `"OTHER"`. Backed by
  `node/tests/method-normalization.test.ts`.
- **`trackQueue` rejects non-positive `intervalMs`** (MED). Pre-fix,
  `intervalMs: 0` created a hot `setInterval` loop that pegged the
  event loop. Now throws at call time with a clear error.

### Changed
- Auth-exempt path set returned by `app.locals.simsysExemptPaths`
  (Express) / `app.simsysExemptPaths` (Hono) now includes the
  user-supplied `metricsPath` — previously the static set with
  `/metrics` was always returned regardless of what `install()` was
  called with.

## [0.3.5] — 2026-04-25

### Fixed
- **CPU counter concurrent-scrape race.** prom-client's `Registry.metrics()`
  walks collectors via `Promise.all` with no per-registry mutex, so two
  concurrent /metrics scrapes (e.g. Prometheus + a sidecar push monitor)
  could both observe the same `lastCpuSeconds` value and inc the
  counter by ~2× the actual CPU delta. The cpu collect callback now
  serializes via a module-scoped `cpuCollectMutex: Promise<void>` chain
  so only one read-update-inc sequence runs at a time. Backed by
  `node/tests/cpu-counter-race.test.ts` (16 parallel scrapes assert
  monotonic + bounded counter values).

### Documentation
- README install snippet bumped from `node-v0.3.4` to `node-v0.3.5`.
- README metric catalogue now lists the four `simsys_progress_*` rows
  (was missing despite being defined in code).
- README gained a "Cross-runtime caveat" section explaining that
  `simsys_process_memory_bytes.type` label values are runtime-specific
  (`rss`/`heapUsed`/`heapTotal`/`external` in Node vs `rss`/`vms` in
  Python/Go). The `rss` value is the only common label across all
  three sibling packages.
- CHANGELOG heading separators standardized on em-dash to match the
  root file (was hyphen).

## [0.3.4] — 2026-04-25

### Documentation
- README install snippet bumped from `node-v0.3.3` to `node-v0.3.4`.

No code changes; version bump keeps the three sibling packages on a
matched semver line. See the [root CHANGELOG](../CHANGELOG.md) for the
v0.3.4 documentation cleanup that prompted this release.

## [0.3.3] — 2026-04-25

### Fixed
- **`install()` is now idempotent.** A second call on the same Express
  or Hono app previously added a SECOND middleware AND a SECOND
  `/metrics` route, causing every request to be counted twice on the
  second-install (and N+1 times on the Nth install). The new behaviour
  matches the Python and Go siblings: a sentinel on `app.locals`
  (Express) / app instance (Hono) short-circuits subsequent calls,
  with a `console.warn` when service/version differs from the
  original. Backed by `node/tests/install-idempotent.test.ts`.

### Tooling
- New CI workflow `.github/workflows/test-node.yml` runs `npm ci +
  build + vitest + npm pack --dry-run` on Node 20 / 22 / 24 (vitest@4
  requires Node ≥ 20). A separate `build-node18` job verifies the
  TypeScript build still succeeds on the declared `engines: node>=18`
  floor.

### Packaging
- `node/LICENSE` is now a real file (was previously listed in
  `package.json#files` but missing from the directory, so it was
  silently dropped from the published tarball).
- `node/package-lock.json` regenerated; was stuck reporting
  `version: 0.1.0` despite `package.json` having advanced through
  several releases. `npm ci` now succeeds against the lockfile.
- README install snippet bumped to `node-v0.3.3` tarball URL.

## [0.3.2] — 2026-04-25

### Documentation
- README install snippet bumped to the v0.3.2 tarball URL (was stale
  `node-v0.1.0`).

No code changes in the Node package; version bumped to keep the three
sibling packages on a matched semver line. See the
[root CHANGELOG](../CHANGELOG.md) for v0.3.2 fixes (Python-only).

## [0.3.1] — 2026-04-25

No code changes in the Node package — version bumped to keep all three
sibling packages on a matched semver line. See the
[root CHANGELOG](../CHANGELOG.md) for the v0.3.1 fixes (Python-only).

The Development section of `node/README.md` was rewritten to reference
the unified monorepo layout (was a stale pre-merge URL).

## [0.3.0] — 2026-04-25

First public release of the Node sibling package. See the
[root CHANGELOG](../CHANGELOG.md) for the full feature set; Node-specific
notes:

- Express 5 + Hono 4 adapters share a single metric catalogue.
- `process.cpuUsage()` is exposed as a monotonic counter using delta-based
  increments — preserves prom-client's reset-detection (no reset()+inc()
  pattern that would break `rate()` over scrape boundaries).
- Hono adapter detects the wildcard middleware path (`/*`) as unmatched so
  unrouted requests collapse to `route="__unmatched__"` rather than leaking
  the wildcard pattern.
- ESM-only, requires Node ≥ 18 (declared via `engines`).
- Tarball distribution via GitHub Releases; install URL is the
  `simsys-metrics-0.3.0.tgz` asset attached to the `node-v0.3.0` release.

[0.3.8]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/node-v0.3.8
[0.3.7]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/node-v0.3.7
[0.3.6]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/node-v0.3.6
[0.3.5]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/node-v0.3.5
[0.3.4]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/node-v0.3.4
[0.3.3]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/node-v0.3.3
[0.3.2]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/node-v0.3.2
[0.3.1]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/node-v0.3.1
[0.3.0]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/node-v0.3.0
