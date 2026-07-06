package simsysmetrics

import (
	"context"
	"fmt"
	"sync"
	"time"
)

// ProgressOpts configures TrackProgress.
type ProgressOpts struct {
	// Operation is the label value for every progress metric (required).
	// Multiple concurrent progress trackers with distinct Operation values
	// coexist safely.
	Operation string

	// Total is the denominator for Remaining and ETA. Update via SetTotal
	// on the returned tracker if work grows mid-run.
	Total int64

	// Window is the EWMA smoothing window. Default 5s if zero.
	Window time.Duration

	// Interval is the gauge refresh cadence. Default 5s if zero.
	Interval time.Duration
}

// ProgressTracker is returned by TrackProgress. Caller reports progress
// via Inc and MUST call Stop() (defer) to avoid goroutine leaks.
type ProgressTracker interface {
	// Inc records n items completed.
	Inc(n int64)
	// SetTotal updates the denominator for Remaining and ETA.
	SetTotal(total int64)
	// Stop halts the background ticker and flushes a final state update.
	// Idempotent.
	Stop()
}

type progressTracker struct {
	m         *Metrics
	operation string

	mu             sync.Mutex
	processed      int64
	total          int64
	rateEwma       float64
	rateEwmaValid  bool
	lastProcessed  int64
	lastTickMonoNs int64

	window time.Duration
	done   chan struct{}
	once   sync.Once
}

// TrackProgress starts tracking a batch operation. Returns a tracker the
// caller uses to report progress (Inc) and MUST stop (defer tr.Stop()).
//
// Returns an error on invalid opts (empty Operation, negative Total,
// negative Window/Interval) — opts values frequently come from runtime
// config, so misconfiguration must be handleable rather than fatal.
// (Changed from panicking in v0.3.0.)
func (m *Metrics) TrackProgress(ctx context.Context, opts ProgressOpts) (ProgressTracker, error) {
	if opts.Operation == "" {
		return nil, fmt.Errorf("simsys-metrics: TrackProgress opts.Operation required")
	}
	if opts.Total < 0 {
		return nil, fmt.Errorf("simsys-metrics: TrackProgress opts.Total must be >= 0, got %d", opts.Total)
	}
	if opts.Window == 0 {
		opts.Window = 5 * time.Second
	}
	if opts.Interval == 0 {
		opts.Interval = 5 * time.Second
	}
	if opts.Window <= 0 || opts.Interval <= 0 {
		return nil, fmt.Errorf("simsys-metrics: TrackProgress opts.Window and Interval must be > 0")
	}

	t := &progressTracker{
		m:              m,
		operation:      opts.Operation,
		total:          opts.Total,
		window:         opts.Window,
		lastTickMonoNs: time.Now().UnixNano(),
		done:           make(chan struct{}),
	}

	// Seed gauges so the series exists immediately.
	m.progressRemaining.WithLabelValues(m.service, opts.Operation).Set(float64(opts.Total))
	m.progressRatePerSecond.WithLabelValues(m.service, opts.Operation).Set(0)
	m.progressEstimatedCompletionTimestamp.WithLabelValues(m.service, opts.Operation).Set(0)

	go func() {
		ticker := time.NewTicker(opts.Interval)
		defer ticker.Stop()
		for {
			select {
			case <-t.done:
				return
			case <-ctx.Done():
				return
			case <-ticker.C:
				func() {
					defer func() { _ = recover() }()
					t.tick()
				}()
			}
		}
	}()
	return t, nil
}

func (t *progressTracker) Inc(n int64) {
	if n < 0 {
		panic(fmt.Sprintf("simsys-metrics: ProgressTracker.Inc n must be >= 0, got %d", n))
	}
	if n == 0 {
		return
	}
	t.mu.Lock()
	t.processed += n
	t.mu.Unlock()
	t.m.progressProcessedTotal.WithLabelValues(t.m.service, t.operation).Add(float64(n))
}

func (t *progressTracker) SetTotal(total int64) {
	if total < 0 {
		panic(fmt.Sprintf("simsys-metrics: ProgressTracker.SetTotal must be >= 0, got %d", total))
	}
	t.mu.Lock()
	t.total = total
	t.mu.Unlock()
}

func (t *progressTracker) Stop() {
	t.once.Do(func() {
		close(t.done)
		// Flush a final state update synchronously.
		defer func() { _ = recover() }()
		t.tick()
	})
}

func (t *progressTracker) tick() {
	nowNs := time.Now().UnixNano()
	t.mu.Lock()
	processed := t.processed
	total := t.total
	elapsedSec := float64(nowNs-t.lastTickMonoNs) / 1e9
	if elapsedSec > 0 {
		delta := processed - t.lastProcessed
		instantRate := float64(delta) / elapsedSec
		alpha := elapsedSec / t.window.Seconds()
		if alpha > 1 {
			alpha = 1
		}
		if !t.rateEwmaValid {
			t.rateEwma = instantRate
			t.rateEwmaValid = true
		} else {
			t.rateEwma = alpha*instantRate + (1-alpha)*t.rateEwma
		}
	}
	t.lastProcessed = processed
	t.lastTickMonoNs = nowNs
	rate := t.rateEwma
	t.mu.Unlock()

	remaining := total - processed
	if remaining < 0 {
		remaining = 0
	}
	t.m.progressRemaining.WithLabelValues(t.m.service, t.operation).Set(float64(remaining))
	t.m.progressRatePerSecond.WithLabelValues(t.m.service, t.operation).Set(rate)

	var completionTs float64
	if rate > 0 && remaining > 0 {
		etaSec := float64(remaining) / rate
		completionTs = float64(time.Now().Unix()) + etaSec
	}
	t.m.progressEstimatedCompletionTimestamp.WithLabelValues(t.m.service, t.operation).Set(completionTs)
}
