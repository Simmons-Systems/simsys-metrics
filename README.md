# simsys-metrics

> A drop-in Prometheus `/metrics` template for Python, Node.js, and Go web apps. One `install()` call; consistent metric catalogue; zero-per-app dashboard work.

[![CI](https://github.com/Simmons-Systems/simsys-metrics/actions/workflows/test.yml/badge.svg)](https://github.com/Simmons-Systems/simsys-metrics/actions/workflows/test.yml)
[![codecov](https://codecov.io/gh/Simmons-Systems/simsys-metrics/branch/main/graph/badge.svg)](https://codecov.io/gh/Simmons-Systems/simsys-metrics)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/Simmons-Systems/simsys-metrics/badge)](https://scorecard.dev/viewer/?uri=github.com/Simmons-Systems/simsys-metrics)
[![Release](https://img.shields.io/github/v/release/Simmons-Systems/simsys-metrics?display_name=tag)](https://github.com/Simmons-Systems/simsys-metrics/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-supported-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Flask](https://img.shields.io/badge/Flask-supported-000000.svg?logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![Node.js](https://img.shields.io/badge/Node.js-supported-339933.svg?logo=node.js&logoColor=white)](node/)
[![Go](https://img.shields.io/badge/Go-supported-00ADD8.svg?logo=go&logoColor=white)](go/)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://github.com/pre-commit/pre-commit)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

---

## Monorepo layout

This repository ships **three packages** sharing one metric catalogue, label
conventions, and cardinality rules — so a single `$service`-templated
Grafana dashboard works across every runtime:

| Package | Path | Languages | Tag prefix | Install |
|---------|------|-----------|------------|---------|
| `simsys-metrics` (Python) | `/` (root) | FastAPI, Flask | `python-v<semver>` (e.g. `python-v0.4.0`) | `pip install ... @ git+https://...@python-v0.4.0` |
| [`@simsys/metrics` (Node)](node/) | `node/` | Express 5, Bun + Hono | `node-v<semver>` (e.g. `node-v0.5.0`) | GitHub Release tarball URL |
| [`simsys-metrics-go`](go/) | `go/` | net/http | `go/v<semver>` (e.g. `go/v0.2.11`) | `go get ...@v0.2.11` |

The Python package remains at the repo root for pip git-install compatibility. The Node and Go packages live under [`node/`](node/) and [`go/`](go/) respectively — see each subdirectory's README for install details.

---

A drop-in observability layer for any Python web app. Adding baseline
metrics is a five-line job, and every emitted series carries the same
`service` label so a generic `$service`-templated Grafana dashboard lights
up automatically.

- **Supported stacks:** FastAPI (primary), Flask (secondary).
- **Baseline metrics** (zero extra code): HTTP request count + latency,
  process CPU/RSS/FDs, build info.
- **Opt-in helpers:** queue depth gauge, job counter + histogram.
- **Cardinality discipline** enforced by the package: route templates,
  status classes, allow-listed label helper. Every metric name must start
  with `simsys_` — the registry refuses anything else.

## Table of contents

- [Install](#install)
- [Usage](#usage)
  - [FastAPI](#fastapi)
  - [Flask](#flask)
  - [`safe_label` — cardinality helper](#safe_label--cardinality-helper)
- [Metric catalogue](#metric-catalogue)
- [Cardinality rules](#cardinality-rules)
- [`commit` detection](#commit-detection)
- [`/metrics` endpoint behaviour](#metrics-endpoint-behaviour)
- [Development](#development)
- [Contributing](#contributing)
- [License](#license)

## Install

```bash
# FastAPI service
pip install "simsys-metrics[fastapi] @ git+https://github.com/Simmons-Systems/simsys-metrics.git@python-v0.4.0"

# Flask service
pip install "simsys-metrics[flask] @ git+https://github.com/Simmons-Systems/simsys-metrics.git@python-v0.4.0"
```

Pin to the tag. Bumping a consumer means re-pointing this URL at a newer tag.

<details>
<summary>Pinning in <code>requirements.txt</code></summary>

```
simsys-metrics[fastapi] @ git+https://github.com/Simmons-Systems/simsys-metrics.git@python-v0.4.0
```

Works in plain Docker builds — no SSH agent, no auth tokens required.

</details>

## Usage

### FastAPI

```python
from fastapi import FastAPI
from simsys_metrics import install, track_queue, track_job

app = FastAPI()
install(app, service="my-api", version="1.2.3")

# Opt-in queue gauge (polled every 5s in a daemon thread)
track_queue("inference", depth_fn=lambda: job_queue.qsize())

# Opt-in per-job timer — as a decorator
@track_job("inference")
def run_inference(...): ...

# ...or as a context manager
def run(...):
    with track_job("inference"):
        ...

# Opt-in batch-progress tracking (v0.2.0+)
from simsys_metrics import track_progress, ProgressOpts
tracker = track_progress(ProgressOpts(operation="scan", total=input_count))
try:
    for item in work:
        process(item)
        tracker.inc()
finally:
    tracker.stop()
```

### Flask

```python
from flask import Flask
from simsys_metrics import install

app = Flask(__name__)
install(app, service="my-worker", version="0.4.1")
```

`install()` auto-detects the framework. It sets the process-wide `service`
label, registers the simsys process collector, wires HTTP request metrics,
mounts `/metrics`, and populates `simsys_build_info`.

Default `metrics_path` is `/metrics`; override with
`install(..., metrics_path="/internal/metrics")`.

### `safe_label` — cardinality helper

Coerce any user-facing label value into a bounded allow-list:

```python
from simsys_metrics import safe_label

ticker = safe_label(request.args.get("ticker"), {"AAPL", "GOOG", "NVDA"})
# -> "AAPL" if in the set, else "other"
```

Use this for anything an external caller controls (tickers, tenant names,
device IDs, free-form search terms) before it ends up as a Prometheus label.

## Metric catalogue

Every **baseline + opt-in** metric this package emits uses the `simsys_`
prefix AND a `service` label — so cross-service PromQL like
`sum by (service) (rate(simsys_http_requests_total[5m]))` works
unmodified across every app.

> **Custom metrics convention:** the `make_counter` / `make_gauge` /
> `make_histogram` factories enforce the `simsys_` prefix but do
> **not** force `service` into your label list. To stay compatible
> with the cross-service dashboards above, include `service` in
> `labelnames` for any custom metric you create. The factories will
> warn at registration time if `service` is missing — see the example
> in [Rich outcome taxonomies](#rich-outcome-taxonomies) below.

<!-- BEGIN GENERATED CATALOGUE -- derived from spec/metrics-contract.json.
     Do not hand-edit rows: tests/test_catalogue_matches_contract.py asserts
     this table against the contract, and the contract is the source of
     truth. Change the contract, then regenerate. -->

| Metric | Type | Labels | Runtimes | Tier | Source |
|---|---|---|---|---|---|
| `simsys_build_info` | Gauge = 1 | `service, version, commit, started_at` | Py Node Go | core | baseline |
| `simsys_http_request_duration_seconds` | Histogram | `service, method, route` | Py Node Go | core | baseline |
| `simsys_http_requests_total` | Counter | `service, method, route, status` | Py Node Go | core | baseline |
| `simsys_job_duration_seconds` | Histogram | `service, job, outcome` | Py Node Go | core | opt-in |
| `simsys_jobs_total` | Counter | `service, job, outcome` | Py Node Go | core | opt-in |
| `simsys_pool_active` | Gauge | `service, pool` | Py Node Go | core | opt-in |
| `simsys_pool_idle` | Gauge | `service, pool` | Py Node Go | core | opt-in |
| `simsys_pool_max` | Gauge | `service, pool` | Py Node Go | core | opt-in |
| `simsys_pool_waiting` | Gauge | `service, pool` | Py Node Go | core | opt-in |
| `simsys_process_cpu_seconds_total` | Counter | `service` | Py Node Go | core | baseline |
| `simsys_process_memory_bytes` | Gauge | `service, type` | Py Node Go | core | baseline — `type` values differ by runtime, see caveat below |
| `simsys_process_open_fds` | Gauge | `service` | Py Node Go | core | baseline |
| `simsys_progress_estimated_completion_timestamp` | Gauge | `service, operation` | Py Node Go | core | opt-in |
| `simsys_progress_processed_total` | Counter | `service, operation` | Py Node Go | core | opt-in |
| `simsys_progress_rate_per_second` | Gauge | `service, operation` | Py Node Go | core | opt-in |
| `simsys_progress_remaining` | Gauge | `service, operation` | Py Node Go | core | opt-in |
| `simsys_queue_depth` | Gauge | `service, queue` | Py Node Go | core | opt-in |
| `simsys_process_threads` | Gauge | `service` | Py Go | **ext** | baseline |
| `simsys_process_uptime_seconds` | Gauge | `service` | Node | **ext** | baseline |
| `simsys_runtime_gc_collections_total` | Counter | `service` (Go) / `service, generation` (Py) | Py Go | **ext** | baseline — divergent, see #50345 |
| `simsys_runtime_gc_pause_total_seconds` | Counter | `service` | Go | **ext** | baseline |
| `simsys_runtime_goroutines` | Gauge | `service` | Go | **ext** | baseline |
| `simsys_scrape_duration_seconds` | Gauge | `service` | Py Go | **ext** | baseline |
| `simsys_scrape_errors_total` | Counter | `service` | Py Go | **ext** | baseline |

Tier **core** is guaranteed in every runtime with the declared type and label
names -- a `$service`-templated dashboard may rely on it unconditionally.
Tier **ext** is runtime-specific; the Runtimes column is authoritative and a
panel using one must tolerate its absence.

<!-- END GENERATED CATALOGUE -->

### Cross-runtime caveat: `simsys_process_memory_bytes.type`

Memory accounting is fundamentally runtime-specific, so the `type` label
values intentionally **differ across the three sibling packages**:

| Runtime | `type` values |
|---------|---------------|
| Python (`simsys-metrics`) | `rss`, `vms` (psutil's resident + virtual sizes) |
| Go (`simsys-metrics-go`) | `rss`, `vms` (procfs status fields) |
| Node (`@simsys/metrics`)  | `rss`, `heapUsed`, `heapTotal`, `external` (`process.memoryUsage()`) |

A `$service`-templated dashboard panel filtering `type="vms"` will return
empty for Node services, and a panel showing `heapUsed` will return
empty for Python/Go services. Either:

- Build runtime-aware dashboards (one panel per runtime, gated on
  `service =~ "node-.*"` etc.), or
- Filter on `type="rss"` only — the one common label value.

## Rich outcome taxonomies

`@track_job` uses a 2-value `{success, error}` enum intentionally — it's the
minimum useful signal for generic job timing, and the label stays cheap.

When an app needs a richer per-operation outcome taxonomy (cache hits, validation
errors, upstream failures, etc.), hand-roll a counter via `make_counter` from the
guarded registry:

```python
from simsys_metrics._registry import make_counter

from simsys_metrics import get_service

forecast_requests_total = make_counter(
    "simsys_forecast_requests_total",
    "Forecast requests by ticker and outcome.",
    # Always include `service` in labelnames — it's what the shared
    # `$service`-templated Grafana dashboards filter on. Omitting it
    # prints a warning at registration time and breaks the dashboard
    # contract for this metric.
    labelnames=("service", "ticker", "interval", "outcome"),
)

# Outcome enum is app-specific: e.g. {cache_hit, bad_request, upstream_error,
# success, ...} for a forecasting API, or {dead, parked, active, deferred}
# for a domain scanner.
#
# At call sites, pass service= explicitly. `get_service()` returns the
# value `install(..., service=...)` set, so you don't need to thread it
# through every call site.
forecast_requests_total.labels(
    service=get_service(),
    ticker="AAPL",
    interval="1d",
    outcome="cache_hit",
).inc()
```

Pair with `safe_label()` to cap cardinality on any dimension a user controls.

## Cardinality rules

- `route` is the route **template** (`/api/jobs/{id}`), never the actual path.
- `status` is bucketed to class strings (`2xx`, `3xx`, `4xx`, `5xx`, `1xx`),
  never the raw numeric code.
- `outcome` on the job metrics is exactly one of `success` or `error`.
- Any user-derived label value should pass through
  `safe_label(value, allowed_set)` before being attached to a metric.
- The package **refuses** at registration time to register any metric whose
  name does not start with `simsys_`. Attempting so raises `ValueError`.

## `commit` detection

`simsys_build_info.commit` is resolved in this order:

1. `SIMSYS_BUILD_COMMIT` environment variable (if set and non-empty).
2. `git rev-parse --short HEAD` in the process's current working directory.
3. Literal string `"unknown"` if neither is available.

In container images, set `SIMSYS_BUILD_COMMIT` at build time:

```dockerfile
ARG GIT_COMMIT=unknown
ENV SIMSYS_BUILD_COMMIT=${GIT_COMMIT}
```

and build with `docker build --build-arg GIT_COMMIT=$(git rev-parse --short HEAD) .`.

## Multiprocess mode (FastAPI only)

> **Scope:** the multiproc support described here is currently **FastAPI
> only**. The Flask installer does not have multiproc support — when run
> under gunicorn workers it still registers the per-process
> `simsys_process_*` collector and serves `/metrics` from
> `prometheus_client`'s default registry, so per-worker metrics will not
> aggregate. If you need multiproc-correct metrics under Flask + gunicorn
> today, mount `prometheus_client.make_wsgi_app(MultiProcessCollector(...))`
> at `/metrics` yourself and skip `install()`'s `/metrics` route. Native
> Flask multiproc support is tracked for a future minor release.

When running FastAPI under uvicorn-with-workers (or gunicorn + uvicorn
worker class), set `PROMETHEUS_MULTIPROC_DIR` so the worker processes
write metric samples to a shared directory and `/metrics` aggregates
them via `prometheus_client.MultiProcessCollector`. With the env var
set, `install()` on a FastAPI app automatically:

- Mounts a multiproc-aware `/metrics` route that walks the shared directory
  on every scrape.
- Skips registration of the per-process `simsys_process_*` collector
  (`SimsysProcessCollector` reads `/proc/self` only and cannot meaningfully
  aggregate across workers).
- Tags `simsys_queue_depth` as `multiprocess_mode="livesum"` and
  `simsys_build_info` as `multiprocess_mode="liveall"`.

> **Important — env-var ordering:** `PROMETHEUS_MULTIPROC_DIR` is read at
> `simsys_metrics` **import time** (so the gauges can be constructed with
> the right `multiprocess_mode`). Set the env var in your Dockerfile / shell
> / process-manager config before any Python code runs. Setting it after
> importing — for example inside an app-factory function — leaves the
> gauges constructed in single-process mode and `/metrics` aggregation
> will silently fail.

A typical Dockerfile:

```dockerfile
ENV PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus_multiproc
RUN mkdir -p /tmp/prometheus_multiproc
```

## `/metrics` endpoint behaviour

- Auto-mounted at `/metrics` on the same port as the app. No separate metrics
  port.
- Designed for direct scraping on a localhost or VPC interface — the package
  itself adds no auth on the endpoint; gate it at the reverse proxy / network
  layer if exposed publicly.
- `app.state.simsys_exempt_paths` (FastAPI) and
  `app.extensions["simsys_metrics"]` (Flask) expose the recommended
  auth-exempt path set so upstream middleware can skip it without
  hard-coding the list.

## Development

```bash
git clone https://github.com/Simmons-Systems/simsys-metrics.git
cd simsys-metrics
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[fastapi,flask,test]'

pytest                           # 93 unit + integration tests
bin/check-metrics-conformance.sh # end-to-end smoke test against the demo app
```

### Release flow

1. Bump `version` in `pyproject.toml` and `simsys_metrics/__init__.py`.
2. Add a `CHANGELOG.md` entry.
3. Run `pytest` and `bin/check-metrics-conformance.sh` — both must be green.
4. `git tag python-vX.Y.Z && git push --tags`.
5. Consumers re-pin their install URL to the new tag.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and PRs welcome — bug reports,
metric-catalogue gaps, or new framework install paths (Starlette, Quart, etc.)
all fair game. Security issues: see the org-level
[SECURITY.md](https://github.com/Simmons-Systems/.github/blob/main/SECURITY.md).

By contributing you agree to the terms of the
[Code of Conduct](https://github.com/Simmons-Systems/.github/blob/main/CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE). Use it anywhere, no attribution required, no warranty.

## See also

- **Upstream dependencies:**
  [prometheus_client](https://github.com/prometheus/client_python),
  [prometheus-fastapi-instrumentator](https://github.com/trallnag/prometheus-fastapi-instrumentator),
  [psutil](https://github.com/giampaolo/psutil).
- **Grafana dashboard template:** any operator-flavored Grafana dashboard that
  templates over the `service` label will work; build it from the metric
  catalogue above.
