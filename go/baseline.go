package simsysmetrics

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

// -------- TrackJob --------

// TrackJob starts a job-timing span. Call the returned function at the end
// of the work:
//
//	defer m.TrackJob("inference")()    // always success — no error path
//
// For outcome-aware timing where the work might fail, use TrackJobSpan.
func (m *Metrics) TrackJob(job string) func() {
	start := time.Now()
	return func() {
		m.recordJob(job, "success", time.Since(start).Seconds())
	}
}

// TrackJobSpan starts a job-timing span with explicit outcome on finish.
// The returned function accepts an error: nil → outcome="success",
// non-nil → outcome="error". Caller defers or invokes manually:
//
//	finish := m.TrackJobSpan("inference")
//	err := runInference()
//	finish(err)
//	return err
func (m *Metrics) TrackJobSpan(job string) func(err error) {
	start := time.Now()
	var once sync.Once
	return func(err error) {
		once.Do(func() {
			outcome := "success"
			if err != nil {
				outcome = "error"
			}
			m.recordJob(job, outcome, time.Since(start).Seconds())
		})
	}
}

func (m *Metrics) recordJob(job, outcome string, elapsedSec float64) {
	m.jobsTotal.WithLabelValues(m.service, job, outcome).Inc()
	m.jobDurationSeconds.WithLabelValues(m.service, job, outcome).Observe(elapsedSec)
}

// -------- TrackQueue --------

// TrackQueue starts a goroutine that polls depthFn every interval and
// updates simsys_queue_depth{queue=name}. The returned stop function
// is idempotent; callers MUST defer it to avoid goroutine leaks.
//
// The goroutine also respects ctx.Done(): cancelling ctx is equivalent
// to calling stop().
func (m *Metrics) TrackQueue(ctx context.Context, name string, interval time.Duration, depthFn func() int) (stop func()) {
	if interval <= 0 {
		panic(fmt.Sprintf("simsys-metrics: TrackQueue interval must be > 0, got %s", interval))
	}
	done := make(chan struct{})
	var stopOnce sync.Once
	stop = func() {
		stopOnce.Do(func() { close(done) })
	}

	var loggedPanic atomic.Bool

	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		tick := func() {
			// #50319: `ok` gates the write. Before 2.0.0 the Set below sat
			// outside this closure with `depth` initialised to 0, so a
			// panicking depthFn recovered, warned -- and then wrote 0 anyway,
			// indistinguishable from a genuinely drained queue. Go's queue
			// path had the identical defect the ticket recorded for Python
			// and Node.
			depth, ok := 0, false
			func() {
				defer func() {
					if r := recover(); r != nil {
						m.collectorErrors.WithLabelValues(m.service, "queue", name).Inc()
						if loggedPanic.CompareAndSwap(false, true) {
							slog.Warn("simsys-metrics: TrackQueue depthFn panicked; "+
								"the gauge keeps its last known value (absent if this "+
								"was the first tick) and simsys_collector_errors_total "+
								"is incremented",
								"queue", name, "service", m.service, "panic", r)
						}
					}
				}()
				depth = depthFn()
				ok = true
			}()
			if !ok {
				return
			}
			if depth < 0 {
				depth = 0
			}
			m.queueDepth.WithLabelValues(m.service, name).Set(float64(depth))
		}
		tick()
		for {
			select {
			case <-done:
				return
			case <-ctx.Done():
				return
			case <-ticker.C:
				tick()
			}
		}
	}()
	return stop
}

// -------- TrackPool --------

// PoolOpts configures TrackPool.
type PoolOpts struct {
	ActiveFn  func() int
	IdleFn    func() int
	WaitingFn func() int // optional; nil to skip
	Max       int        // 0 means unknown
}

// TrackPool starts a goroutine that polls pool stat callbacks every interval
// and updates simsys_pool_* gauges. The returned stop function is idempotent;
// callers MUST defer it to avoid goroutine leaks.
func (m *Metrics) TrackPool(ctx context.Context, name string, interval time.Duration, opts PoolOpts) (stop func()) {
	if interval <= 0 {
		panic(fmt.Sprintf("simsys-metrics: TrackPool interval must be > 0, got %s", interval))
	}
	done := make(chan struct{})
	var stopOnce sync.Once
	stop = func() {
		stopOnce.Do(func() { close(done) })
	}

	if opts.Max > 0 {
		m.poolMax.WithLabelValues(m.service, name).Set(float64(opts.Max))
	}

	var loggedPanic atomic.Bool

	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		tick := func() {
			// #50319: read EVERY callback before writing ANY gauge. The
			// previous shape interleaved reads and writes inside one recover,
			// so a panicking IdleFn left poolActive already updated for this
			// tick -- a fresh `active` beside a stale `idle`, a
			// self-inconsistent snapshot no consumer can detect.
			type write struct {
				vec   *prometheus.GaugeVec
				value float64
			}
			var writes []write
			ok := false
			func() {
				defer func() {
					if r := recover(); r != nil {
						m.collectorErrors.WithLabelValues(m.service, "pool", name).Inc()
						if loggedPanic.CompareAndSwap(false, true) {
							slog.Warn("simsys-metrics: TrackPool callback panicked; "+
								"the gauges keep their last known values (absent if "+
								"this was the first tick) and "+
								"simsys_collector_errors_total is incremented",
								"pool", name, "service", m.service, "panic", r)
						}
					}
				}()
				writes = append(writes, write{m.poolActive, float64(max(0, opts.ActiveFn()))})
				writes = append(writes, write{m.poolIdle, float64(max(0, opts.IdleFn()))})
				if opts.WaitingFn != nil {
					writes = append(writes, write{m.poolWaiting, float64(max(0, opts.WaitingFn()))})
				}
				ok = true
			}()
			if !ok {
				return
			}
			for _, w := range writes {
				w.vec.WithLabelValues(m.service, name).Set(w.value)
			}
		}
		tick()
		for {
			select {
			case <-done:
				return
			case <-ctx.Done():
				return
			case <-ticker.C:
				tick()
			}
		}
	}()
	return stop
}
