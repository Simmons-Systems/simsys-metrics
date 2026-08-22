# Changelog

All notable changes to `simsys-metrics-go` will be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [go/v0.3.1] — 2026-08-22

Supersedes the abandoned `go/v0.3.0` tag. **Pin `go/v0.3.1`, not `go/v0.3.0`.**

### Why v0.3.1 and not a re-tag

`go/v0.3.0` was pushed on 2026-07-06 (`8636e8c`) carrying real work, but no
GitHub release was ever created for it: the tag predates the tag-routing fix
in `release.yml`, which only matched the old unified `v*` prefix. It was never
documented here either, so a consumer had no way to tell whether it was a
release, a pre-release, or an abandoned tag — gorapide-observer deliberately
pinned `v0.2.11` because of exactly that ambiguity (Redmine #50032).

The tag is **left exactly where it is**. `proxy.golang.org` caches module tags
immutably, so re-pointing one hands a checksum mismatch to anyone who already
fetched it. Superseding is the only safe move.

`go/v0.3.0` also predates the extended HTTP bucket schedule, so pinning it
would *regress* the p95 ceiling fix that Python already shipped.

### Added

- `TrackPool` and the four `simsys_pool_*` gauges (`active`, `idle`,
  `waiting`, `max`). Returns an idempotent `stop func()`.
- Runtime and process metrics: `simsys_process_threads`,
  `simsys_runtime_goroutines`, `simsys_runtime_gc_collections_total`,
  `simsys_runtime_gc_pause_total_seconds`.
- `simsys_scrape_duration_seconds` and `simsys_scrape_errors_total`.
- `fuzz_test.go` — fuzz target over the HTTP label normalisation path.
- Conformance against `spec/metrics-contract.json` (`contract_conformance_test.go`).

### Changed (BREAKING, from v0.2.11)

- **`TrackProgress` now returns `(ProgressTracker, error)`** instead of
  `ProgressTracker`, and validates its options rather than panicking on
  invalid input. Call sites need one extra error check. This is the whole of
  the breaking surface between v0.2.11 and here.
- HTTP latency buckets extended past 10s — the schedule gains `15.0, 30.0,
  60.0`. Buckets are baked in at instrumentation time, so a service only picks
  this up when it upgrades and redeploys; during a partial rollout a service
  running mixed versions across replicas emits inconsistent `le` sets.
- `Install` now trims `Service` and `Version`, so an all-whitespace value
  folds into the existing `ErrInvalidInstallOpts` rather than becoming a real
  identity. `service` is the join key with simsys-logevent, which trims its
  own copy.

### Fixed

- `simsys_scrape_errors_total` was registered but **never incremented**, while
  `MetricsHandler`'s doc comment claimed it was — it read 0 forever, so an
  operator alerting on it had a permanently green signal from an unwired
  instrument. Now wired through `promhttp`'s `ErrorLog` (#50320).
- The same counter had **no series at all** until the first error, because a
  `CounterVec` creates no child until `WithLabelValues`. `rate()` over an
  absent series returns nothing rather than 0, so an "errors started" alert
  could not fire on the transition that matters. Initialised to 0 at install.

### Known divergence

`simsys_runtime_gc_collections_total` carries `["service"]` here and
`["service", "generation"]` in Python. Tracked as #50345 and declared in
`spec/metrics-contract.json`; a panel using `by (generation)` returns empty
for Go services.

## [go/v0.2.11] — 2026-04-29

### Changed (BREAKING)

- **Module path renamed**:
  `github.com/Avicennasis/simsys-metrics/go` →
  `github.com/Simmons-Systems/simsys-metrics/go`. Repository
  transferred to the `Simmons-Systems` GitHub org. Consumers must
  update `import` statements AND `go.mod` `require` lines. Run
  `go mod tidy` to refresh `go.sum`.
- The `go/v0.2.0` … `go/v0.2.10` tags resolve via the GitHub redirect
  but their `go.mod` files contain the old `Avicennasis` module path,
  which Go's verifier rejects against the new import path. Pin to
  `go/v0.2.11` or later.
- No functional code changes — metadata-only.

## [go/v0.2.8] — 2026-04-25

### Fixed
- **`Install` no longer accumulates `simsys_build_info` samples on
  re-Install** (LOW/MED). Pre-fix, calling `Install` twice on the
  same `Registry` re-used the existing `*GaugeVec` (good) but then
  wrote a fresh `.Set(1)` with a new `started_at` (bad), leaving
  TWO `simsys_build_info` samples for the same
  service/version/commit when the two calls straddled a second
  boundary. Now the registration uses
  `registerOrExistingWithStatus`; the `.Set(1)` is skipped when
  the GaugeVec was reused (an earlier Install owns the canonical
  sample). Backed by
  `TestInstallIdempotentDoesNotAccumulateBuildInfoSamples`.

### Changed
- New internal helper `registerOrExistingWithStatus[T]` returns
  `(T, isNew bool)` so callers (currently just the build_info
  registration) can branch on first-vs-reused. `registerOrExisting`
  is preserved as a thin wrapper for the existing call sites that
  don't need the bool.

## [go/v0.2.7] — 2026-04-25

### Changed
- `MakeCounter` / `MakeGauge` / `MakeHistogram` now log a one-time
  `slog.Warn` at registration time when `"service"` is missing from
  the labels slice. Custom metrics without a `service` label can't
  participate in cross-service `$service` Grafana dashboards; the
  warning makes the omission visible without erroring (consumer code
  that legitimately doesn't need `service` is not broken). Backed by
  `TestMakeCounterWarnsWhenServiceLabelMissing`.

## [go/v0.2.6] — 2026-04-25

### Fixed
- **HTTP method label allow-list** (HIGH). `r.Method` was passed
  straight into the `simsys_http_requests_total` label, so a hostile
  client sending arbitrary methods forced one new series per distinct
  value. The middleware's shared `recordRequest` helper now routes the
  method through `NormalizeMethod()` which collapses anything outside
  the RFC 9110 + PATCH allow-list to `"OTHER"`. Backed by
  `go/method_normalization_test.go`.

### Changed
- `go.mod` now declares `go 1.23` as the floor instead of the
  toolchain-bumped `go 1.25.0`. The `github.com/prometheus/procfs`
  v0.20+ pin still forces `go >= 1.25.0` via its own go directive,
  so the effective minimum remains 1.25 in practice — but the module
  declaration matches what our code actually requires.
- CI matrix updated to test against Go 1.25.x and 1.26.x (matching
  the procfs-imposed floor). Previously claimed 1.23.x/1.24.x while
  the toolchain silently downloaded 1.25.

## [go/v0.2.5] — 2026-04-25

### Documentation
- `Middleware` godoc gained a note covering the hijack-then-panic edge
  case: a handler that calls `http.NewResponseController(w).Hijack()`
  and then panics still records `status="5xx"` correctly, but the
  re-panic propagates to a connection that is no longer http-managed —
  net/http closes the underlying socket without writing a 500 body.
  Correct behavior for mid-upgrade websockets; documented so it
  doesn't surprise consumers.
- README install snippet bumped from `v0.2.4` to `v0.2.5`.
- README gained a "Cross-runtime caveat" section explaining the
  `simsys_process_memory_bytes.type` label-value divergence between
  Python+Go (`rss`/`vms`) and Node
  (`rss`/`heapUsed`/`heapTotal`/`external`).
- CHANGELOG heading separators standardized on em-dash to match the
  root file (was hyphen).

## [go/v0.2.4] — 2026-04-25

### Documentation
- README install snippet bumped from `go/v0.2.0` to `v0.2.4`. Added a
  note clarifying that the `go get` version arg drops the `go/` tag
  prefix (the module path already ends in `/go`, so the Go module
  resolver expects `@v0.2.4` not `@go/v0.2.4`).

No code changes.

## [go/v0.2.3] — 2026-04-25

### Fixed
- **Handler panics are now correctly recorded as 5xx.** Previously, if
  a handler panicked before calling `WriteHeader`, the `statusRecorder`
  default of `http.StatusOK` (200) was emitted as the metric label,
  the panic bypassed the deferred labeller, and the request was
  counted as a successful 2xx. The middleware now `recover()`s the
  panic, sets `wrapped.status` to `500` if no status was written,
  emits the metric with `status="5xx"`, and re-panics so net/http's
  default recovery (log + close connection) still runs. If the
  handler had already written an explicit status (e.g. 400) before
  panicking, that status is preserved. Backed by
  `go/middleware_panic_test.go`.
- Refactored shared labelling logic into `recordRequest()` so the
  happy path and the panic-recovery path can't drift apart.

## [go/v0.2.2] — 2026-04-25

### Added
- Test coverage: `TestInstallIdempotentProcCollectorReused` directly
  asserts that the simsysProcessCollector survives a second `Install`
  with the same Registry and continues to emit `simsys_process_*` on
  scrape — closing a coverage gap from v0.2.1's orphan fix.

### Documentation
- `Install` godoc now explicitly warns that re-Install with a different
  `Service` on the same Registry produces inconsistent labels (the
  build_info gauge gets the new Service value, but the existing
  collectors keep emitting the original one). Use one Service per
  Registry; allocate a fresh Registry to legitimately re-init under a
  new identity.

## [go/v0.2.1] — 2026-04-25

### Fixed
- **`Install()` no longer orphans a process collector on second call.**
  When the same `*prometheus.Registry` was passed to `Install` twice, the
  second-call `simsysProcessCollector` was built but its registration
  silently absorbed an `AlreadyRegisteredError` and the freshly-built
  collector was never wired up — leaving an unreferenced object on the
  heap. Now routed through the same `registerOrExisting` generic helper
  used by `MakeCounter`/`MakeGauge`/`MakeHistogram`, so the existing
  registered collector is reused. No user-visible behaviour change for
  first-call installs.

## [go/v0.2.0] — 2026-04-25

First public release of the Go sibling package. See the
[root CHANGELOG](../CHANGELOG.md) for the full feature set; Go-specific
notes:

- `Install(InstallOpts)` returns `*Metrics` with a private
  `*prometheus.Registry` to avoid default-registry collisions, and is
  idempotent on repeated calls with the same Registry.
- Middleware preserves `http.Hijacker`, `http.Pusher`, and `http.Flusher`
  via `Unwrap()` so handlers can call `http.NewResponseController(w)` after
  the metric wrapper. Required for websocket upgrades, HTTP/2 server push,
  and SSE flushing.
- Adapter entry points: `MetricsHandler()` for `/metrics`,
  `Middleware(MiddlewareOpts)` with a `RouteExtractor` callback for
  bounded-cardinality request instrumentation. When the extractor returns
  an empty string (or no extractor is supplied), the route label is the
  literal `UnmatchedRouteLabel` (`"__unmatched__"`) — scanner traffic
  cannot blow out cardinality.
- Goroutine-leak safety: `TrackQueue` and `TrackProgress` accept
  `context.Context` for cancellation and return idempotent stop funcs.
  Enforced in tests via `go.uber.org/goleak` in `TestMain`.
- `TrackQueue` panic recovery: a `depthFn` panic logs the first occurrence
  via `slog` and silently absorbs subsequent panics to avoid log floods.

[go/v0.2.8]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/go/v0.2.8
[go/v0.2.7]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/go/v0.2.7
[go/v0.2.6]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/go/v0.2.6
[go/v0.2.5]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/go/v0.2.5
[go/v0.2.4]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/go/v0.2.4
[go/v0.2.3]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/go/v0.2.3
[go/v0.2.2]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/go/v0.2.2
[go/v0.2.1]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/go/v0.2.1
[go/v0.2.0]: https://github.com/Simmons-Systems/simsys-metrics/releases/tag/go/v0.2.0
