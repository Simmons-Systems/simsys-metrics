package simsysmetrics

import (
	"context"
	"regexp"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// #50319 -- a failing poller must not fabricate a value.
//
// Go's TrackQueue had the SAME defect the ticket recorded only for Python and
// Node: the recover() closure caught the panic and warned, but the
// `Set(float64(depth))` call sat OUTSIDE that closure with depth initialised
// to 0, so every panicking tick wrote a confident zero. An empty queue and a
// broken depthFn are operationally opposite, and only one of them ever gets
// investigated.
//
// TrackPool had the neighbouring defect: reads and writes interleaved inside
// one recover, so a panicking IdleFn left poolActive already updated for that
// tick -- a fresh `active` beside a stale `idle`.

// presentSample returns the value of the first sample of `metric` whose text
// contains every fragment in `must`, plus whether such a sample exists at all.
//
// Deliberately NOT progress_test.go's sampleValue(), which returns NaN when a
// sample is missing. Collapsing "absent" into a float is fine there and fatal
// here: absent-vs-present is precisely the distinction #50319 creates, so a
// helper that folded them together would make every assertion below vacuous.
func presentSample(body, metric string, must ...string) (float64, bool) {
	re := regexp.MustCompile(`(?m)^` + regexp.QuoteMeta(metric) + `\{[^}]*\}\s+([0-9.e+-]+)$`)
	for _, line := range strings.Split(body, "\n") {
		if !strings.HasPrefix(line, metric+"{") {
			continue
		}
		ok := true
		for _, frag := range must {
			if !strings.Contains(line, frag) {
				ok = false
				break
			}
		}
		if !ok {
			continue
		}
		if m := re.FindStringSubmatch(line); m != nil {
			v, err := strconv.ParseFloat(m[1], 64)
			if err == nil {
				return v, true
			}
		}
	}
	return 0, false
}

func TestPresentSampleHelperDiscriminates(t *testing.T) {
	// Positive control for the helper every test below depends on. If it
	// always returned (0,false) the "series is absent" assertions would pass
	// against a perfectly working collector.
	body := "simsys_queue_depth{service=\"s\",queue=\"q\"} 7\n"
	if v, ok := presentSample(body, "simsys_queue_depth", `queue="q"`); !ok || v != 7 {
		t.Fatalf("helper failed to read a present sample: v=%v ok=%v", v, ok)
	}
	if _, ok := presentSample(body, "simsys_queue_depth", `queue="absent"`); ok {
		t.Fatalf("helper reported a sample that is not there")
	}
}

func TestTrackQueueLeavesSeriesAbsentWhenFirstTickPanics(t *testing.T) {
	m := mustInstallForTest(t, "queue-first-tick-panic")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	stop := m.TrackQueue(ctx, "broken", 20*time.Millisecond, func() int {
		panic("boom")
	})
	defer stop()
	time.Sleep(80 * time.Millisecond)

	body := scrapeMetrics(t, m)
	if v, ok := presentSample(body, "simsys_queue_depth", `queue="broken"`); ok {
		t.Fatalf("first tick panicked, so simsys_queue_depth must have NO sample "+
			"for this queue -- got %v. Writing 0 here is the #50319 lie.\n%s", v, body)
	}
	if v, ok := presentSample(body, "simsys_collector_errors_total",
		`collector="queue"`, `name="broken"`); !ok || v < 1 {
		t.Fatalf("expected simsys_collector_errors_total to count the failure, "+
			"got v=%v ok=%v\n%s", v, ok, body)
	}
}

func TestTrackQueuePreservesLastKnownValueWhenALaterTickPanics(t *testing.T) {
	m := mustInstallForTest(t, "queue-later-panic")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// atomic: the poller runs on its own goroutine and CI runs `go test -race`.
	// A plain bool here is a genuine data race, and the detector says so.
	healthy := make(chan struct{})
	var broken atomic.Bool
	stop := m.TrackQueue(ctx, "flaky", 20*time.Millisecond, func() int {
		if broken.Load() {
			panic("boom")
		}
		select {
		case <-healthy:
		default:
			close(healthy)
		}
		return 42
	})
	defer stop()

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if v, ok := presentSample(scrapeMetrics(t, m), "simsys_queue_depth", `queue="flaky"`); ok && v == 42 {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if v, ok := presentSample(scrapeMetrics(t, m), "simsys_queue_depth", `queue="flaky"`); !ok || v != 42 {
		t.Fatalf("precondition: gauge must read 42 before we break the callback, got v=%v ok=%v", v, ok)
	}

	broken.Store(true)
	time.Sleep(120 * time.Millisecond)

	body := scrapeMetrics(t, m)
	if v, ok := presentSample(body, "simsys_queue_depth", `queue="flaky"`); !ok || v != 42 {
		t.Fatalf("a panicking tick must leave the last known value in place, "+
			"not reset to 0 -- got v=%v ok=%v\n%s", v, ok, body)
	}
	if v, ok := presentSample(body, "simsys_collector_errors_total",
		`collector="queue"`, `name="flaky"`); !ok || v < 1 {
		t.Fatalf("the stale gauge must be annotated by an error count, got v=%v ok=%v", v, ok)
	}
}

func TestTrackPoolNeverWritesAPartialSnapshot(t *testing.T) {
	m := mustInstallForTest(t, "pool-partial")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var broken atomic.Bool
	var active atomic.Int64
	active.Store(1)
	stop := m.TrackPool(ctx, "partial", 20*time.Millisecond, PoolOpts{
		ActiveFn: func() int { return int(active.Load()) },
		IdleFn: func() int {
			if broken.Load() {
				panic("boom")
			}
			return 10
		},
	})
	defer stop()

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if v, ok := presentSample(scrapeMetrics(t, m), "simsys_pool_idle", `pool="partial"`); ok && v == 10 {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}

	broken.Store(true)
	active.Store(99) // would land under the pre-2.0.0 interleaved shape
	time.Sleep(120 * time.Millisecond)

	body := scrapeMetrics(t, m)
	av, aok := presentSample(body, "simsys_pool_active", `pool="partial"`)
	iv, iok := presentSample(body, "simsys_pool_idle", `pool="partial"`)
	if !aok || !iok || av != 1 || iv != 10 {
		t.Fatalf("a failing pool tick must be all-or-nothing: expected active=1 "+
			"idle=10, got active=%v(%v) idle=%v(%v)\n%s", av, aok, iv, iok, body)
	}
	if v, ok := presentSample(body, "simsys_collector_errors_total",
		`collector="pool"`, `name="partial"`); !ok || v < 1 {
		t.Fatalf("expected a pool collector error count, got v=%v ok=%v", v, ok)
	}
}

func TestTrackPoolLeavesGaugesAbsentWhenFirstTickPanics(t *testing.T) {
	m := mustInstallForTest(t, "pool-first-tick-panic")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	stop := m.TrackPool(ctx, "brokenpool", 20*time.Millisecond, PoolOpts{
		ActiveFn: func() int { panic("boom") },
		IdleFn:   func() int { return 1 },
	})
	defer stop()
	time.Sleep(80 * time.Millisecond)

	body := scrapeMetrics(t, m)
	if v, ok := presentSample(body, "simsys_pool_active", `pool="brokenpool"`); ok {
		t.Fatalf("simsys_pool_active must be absent after a first-tick panic, got %v\n%s", v, body)
	}
	if v, ok := presentSample(body, "simsys_pool_idle", `pool="brokenpool"`); ok {
		t.Fatalf("simsys_pool_idle must be absent after a first-tick panic, got %v\n%s", v, body)
	}
}
