# simsys-metrics (Go)

Drop-in Prometheus `/metrics` template for Go web apps.
Go sibling of the [Python package](../) and [Node package](../node/).

One `Install` call registers the baseline (build info, HTTP, process CPU/memory/FDs),
returns a `*Metrics` handle, and exposes a prefix-guarded registry consumers use
for app-specific metrics.

## Install

```bash
go get github.com/Simmons-Systems/simsys-metrics/go/v2@latest
```

Pin to a specific release:

```bash
go get github.com/Simmons-Systems/simsys-metrics/go/v2@v2.0.1
```

Note: the GitHub release tag is named `go/vX.Y.Z` (per Go's
sub-module tagging scheme), but the `go get` version arg drops the
`go/` prefix — the module path already ends in `/go`.

## Usage

```go
import (
    "context"
    "net/http"
    "time"

    simsys "github.com/Simmons-Systems/simsys-metrics/go/v2"
)

func main() {
    m, err := simsys.Install(simsys.InstallOpts{
        Service: "my-service",
        Version: "1.0.0",
        // Commit: auto-detected from SIMSYS_BUILD_COMMIT env,
        //         debug.ReadBuildInfo() vcs.revision, or `git rev-parse`.
    })
    if err != nil {
        panic(err)
    }

    mux := http.NewServeMux()
    mux.Handle("/metrics", m.MetricsHandler())
    mux.HandleFunc("/work", func(w http.ResponseWriter, r *http.Request) {
        defer m.TrackJob("compute")()
        time.Sleep(10 * time.Millisecond)
        w.Write([]byte("done"))
    })

    // Route-template extractor for Go 1.22+ ServeMux — prevents unbounded
    // cardinality from raw paths.
    extractor := func(r *http.Request) string {
        if r.Pattern != "" { return r.Pattern }
        return "unknown"
    }
    handler := m.Middleware(simsys.MiddlewareOpts{Extractor: extractor})(mux)

    // Queue depth observability.
    ctx, cancel := context.WithCancel(context.Background())
    defer cancel()
    stopQueue := m.TrackQueue(ctx, "dns", 5*time.Second, func() int {
        return dnsQueue.Depth()
    })
    defer stopQueue()

    // Batch progress.
    tr, err := m.TrackProgress(ctx, simsys.ProgressOpts{
        Operation: "scan",
        Total:     int64(inputCount),
    })
    if err != nil {
        log.Fatalf("progress tracking misconfigured: %v", err)
    }
    defer tr.Stop()
    for _, item := range work {
        process(item)
        tr.Inc(1)
    }

    http.ListenAndServe(":8080", handler)
}
```

## Metric catalogue

All metrics carry a `service` label set at Install time.

| Metric | Type | Labels | Source |
|---|---|---|---|
| `simsys_build_info` | Gauge (=1) | `service, version, commit, started_at` | `Install` |
| `simsys_http_requests_total` | Counter | `service, method, route, status` | `Middleware` |
| `simsys_http_request_duration_seconds` | Histogram | `service, method, route` | `Middleware` |
| `simsys_process_cpu_seconds_total` | Counter | `service` | `Install` (custom process collector) |
| `simsys_process_memory_bytes` | Gauge | `service, type` (`rss`, `vms`) | `Install` |
| `simsys_process_open_fds` | Gauge | `service` | `Install` |
| `simsys_queue_depth` | Gauge | `service, queue` | `TrackQueue` |
| `simsys_jobs_total` | Counter | `service, job, outcome` | `TrackJob` / `TrackJobSpan` |
| `simsys_job_duration_seconds` | Histogram | `service, job, outcome` | same |
| `simsys_progress_processed_total` | Counter | `service, operation` | `TrackProgress` |
| `simsys_progress_remaining` | Gauge | `service, operation` | same |
| `simsys_progress_rate_per_second` | Gauge | `service, operation` | same (EWMA-smoothed) |
| `simsys_progress_estimated_completion_timestamp` | Gauge | `service, operation` | same |

Histogram bucket schedules:
- **HTTP**: `[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]` (sub-second latency)
- **Jobs**: `[0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300]` (seconds to minutes)

Both identical to Python/Node so cross-app dashboards align.

### Cross-runtime caveat: `simsys_process_memory_bytes.type`

Memory accounting is runtime-specific. The `type` label values differ
across the three sibling packages:

| Runtime | `type` values |
|---------|---------------|
| Go (this package) | `rss`, `vms` (procfs status fields) |
| Python (`simsys-metrics`) | `rss`, `vms` (psutil's resident + virtual sizes) |
| Node (`@simsys/metrics`) | `rss`, `heapUsed`, `heapTotal`, `external` (`process.memoryUsage()`) |

A `$service`-templated dashboard filtering `type="vms"` returns nothing
for Node services. Either build runtime-aware panels or filter on
`type="rss"` — the one label value common to all three runtimes.

## One service identity per Registry

**`Service` is per-`Registry`, not per-process — and unlike the Python and Node
lanes, that makes two identities in one process legitimate, provided each has
its own registry.**

```go
// Supported: two identities, two registries.
a, _ := simsys.Install(simsys.InstallOpts{Service: "alpha", Version: "1.0.0",
    Registry: prometheus.NewRegistry()})
b, _ := simsys.Install(simsys.InstallOpts{Service: "beta", Version: "1.0.0",
    Registry: prometheus.NewRegistry()})

// NOT supported: two identities, one registry.
reg := prometheus.NewRegistry()
simsys.Install(simsys.InstallOpts{Service: "alpha", Version: "1.0.0", Registry: reg})
simsys.Install(simsys.InstallOpts{Service: "beta",  Version: "1.0.0", Registry: reg})
```

The second form does not split the two identities. `Install` is idempotent on a
registry, so the already-registered collectors — process metrics, HTTP
histograms, queue/job/progress — keep emitting under the **first** `Service`;
only `simsys_build_info` gains the new value. A dashboard joining `build_info`
to the rest on `service` then matches nothing.

Since 1.0.0 that case logs `slog.Warn` with the stable marker
`simsys-metrics: SERVICE IDENTITY CHANGE`, naming both identities, once per new
identity per registry. Behaviour is unchanged in this release; it will return
`ErrServiceIdentityConflict` in the next major (Redmine #50321). Installing
twice with the *same* `Service`, and installing different services into
*different* registries, both stay silent — those are the supported shapes.

## Prefix guard

Any metric registered via `m.MakeCounter` / `m.MakeGauge` / `m.MakeHistogram` whose
name doesn't start with `simsys_` panics at registration time. Matches the
`ValueError` raised by the Python `_registry._guard_name` and the `Error`
thrown by the Node `guardName`.

```go
m.MakeCounter("requests_total", "help", nil)         // PANICS — no simsys_ prefix
m.MakeCounter("simsys_requests_total", "help", nil)  // OK
```

## Cardinality helpers

`SafeLabel` (and `SafeLabelSet` for map-based allow sets) coerce user-facing
values into a bounded enum:

```go
ticker := simsys.SafeLabel(userInput, []string{"AAPL", "GOOG", "NVDA"})
// -> "AAPL" or "other"
```

`IPToSubnet24` buckets IPv4 addresses into their `/24` CIDR for metrics that
would otherwise have an unbounded IP label:

```go
bucket := simsys.IPToSubnet24("192.168.1.42")  // -> "192.168.1.0/24"
// IPv6 and unparseable inputs -> "other"
```

## Commit detection

`simsys_build_info.commit` is resolved in this order:

1. `SIMSYS_BUILD_COMMIT` environment variable (authoritative for container builds)
2. `runtime/debug.ReadBuildInfo()` `vcs.revision` (populated when `go build`
   runs inside a git tree; also reflects `vcs.modified` with a `-dirty` suffix)
3. `git rev-parse --short HEAD` (2s timeout, falls back silently)
4. Literal `"unknown"`

Containerized services should set `SIMSYS_BUILD_COMMIT` at build time via a
Docker `ARG`:

```dockerfile
ARG GIT_COMMIT=unknown
ENV SIMSYS_BUILD_COMMIT=${GIT_COMMIT}
```

```bash
docker build --build-arg GIT_COMMIT=$(git rev-parse --short HEAD) .
```

## Goroutine-leak safety

`TrackQueue` and `TrackProgress` start background goroutines. Callers MUST
either defer the returned `stop()` / `tr.Stop()` or cancel the `ctx.Context`
passed in. The test suite enforces this with
[`go.uber.org/goleak`](https://github.com/uber-go/goleak).

## Differences from Python/Node

- **Private registry per `Install`** (not the default global). Lets multiple
  tests run in parallel without duplicate-registration panics, and lets
  consumers register app-specific metrics alongside the baseline via
  `m.Registry()`.
- **No multiprocess support.** Go's concurrency model is goroutines inside a
  single process; there's no fork-worker equivalent of gunicorn or uvicorn.
- **No async story needed.** `TrackJob` returns a `func()` to defer; for
  error-aware timing use `TrackJobSpan(job)(err)`.

## See also

- [Python `simsys-metrics`](../)
- [Node `@simsys/metrics`](../node/)
