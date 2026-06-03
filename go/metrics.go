package simsysmetrics

import (
	"errors"
	"net/http"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// ErrInvalidInstallOpts is returned by Install when required fields are missing.
var ErrInvalidInstallOpts = errors.New("simsys-metrics: Install requires non-empty Service and Version")

// InstallOpts configures Install.
type InstallOpts struct {
	// Service is the service-name label every emitted metric will carry
	// (required). Pick a stable identifier per app, e.g. "my-api".
	Service string

	// Version is the service's semantic version string (required).
	Version string

	// Commit is the short git SHA. If empty, it's detected in this order:
	//   1. SIMSYS_BUILD_COMMIT env var
	//   2. debug.ReadBuildInfo() vcs.revision
	//   3. `git rev-parse --short HEAD` (if git is in PATH)
	//   4. "unknown"
	Commit string

	// Registry is an optional pre-existing prometheus.Registry to attach
	// baseline metrics to. When nil, a fresh private registry is created.
	// Use this when the consumer has app-specific metrics they want served
	// on the same /metrics endpoint — pass their registry here.
	Registry *prometheus.Registry
}

// Metrics is the handle returned by Install. Methods attach metrics to a
// private *prometheus.Registry rather than the global default; consumers
// serve /metrics via MetricsHandler and create their own metrics via
// MakeCounter / MakeGauge / MakeHistogram (prefix-guarded).
type Metrics struct {
	service  string
	registry *prometheus.Registry

	// Baseline metrics — held as struct fields so HTTP middleware can
	// reach them without package-level state.
	buildInfo                  *prometheus.GaugeVec
	httpRequestsTotal          *prometheus.CounterVec
	httpRequestDurationSeconds *prometheus.HistogramVec
	queueDepth                 *prometheus.GaugeVec
	jobsTotal                  *prometheus.CounterVec
	jobDurationSeconds         *prometheus.HistogramVec

	progressProcessedTotal               *prometheus.CounterVec
	progressRemaining                    *prometheus.GaugeVec
	progressRatePerSecond                *prometheus.GaugeVec
	progressEstimatedCompletionTimestamp *prometheus.GaugeVec

	poolActive  *prometheus.GaugeVec
	poolIdle    *prometheus.GaugeVec
	poolWaiting *prometheus.GaugeVec
	poolMax     *prometheus.GaugeVec

	startedAt time.Time
}

// Install wires the simsys baseline into a private registry and returns
// the Metrics handle. Does not mount /metrics on any router — use
// MetricsHandler() for that. Returns an error (does not panic) on
// invalid options.
//
// Idempotent on the same Registry: a second Install with the same
// opts.Registry will reuse the already-registered collectors rather than
// panic on duplicate descriptors. The returned *Metrics still wraps the
// same registry and is safe to use for emitting samples.
//
// CAUTION — re-Install with a DIFFERENT Service on the same Registry
// produces inconsistent metric labels. Process metrics, HTTP histograms,
// queue/job/progress collectors all keep emitting under the FIRST call's
// service label (the existing collectors are reused), but the build_info
// gauge is overwritten with the NEW Service value. Dashboards that join
// build_info to the other simsys_* series via the service label will
// stop matching. Use one Service per Registry; allocate a fresh Registry
// when you legitimately need to re-init under a new service identity.
func Install(opts InstallOpts) (*Metrics, error) {
	if opts.Service == "" || opts.Version == "" {
		return nil, ErrInvalidInstallOpts
	}
	reg := opts.Registry
	if reg == nil {
		reg = prometheus.NewRegistry()
	}
	m := &Metrics{
		service:   opts.Service,
		registry:  reg,
		startedAt: time.Now().UTC(),
	}

	// Baseline HTTP + build-info + job/queue/progress metrics.
	//
	// Use registerOrExistingWithStatus directly for build_info so we can
	// detect a reused-vs-new registration: a re-Install on the same
	// Registry must NOT add a SECOND build_info sample with a fresher
	// `started_at`; the first sample stays canonical.
	warnIfMissingService(
		"simsys_build_info",
		[]string{"service", "version", "commit", "started_at"},
	)
	buildInfoVec := prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: guardName("simsys_build_info"),
			Help: "Service build information. Always equal to 1; read labels for actual data.",
		},
		[]string{"service", "version", "commit", "started_at"},
	)
	var buildInfoIsNew bool
	m.buildInfo, buildInfoIsNew = registerOrExistingWithStatus(reg, buildInfoVec)
	m.httpRequestsTotal = m.MakeCounter(
		"simsys_http_requests_total",
		"Total HTTP requests handled, bucketed by status class.",
		[]string{"service", "method", "route", "status"},
	)
	m.httpRequestDurationSeconds = m.MakeHistogram(
		"simsys_http_request_duration_seconds",
		"HTTP request duration in seconds, labelled by route template.",
		[]string{"service", "method", "route"},
		HTTPBuckets,
	)
	m.queueDepth = m.MakeGauge(
		"simsys_queue_depth",
		"Current depth of an application-owned queue.",
		[]string{"service", "queue"},
	)
	m.jobsTotal = m.MakeCounter(
		"simsys_jobs_total",
		"Jobs completed, labelled by name and outcome (success/error).",
		[]string{"service", "job", "outcome"},
	)
	m.jobDurationSeconds = m.MakeHistogram(
		"simsys_job_duration_seconds",
		"Job duration in seconds.",
		[]string{"service", "job", "outcome"},
		JobBuckets,
	)
	m.progressProcessedTotal = m.MakeCounter(
		"simsys_progress_processed_total",
		"Items completed in a batch operation (monotonic counter).",
		[]string{"service", "operation"},
	)
	m.progressRemaining = m.MakeGauge(
		"simsys_progress_remaining",
		"Items not yet completed in a batch operation.",
		[]string{"service", "operation"},
	)
	m.progressRatePerSecond = m.MakeGauge(
		"simsys_progress_rate_per_second",
		"EWMA-smoothed processing rate in items per second.",
		[]string{"service", "operation"},
	)
	m.progressEstimatedCompletionTimestamp = m.MakeGauge(
		"simsys_progress_estimated_completion_timestamp",
		"Estimated completion time as a Unix timestamp (0 when unknown).",
		[]string{"service", "operation"},
	)
	m.poolActive = m.MakeGauge(
		"simsys_pool_active",
		"Number of active (checked-out) connections in a pool.",
		[]string{"service", "pool"},
	)
	m.poolIdle = m.MakeGauge(
		"simsys_pool_idle",
		"Number of idle connections in a pool.",
		[]string{"service", "pool"},
	)
	m.poolWaiting = m.MakeGauge(
		"simsys_pool_waiting",
		"Number of requests waiting for a pool connection.",
		[]string{"service", "pool"},
	)
	m.poolMax = m.MakeGauge(
		"simsys_pool_max",
		"Maximum pool size.",
		[]string{"service", "pool"},
	)

	// Custom process collector reading /proc/self. Idempotent on the same
	// registry: if Install was called before with this registry, reuse the
	// existing collector rather than panicking on duplicate descriptors or
	// orphaning a freshly-built second-call collector.
	registerOrExisting(reg, newSimsysProcessCollector(opts.Service))

	// Set build_info to 1 with resolved labels — but ONLY when this
	// install actually registered a fresh GaugeVec. If we reused an
	// existing one (Install called twice on the same Registry), skip
	// the Set; the first install's label-set stays canonical and we
	// avoid stacking multiple build_info samples with different
	// started_at values across a second boundary.
	if buildInfoIsNew {
		commit := opts.Commit
		if commit == "" {
			commit = detectCommit()
		}
		m.buildInfo.WithLabelValues(
			opts.Service,
			opts.Version,
			commit,
			m.startedAt.Format(time.RFC3339),
		).Set(1)
	}

	return m, nil
}

// Service returns the service label this Metrics was installed with.
func (m *Metrics) Service() string { return m.service }

// Registry returns the underlying *prometheus.Registry. Useful for consumers
// that want to register their own collectors alongside the simsys baseline.
func (m *Metrics) Registry() *prometheus.Registry { return m.registry }

// MetricsHandler returns an http.Handler that serves the prometheus text
// exposition format from m's registry. Mount it at whatever path matches
// MiddlewareOpts.MetricsPath (default "/metrics").
func (m *Metrics) MetricsHandler() http.Handler {
	return promhttp.HandlerFor(m.registry, promhttp.HandlerOpts{Registry: m.registry})
}

// HTTPBuckets is the shared HTTP latency histogram bucket schedule.
// Identical to Python/Node so cross-app dashboards align.
var HTTPBuckets = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0}

// JobBuckets is the shared job duration histogram bucket schedule.
// Identical to Python/Node so cross-app dashboards align.
var JobBuckets = []float64{0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300}
