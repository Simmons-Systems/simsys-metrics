# Changelog

All notable changes to `simsys-metrics` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Per-language detail for the Node package lives in
[`node/CHANGELOG.md`](node/CHANGELOG.md); entries here are summaries.

## [Unreleased]

Nothing yet.

## [python-v2.0.0] — 2026-08-25

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

### Changed — BREAKING: `simsys_runtime_gc_collections_total` renamed on Python (#50345)

Python now emits **`simsys_runtime_gc_collections_by_generation_total`**
`{service, generation}`. The old name is retained by **Go only**, for
`runtime.MemStats.NumGC`.

This was the only metric in the catalogue whose label *names* differed by
runtime. A panel with `by (generation)` returned empty for Go services, and
`sum()` across lanes compared two different quantities.

Split rather than unified, deliberately. Making the label sets match would not
have made the numbers comparable — it would only have made them look it. A
CPython total is dominated by gen0 (a live service at the time of the change
read gen0=854, gen1=77, gen2=2) against a Go cycle count in the low single
digits. One name now means one thing.

The rename cost nothing to verify: **no dashboard panel or alert rule
referenced the old name**, in either `dashboards/` or the ServerOps
`simsys_metrics_scrape` pack. Per-generation detail is kept, under the new
name.

**Action required** for anything querying `simsys_runtime_gc_collections_total`
against a Python service: switch to the `_by_generation_` name.

### Changed

Contract version is now `2.0.0`, and all three lanes move together.
`spec/metrics-contract.json` carries **zero** tracked divergences for the
first time (`expected_count` 1 → 0): #50345 is resolved, and
`behaviors.poller_failure_policy` flips from `divergent` to `specified`.


## [python-v1.0.0] — 2026-08-22

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

- **Published to PyPI.** `pip install simsys-metrics`
- `spec/metrics-contract.json` — machine-readable catalogue, with per-lane
  conformance tests asserting against a live registry.
- Shipped Grafana dashboard and Prometheus alert pack under `dashboards/`.

### Changed

- No behavioural change from the previous release in this lane. This is a
  renumbering plus packaging; every emitted series is byte-identical.

## [python-v0.4.0] — 2026-08-05 — Python only

- **HTTP latency histogram buckets now extend past 10s** —
  `0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 30.0, 60.0`.
  Minor rather than patch because it changes emitted metrics: dashboards and
  recording rules that assume the old `le` set will see three new series per
  label combination (~+25% for this metric family). See the `[Unreleased]`
  entry above for the full rationale and the mixed-version rollout hazard.
- `track_queue()`/`track_pool()` now return a `PollerThread` with a
  cooperative `.stop()` (fixes daemon-thread leak on orphaned trackers), and
  poll loops self-terminate once the interpreter begins finalizing (no more
  user callbacks after shutdown). Return type is a `threading.Thread`
  subclass — fully backward compatible.

## [0.5.0] — 2026-07-05 — Node only

- Engines floor raised `node>=18` → `node>=20` (Node 18 EOL). The
  `build-node18` CI job was removed — the Node 20 matrix lane exercises
  the floor. No API or runtime behavior changes. Tag `node-v0.5.0`.

## [0.4.4] — 2026-06-10 — Node only

- Route cardinality hardening release: the Next.js adapter collapses
  404s to `/__unmatched__` and caps distinct route labels per process
  (`maxRoutes` install option, default 300; overflow →
  `/__overflow__`) (infra#37576). Also ships the `trackPool()`
  connection-pool gauges (`simsys_pool_active/idle/waiting/max`) and a
  CJS dual-build (`require("@simsys/metrics")` now works). Tag
  `node-v0.4.4`; see [`node/CHANGELOG.md`](node/CHANGELOG.md) for
  detail.

## [0.3.11] — 2026-04-29 — Python only

- Introduces the `python-vX.Y.Z` tag convention (first tag
  `python-v0.3.11`); `__version__` synced to `0.3.11`. No functional
  changes.

## 2026-04-29 — Repository transferred to `Simmons-Systems` org

Coordinated migration release across all three languages. No functional
changes; metadata-only.

- **Python** — `python-v0.3.11`. Introduces the `python-vX.Y.Z` tag
  convention (parallels `node-vX.Y.Z` and `go/vX.Y.Z`). Last unified
  `vX.Y.Z` tag was `v0.3.10`; consumers can keep using legacy tags via
  the redirect or migrate to the new convention.
- **Node** — `node-v0.4.3`. Tarball URL points at new owner.
- **Go** — `go/v0.2.11`. **Module path renamed**:
  `github.com/Avicennasis/simsys-metrics/go` →
  `github.com/Simmons-Systems/simsys-metrics/go`. Consumers must
  update import paths AND `go.mod` `require` lines. The module path is
  part of Go's public API; the rename is a hard break for any consumer
  that doesn't update.
- `pyproject.toml`, `node/package.json`, `go/go.mod`, `go/doc.go`, all
  three `README.md` files, badges, and install snippets now reference
  `Simmons-Systems`.
- `CODEOWNERS` left as `@Avicennasis` — handle is still valid.
- Repository transferred via GitHub native Transfer Ownership; the
  `Avicennasis/simsys-metrics` URL redirects indefinitely (until/unless
  someone re-creates the old name).

## [0.4.3] — 2026-04-29 — Node only

- Post-transfer metadata release (tarball URL points at the new owner).
  Tag `node-v0.4.3`; see the transfer entry above.

## [0.4.2] — 2026-04-27 — Node only

- Next.js empty-`/metrics`-body fix (registry singletons pinned to
  `globalThis` against webpack chunk splitting). Tag `node-v0.4.2`; see
  [`node/CHANGELOG.md`](node/CHANGELOG.md) for the full analysis.

## [0.4.1] — 2026-04-26 — Node only

Patch release to the Node package. Python + Go versions unchanged.

### Fixed (Node)
- **`@simsys/metrics` 0.4.1** — Six self-audit findings on top of
  v0.4.0's Next.js adapter. Highlights:
  - **F17 / F18** (HIGH) — hot-reload + sentinel-clear no longer
    double-patches `http.Server.prototype.emit` (request counter
    would double on the second install). Now caches the true
    original emit in `globalThis.__simsysNextOrigEmit` and reuses
    it across re-arm cycles.
  - **F19** (HIGH) — `registerNodeDefaultMetrics` refreshes
    `setDefaultLabels` on every call so default `process_*` /
    `nodejs_*` metrics carry the new service label after a hot-
    reload.
  - **F20 / F21** (HIGH security) — `bucketRoute` cardinality is
    now actually bounded against attacker URL shapes. Mixed-
    alphanumeric / slug / percent-decoded-non-ASCII segments
    collapse to `:str` (was: passed through verbatim, allowing
    `/api/foo/abc1`, `/api/foo/abc2`, ... to spray unbounded
    Prometheus time series). Percent-decoding before
    classification ensures `/%41` and `/A` produce the same
    label. `routeTemplates` still wins for legitimate dynamic
    paths.
  - **F24** (LOW) — `bucketRoute` paths > 8KB short-circuit to
    `/__toolong__`.

  See `node/CHANGELOG.md` for the full entry. Note: F20/F21 is a
  label-format change for mixed-alphanumeric segments — Grafana
  dashboards using raw slug labels need to update to `:str` or
  use `routeTemplates` to preserve the prior label.

## [0.4.0] — 2026-04-26 — Node only

Minor release to the Node package. Python + Go versions unchanged.

### Added (Node)
- **`@simsys/metrics` 0.4.0** — Next.js standalone adapter
  (`installNext()`) plus `@simsys/metrics/next/route` GET handler.
  Same baseline + per-request HTTP instrumentation as the existing
  Express + Hono adapters; targets the BFR Next.js cohort
  (leadership / line-portal / board-portal / roster) and any other
  Next.js consumer on the fleet. Cardinality-bounded route
  bucketing (`:id` / `:uuid` / `__deep__`) plus opt-in
  `routeTemplates` for high-fidelity overrides. See
  `node/CHANGELOG.md` for the full entry.

## [0.3.10] — 2026-04-26

Patch release closing seven self-audit findings on top of v0.3.9. The
audit was run by three parallel reviews (focused diff review,
concurrency hunt, cross-implementation consistency check) and surfaced
both bugs introduced by v0.3.9's ownership/rollback redesign and
latent issues that v0.3.9 didn't cover.

### Fixed
- **F6 — Python `build_info` ownership lock too narrow** (HIGH).
  `register_build_info` released `_owned_label_keys_lock` between the
  set mutation and the `build_info.labels(...).set(1)` call. A
  concurrent `unregister_build_info_if_owned` for the same labels
  could discard the key + remove the gauge sample in that window;
  the in-flight register's eventual `.set(1)` would then create an
  unowned sample whose subsequent rollback would silently delete the
  next install's owned sample. Fix holds the lock across both the
  set update AND the gauge mutation in `register_build_info`,
  `unregister_build_info`, and `unregister_build_info_if_owned`.
- **F7 — Python `restore_process_collector` no longer clobbers a
  concurrent install's collector** (HIGH). `restore_process_collector`
  unconditionally dropped or swapped `_collector` regardless of
  whether the live collector was still the instance THIS install
  had registered. A concurrent install that swapped the singleton
  mid-rollback would have its collector silently unregistered. Fix
  records `installed_collector` in `ProcessCollectorRollbackState`
  and only mutates the live `_collector` if `_collector is
  state.installed_collector` — otherwise rollback no-ops on the
  live collector (the concurrent install owns it now).
- **F8 / F11 — Node process-collector swap no longer creates a
  Prometheus counter-reset artifact and no longer races with
  in-flight scrapes** (HIGH/MED, Node). v0.3.9's swap path called
  `cpuTotal.reset()` and zeroed `lastCpuSeconds` synchronously.
  The reset zeroed every labelset (Prometheus reads it as a counter
  reset and gaps `rate()`), and the lastCpuSeconds=0 mutation made
  the next collect inc by the full process-cumulative CPU (visible
  spike). Replaced with `metric.remove({service: priorService})`
  which drops only the swapped-out labelset, and `lastCpuSeconds`
  is now preserved so the new service's collect() inc()s by a
  small delta. The same shape applies to the rollback path. Net
  effect: Prometheus sees A's series end (stale) and B's series
  start fresh — no spike, no reset.
- **F9 — Hono adapter now respects `app.basePath()`** (MED, Node).
  Pre-fix, the metrics-path exemption compared `c.req.path ===
  livePath` where `livePath` was the install-time `metricsPath`
  argument. With `new Hono().basePath("/api")`, requests arrive at
  `/api/metrics` and the exemption failed — the metrics scrape got
  recorded as a regular instrumented request. Fix reads
  `app._basePath` at install time and stores the absolute URL
  (`/api/metrics`) as `appProps.simsysMetricsPath`; the runtime
  comparison now matches Hono's actual routing.
- **F10 — `started_at` format aligned across Python/Node/Go** (MED).
  Pre-fix, Python emitted `2026-04-26T10:30:00+00:00` while Node
  and Go emitted `2026-04-26T10:30:00Z`. Cross-implementation
  `simsys_build_info` samples for the same service/version/commit
  had non-equal label tuples, breaking the unified-contract claim
  in the README. Python now uses
  `strftime("%Y-%m-%dT%H:%M:%SZ")`.
- **F12 — Hono idempotency warning includes `metricsPath`
  mismatch** (LOW). Pre-fix, the second-install warning fired only
  on service/version mismatch; a different `metricsPath` was a
  silent no-op (route is stuck at the first install's path). The
  warning now compares the absolute metricsPath as well so
  re-install with a different path tells the caller why their new
  path is 404ing.
- **F16 — Python `_peek_service` reads under the lock** (LOW).
  Pre-fix, `_peek_service` dereferenced `_SERVICE` without holding
  `_SERVICE_LOCK`. Concurrent installs could observe torn or stale
  snapshots. One-line fix.

### Changed
- `register_process_collector(service)` now returns
  `ProcessCollectorRollbackState(action, prior_collector,
  installed_collector)` — added `installed_collector` for the
  identity check on rollback.
- `register_build_info` (Python) now emits `started_at` with `Z`
  suffix (was `+00:00`). User-visible label format change — Grafana
  dashboards comparing `started_at` against literal strings should
  switch to ends-with-Z matching if any were strict-equality. The
  format is the same as Node/Go.

### Tests
- `tests/test_install_self_audit_v0310.py` — 5 new pytest cases
  (F6 lock-scope coherence, F7 no-clobber on concurrent swap, F7
  third-swap variant, F10 Z-suffix, F16 _peek_service round-trip).
- `node/tests/install-self-audit-v0310.test.ts` — 4 new vitest
  cases (F8/F11 swap counter monotonicity, F9 basePath
  exemption, F12 metricsPath warning, F12 silent-when-match).
- Verification: ruff clean, pytest 93 passed, go vet/test/test
  -race/staticcheck/gofmt -l clean, npm build clean, npm test
  42 passed, npm pack 43 files, npm audit 0 vulns, conformance
  smoke green on port 8877.

## [0.3.9] — 2026-04-26

Patch release closing five findings from a follow-up Codex audit (one
MEDIUM/HIGH, three MEDIUM, one LOW). All five regressed pre-fix on the
exact reproductions Codex provided; v0.3.9 includes regression coverage
for all four behavioural fixes.

### Fixed
- **F1 — Hono partial install double-stacks middleware on retry**
  (MED/HIGH, Node). Pre-fix, when `app.get(metricsPath, ...)` threw
  during install, rollback cleared app props but left the wildcard
  middleware (`app.use("*", ...)`) wired. A retry then added a SECOND
  middleware on top, so every subsequent request was counted twice
  (Codex repro: `simsys_http_requests_total{route="/hello"} = 2`).
  Hono offers no public API for removing routes, so the fix wires
  middleware/route AT MOST ONCE per app and gates both handlers on
  the live `simsysMetricsInstalled` flag — partial installs leave the
  wiring INERT until a retry re-arms the flag, eliminating the
  double-stack failure mode.
- **F2 — Express 5 rollback snapshots the wrong router stack**
  (MED, Node). Express 5 exposes the router at `app.router`, not
  `app._router`. Pre-fix snapshot read `app._router?.stack?.length`
  (always undefined on Express 5) so rollback no-opped — leaving a
  `/metrics` route layer in the stack on failure. Codex repro:
  stack `["/metrics"]` after fail, `["/metrics", "/metrics", "mw"]`
  after retry. Fix prefers `app.router.stack` and falls back to
  `app._router.stack` so both Express 4 and 5 truncate cleanly.
- **F3 — `build_info` rollback could delete a pre-existing sample it
  did not own** (MED, Python + Node). `started_at` is second-precision,
  so two installs of the same `service+version+commit` started in
  the same wall-clock second produce identical labelsets. Pre-fix,
  install B's rollback unconditionally called
  `build_info.remove(...)` and silently deleted install A's
  still-live sample. Fix: `register_build_info` now returns
  `(labels, was_new)` (Node: `{labels, wasNew}`) tracked via a
  module-scoped ownership set; rollback uses the new
  `unregister_build_info_if_owned(labels, was_new)` /
  `unregisterBuildInfoIfOwned(labels, wasNew)` helpers which are a
  no-op when `was_new=false`.
- **F4 — Process-collector rollback did not restore the prior
  collector/service** (MED, Python + Node). When install B
  service-swapped the singleton (Python: unregistered A's
  `SimsysProcessCollector`, registered B's; Node: mutated the
  static `service` global), rollback called
  `unregister_process_collector()` and left the registry with NO
  process collector at all — service A's `simsys_process_*` lines
  disappeared. Fix: `register_process_collector` now returns a
  `ProcessCollectorRollbackState` capturing the action
  (`reused`/`registered`/`service_swap`) plus the prior collector;
  rollback's `restore_process_collector` re-registers A's collector
  on swap-then-fail. Node additionally `.reset()`s the per-labelset
  state on swap so the swapped-out service's stale gauge samples
  don't linger between scrapes (matches Python's clean-emit
  behaviour where each collect() yields a fresh MetricFamily).
- **F5 — Go files not gofmt-clean** (LOW, Go). Pure formatting in
  `go/metrics.go` and `go/registry.go` (alignment around struct
  fields and a `var ()` block).

### Changed
- `register_build_info(...)` now returns
  `tuple[tuple[str, str, str, str], bool]` — `(labels, was_new)`.
  The same-shape `unregister_build_info(...)` is preserved as the
  unconditional-remove escape hatch for tests; new callers should
  use `unregister_build_info_if_owned(labels, was_new)`.
- `register_process_collector(service)` (Python) now returns
  `tuple[SimsysProcessCollector, ProcessCollectorRollbackState]`.
  The previous `(collector, was_new: bool)` shape is gone — adapter
  rollback uses the structured state to choose between
  re-registering the prior collector (swap), dropping the new one
  (fresh registration), or no-op (reuse).
- `registerProcessCollector(svc)` (Node) now returns
  `ProcessCollectorRollbackState`; pair with the new
  `restoreProcessCollector(state)` to undo on partial install.

### Tests
- `tests/test_install_codex_findings.py` — 4 new pytest cases for
  F3 and F4 (unit + end-to-end FastAPI rollback).
- `node/tests/install-codex-findings.test.ts` — 4 new vitest cases
  for F1–F4.
- Verification: ruff clean, `pytest -q` 88 passed, `go vet`/`go test`/
  `go test -race`/`staticcheck`/`gofmt -l` clean, `npm run build`
  clean, `npm test` 38 passed, `npm pack --dry-run` 43 files,
  `npm audit --omit=dev --audit-level=moderate` 0 vulns,
  conformance smoke green on port 8876.

## [0.3.8] — 2026-04-25

Patch release closing five findings from a follow-up Codex audit (three
MEDIUM, one LOW/MEDIUM, one LOW).

### Fixed
- **Python install rollback no longer leaks process-wide Prometheus
  state** (MED). Pre-fix, FastAPI/Flask called `set_service`,
  `register_process_collector`, and `register_build_info` BEFORE
  framework wiring — and the rollback only cleared app-local state.
  A successful retry then left two `simsys_build_info` samples for
  the same service/version/commit (different `started_at`). The
  rollback now also: drops the build_info label-set
  (`unregister_build_info(...)`); unregisters the process collector
  if THIS install was the one that created it; restores the
  process-wide `_SERVICE` global to its pre-install value. Backed by
  two regression tests in `tests/test_install_partial_failure.py`.
- **Python `ProgressTracker.inc` and `set_total` reject NaN/Infinity**
  (MED). Constructor validation was added in v0.3.7 but the mutators
  still used a plain `< 0` check, which `nan` passed vacuously
  (since `nan < 0` is False per IEEE-754). Both methods now use
  `math.isfinite` and reject `nan`, `+inf`, `-inf`.

### Changed
- `register_process_collector(service)` now returns
  `(collector, was_new)` — install rollback uses the bool to decide
  whether to undo the registration.
- `register_build_info(service, version, commit, started_at)` now
  returns the resolved label tuple so callers (rollback, tests) can
  drop the exact label-set via the new
  `unregister_build_info(...)` helper.
- `set_service` accepts `Optional[str]`. Internal-only signature
  widening for rollback; passing None is private API.
- `simsys_metrics.get_service` and `set_service` are now in `__all__`.
  `get_service` was previously not re-exported (the README example
  referenced it anyway). The earlier `set_service = set_service`
  no-op self-assignment is gone.

### Documentation
- README custom-metric example was copy-paste broken: the
  `labelnames` declared `service` but the active call omitted it,
  so consumers running the example verbatim got
  `ValueError: counter metric is missing label values`. The example
  now passes `service=get_service()` and references `get_service`
  via the public API.

## [0.3.7] — 2026-04-25

Patch release closing three findings from a follow-up Codex audit (one
HIGH, two MEDIUM).

### Fixed
- **install() retry no longer double-stacks middleware after partial
  failure** (HIGH). When `app.add_middleware()` succeeded but a
  subsequent step (e.g. `Instrumentator.expose()`) raised, the
  rollback cleared the sentinel but left the middleware permanently
  attached to `app.user_middleware`. A retry then added a SECOND
  metrics middleware and every request was counted twice. The
  rollback now snapshots `app.user_middleware` and `app.router.routes`
  before the install attempt and truncates back to that snapshot on
  failure. Same pattern in Flask: snapshots `before_request_funcs`,
  `after_request_funcs`, `url_map`, and disconnects the
  `got_request_exception` signal handler. Backed by a regression
  test that forces `Instrumentator.expose` to fail once, asserts
  `len(app.user_middleware)` is unchanged after rollback, retries,
  and confirms one request → counter=1 (not 2).
- **NaN / +Inf / -Inf intervals are now rejected** (MED). The
  previous `interval <= 0` check let `math.nan` through (since
  `nan <= 0` is False per IEEE-754) and Infinity passed too. The
  daemon thread would later crash inside `time.sleep(nan)`. Affects
  `track_queue` and `track_progress` (and `ProgressOpts.total` /
  `.window`); now use `math.isfinite` checks.

### Documentation
- README "Metric catalogue" section now correctly distinguishes
  baseline/opt-in metrics (which DO have `service`) from custom
  metrics (which the factories don't force). The advanced-counter
  example was updated to include `service` in `labelnames`.

### Changed
- `make_counter` / `make_gauge` / `make_histogram` now WARN (don't
  error) at registration time when `service` is missing from
  `labelnames`. Once-per-metric-name; library code that
  legitimately doesn't need `service` (rare) isn't drowned in
  noise. Backed by tests in `tests/test_prefix_guard.py`.

## [0.3.6] — 2026-04-25

Patch release closing six findings from a follow-up Codex audit (two
HIGH, three MEDIUM, one LOW).

### Fixed
- **HTTP method label is now bounded** (HIGH). Pre-fix, the raw
  `request.method` string was passed straight through, so a hostile
  client sending arbitrary methods (`X_AUDIT_1`, `ASDF`, etc.) forced
  one new `simsys_http_requests_total` series per distinct value —
  defeating the package's stated cardinality discipline. Both FastAPI
  and Flask now route the method through `normalize_method()` which
  returns the upper-cased value if it's in the RFC 9110 + PATCH
  allow-list, else `"OTHER"`. Backed by
  `tests/test_method_normalization.py` (FastAPI + Flask end-to-end).
- **`install()` rollback now covers the full framework-wiring phase**
  (HIGH). Pre-fix the try/except only wrapped the registry/build-info
  setup, so a late install (after the app started serving) where
  FastAPI's `app.add_middleware` or Flask's `before_request` /
  `add_url_rule` raised would leave the sentinel set without metrics
  actually wired — and the idempotent guard would silently swallow
  every retry. The framework wiring is now inside the protected block;
  failures roll back the sentinel cleanly. Backed by two new
  regression tests using `monkeypatch` to force late-install errors.
- **Custom `metrics_path` now appears in the exempt-paths set** (MED).
  When a user passed `metrics_path="/internal/metrics"`, the exposed
  set (`app.state.simsys_exempt_paths` / `app.extensions[...]`) still
  said `/metrics`, so auth middleware reading it would still
  auth-block the actual scrape route. The set is now built per-install
  to include the user's path.
- **`track_queue` now rejects non-positive intervals** (MED). Pre-fix,
  `interval=0` would create a busy-loop in an unstoppable daemon
  thread (the daemon dies with the interpreter, but burns CPU until
  then). Raises `ValueError` at call time instead.

### Documentation
- Multiprocess mode README section now clearly marks itself as
  **FastAPI only** and documents that Flask under gunicorn does NOT
  have automatic multiproc handling — the previous wording implied
  framework parity. Native Flask multiproc support is tracked for a
  future minor release.

## [0.3.5] — 2026-04-25

Patch release addressing nine findings from a follow-up audit (four
≥80-confidence + five below-threshold once validated).

### Fixed
- **`track_job` async detection now handles `functools.partial` and
  callable instances.** v0.3.3 used `asyncio.iscoroutinefunction` which
  is also being deprecated in 3.12. Switched to
  `inspect.iscoroutinefunction` (which correctly detects partial-wrapped
  coroutines via `__wrapped__`), plus a fall-through that inspects
  `__call__` for callable-instance wrappers (those return False from the
  top-level check). Backed by two regression tests in
  `tests/test_track_job.py`: partial-wrapped async raising → `error=1`,
  async-callable instance raising → `error=1`.
- **Removed dead `_QUEUE_THREADS` module-level list.** Every
  `track_queue()` call appended to it; the list was only ever read by
  `_reset_for_tests()` for `clear()`. Unbounded leak source for apps
  that re-init queue trackers in factories. Daemon threads continue to
  die with the interpreter as before — no behavioural change.
- **Removed dead `Optional[Response] = None`** in the FastAPI
  middleware dispatch — the variable was assigned but never read after
  the `try` block.

### Added
- `tests/test_fastapi_install_multiproc.py` now drives a real
  `/items/{id}` route through the BaseHTTPMiddleware under multiproc
  mode and asserts `simsys_http_requests_total` records the dispatch.
  Closes a coverage gap from v0.3.3.

### Documentation
- Added a "Cross-runtime caveat" section to all three READMEs
  (`README.md`, `node/README.md`, `go/README.md`) documenting that
  `simsys_process_memory_bytes.type` label values are intentionally
  runtime-specific (Python+Go: `rss`/`vms`; Node:
  `rss`/`heapUsed`/`heapTotal`/`external`). A `$service`-templated
  dashboard panel filtering `type="vms"` will return empty for Node
  services. The `rss` value is the only label-value common to all
  three runtimes.
- `node/README.md` metric catalogue now lists the four
  `simsys_progress_*` rows (was missing despite being defined in code).
- `node/CHANGELOG.md` and `go/CHANGELOG.md` heading-separator
  standardised on em-dash to match the root file (was hyphen).

## [0.3.4] — 2026-04-25

Documentation-only release. No code changes.

### Documentation
- Root `README.md` now describes the monorepo as **three** packages
  (Python + Node + Go), with a Go badge and a Go row in the layout
  table. Previously read "two packages" with no Go mention.
- Python install snippets in `README.md` bumped from the stale `v0.3.0`
  pin to the current `v0.3.4` (was missing every fix from v0.3.1
  through v0.3.3 — install idempotency, async track_job, partial-
  failure rollback, etc.).
- `go/README.md` install snippet bumped from `go/v0.2.0` to `v0.2.4`,
  and a note added clarifying the `go get` version-arg syntax (drops
  the `go/` tag prefix because the module path already ends in `/go`).

## [0.3.3] — 2026-04-25

Patch release addressing findings from a follow-up audit.

### Fixed
- **`@track_job` now correctly times async functions.** The previous
  decorator wrapped only sync callables: passing an async function
  worked at the type level but exited the timing span as soon as the
  coroutine OBJECT was returned (before it ran). Async exceptions were
  silently misrecorded as `outcome="success"`. The decorator now
  detects coroutine functions via `asyncio.iscoroutinefunction` and
  emits an async-aware wrapper that awaits the coroutine inside the
  span. Backed by `tests/test_track_job.py` (success + error cases).
- **FastAPI middleware migrated from function-style decorator to
  `BaseHTTPMiddleware`.** `@app.middleware("http")` is brittle under
  recent Starlette versions (0.51+) — the `TestClient` can deadlock
  on the call_next path. Switched to a `BaseHTTPMiddleware` subclass
  registered via `app.add_middleware()`, the canonical and testable
  pattern. No behavior change for the happy path; metrics still
  carry the same labels.

### Hardening
- pytest suite now runs under `pytest-timeout` with a 30-second
  per-test cap (`addopts = --timeout=30`), so a future hang fails
  fast instead of holding CI indefinitely.
- `pytest-timeout>=2.3` added to the `[test]` extra in
  `pyproject.toml`.

## [0.3.2] — 2026-04-25

Patch release closing a regression in v0.3.1's install-sentinel ordering
and adding regression-test coverage for two paths that were previously
implicit.

### Fixed
- **`install()` no longer phantom-installs on partial failure.** v0.3.1
  moved the sentinel ahead of the side-effecting registration calls; if
  any of them raised (e.g. transient `subprocess.TimeoutExpired` in
  `git rev-parse`), the sentinel was left set and subsequent retry calls
  returned early — silently dropping the install. The sentinel is now
  rolled back inside an `except` block so a retry can proceed cleanly.
  Backed by `tests/test_install_partial_failure.py` (FastAPI + Flask).

### Added (test coverage only)
- `test_flask_unhandled_exception_count_is_exactly_one` — symmetry with
  the double-count test for the no-handler 5xx path.
- `test_flask_before_request_exception_records_5xx` — verifies the
  exception-from-`before_request` path still records the route + 5xx.
- `test_flask_before_request_failure_before_simsys_skips_histogram` —
  pins the contract that when a user's `before_request` runs (and fails)
  ahead of `_simsys_before`, the counter still increments but the
  histogram observe is skipped (no 0.0-bucket pollution).

### Documentation
- `go/metrics.go` — `Install` godoc now warns that re-Install with a
  different Service on the same Registry produces inconsistent labels
  (build_info gauge gets the new Service; everything else keeps the
  original).
- `node/README.md` — Install snippet bumped from the stale
  `node-v0.1.0` tarball URL to `node-v0.3.2`.

## [0.3.1] — 2026-04-25

Patch release tightening `install()` semantics and Flask error-path
observability based on a follow-up code review.

### Fixed
- **Flask: uncaught exceptions are now counted as 5xx.** Previously, an
  unhandled exception escaped Flask's `after_request` hook entirely and
  the request silently produced no metric sample at all — a real
  observability gap on the error path. Now wired through
  `got_request_exception` so the counter increments and the histogram
  observes once-and-only-once even on the exception path.
- **`install()` warns on service/version mismatch.** A second `install()`
  on the same app with different `service=` / `version=` arguments
  previously no-op'd silently. Now logs a clear warning and keeps the
  original install — apps that legitimately want re-init must drop the
  sentinel first.
- **`install()` sets the sentinel before side effects.** A partial first
  install (e.g. transient subprocess timeout in `git rev-parse`) no
  longer leaves the registry in a half-populated state that a retry
  would re-poison.

### Documentation
- README: new "Multiprocess mode" section. Documents the
  `PROMETHEUS_MULTIPROC_DIR` env-var ordering requirement (must be set
  before `simsys_metrics` is imported, not from inside an app factory).

## [0.3.0] — 2026-04-25

First public release.

### Added
- **Python package** (`simsys-metrics`) — FastAPI and Flask install paths.
  - `install()` auto-detects framework, mounts `/metrics`, registers HTTP
    request count + duration, process CPU/memory/FDs, and `simsys_build_info`.
  - Opt-in helpers: `track_queue` (poller thread + gauge), `track_job`
    (decorator + context manager), `track_progress` (batch progress with EWMA
    rate + ETA gauges).
  - Prefix-guarded registry: every metric name must start with `simsys_`.
  - Cardinality discipline: route templates only, status-class bucketing
    (`2xx` / `3xx` / …), `safe_label()` allow-list helper.
  - Multiprocess mode: when `PROMETHEUS_MULTIPROC_DIR` is set, `/metrics`
    aggregates samples from every process via `MultiProcessCollector`.
- **Node package** (`@simsys/metrics`) — Express 5 + Hono (Bun/Node) adapters.
  Same metric catalogue and label conventions as Python; ESM-only,
  TypeScript types included, requires Node ≥ 18.
- **Go package** (`github.com/Avicennasis/simsys-metrics/go`) — net/http
  middleware. Same catalogue, prefix-guarded registry, idiomatic
  `*Metrics` handle.
- Unmatched routes (404 / scanner traffic) collapse to one
  `route="__unmatched__"` bucket so they cannot blow out cardinality.
- `install()` / `Install()` are idempotent: a second call on the same app
  is a no-op rather than doubling middleware or panicking on duplicate
  metric registrations.
- Go middleware preserves `http.Hijacker` / `http.Pusher` / `http.Flusher`
  via `Unwrap()` and `http.NewResponseController`, supporting websocket
  upgrades and HTTP/2 server push.

### Tooling
- CI: pytest matrix on Python 3.10–3.13; vitest on Node; `go test -race`
  on Go 1.23 + 1.24; staticcheck pinned to a tagged release.
- Dependabot covers `pip`, `npm`, `gomod`, and `github-actions`.
- pre-commit with ruff (lint + format).
- OpenSSF Scorecard, build provenance attestation on Go releases.

[0.3.8]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/v0.3.8
[0.3.7]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/v0.3.7
[0.3.6]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/v0.3.6
[0.3.5]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/v0.3.5
[0.3.4]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/v0.3.4
[0.3.3]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/v0.3.3
[0.3.2]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/v0.3.2
[0.3.1]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/v0.3.1
[0.3.0]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/v0.3.0
