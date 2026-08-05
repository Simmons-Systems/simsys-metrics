package simsysmetrics

import (
	"sort"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
)

// The HTTP latency bucket schedule is a cross-language contract: Python, Node
// and Go are kept in sync by convention rather than codegen, and nothing pinned
// it — which is how the ceiling came to matter without anyone noticing.
//
// The schedule used to stop at 10.0, so any request slower than that landed in
// +Inf and histogram_quantile could never return more than 10. On the fleet
// that pinned voicestudio's p95 to exactly 10.00 with 23.3% of its requests
// above the ceiling, and left a 15.0s alert threshold on another service
// structurally unable to fire.
//
// Assertions read the bounds a registered histogram actually emits, not a
// literal declared in this file — otherwise they would only test themselves.

var wantHTTPBuckets = []float64{
	0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 30.0, 60.0,
}

// gatherBounds registers a histogram using HTTPBuckets, observes one value, and
// returns the finite upper bounds actually emitted plus their cumulative counts.
func gatherBounds(t *testing.T, observe float64) ([]float64, map[float64]uint64) {
	t.Helper()

	reg := prometheus.NewRegistry()
	h := prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "simsys_http_request_duration_seconds",
		Help:    "test",
		Buckets: HTTPBuckets,
	}, []string{"service"})
	reg.MustRegister(h)
	h.WithLabelValues("bucket_schedule_svc").Observe(observe)

	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}

	var bounds []float64
	counts := map[float64]uint64{}
	for _, fam := range families {
		if fam.GetName() != "simsys_http_request_duration_seconds" {
			continue
		}
		for _, m := range fam.GetMetric() {
			for _, b := range m.GetHistogram().GetBucket() {
				bounds = append(bounds, b.GetUpperBound())
				counts[b.GetUpperBound()] = b.GetCumulativeCount()
			}
		}
	}
	return bounds, counts
}

func TestHTTPBucketScheduleMatchesContract(t *testing.T) {
	bounds, _ := gatherBounds(t, 0.1)
	if len(bounds) != len(wantHTTPBuckets) {
		t.Fatalf("bucket count = %d, want %d (%v)", len(bounds), len(wantHTTPBuckets), bounds)
	}
	for i, b := range bounds {
		if b != wantHTTPBuckets[i] {
			t.Errorf("bucket[%d] = %v, want %v", i, b, wantHTTPBuckets[i])
		}
	}
}

func TestHTTPBucketScheduleExtendsPastTenSeconds(t *testing.T) {
	bounds, _ := gatherBounds(t, 0.1)

	var above []float64
	for _, b := range bounds {
		if b > 10.0 {
			above = append(above, b)
		}
	}
	if len(above) == 0 {
		t.Fatal("schedule must resolve latency above 10s; nothing above the old ceiling")
	}
	want := []float64{15.0, 30.0, 60.0}
	if len(above) != len(want) {
		t.Fatalf("buckets above 10s = %v, want %v", above, want)
	}
	for i := range want {
		if above[i] != want[i] {
			t.Errorf("above[%d] = %v, want %v", i, above[i], want[i])
		}
	}
}

func TestHTTPBucketScheduleIsStrictlyIncreasing(t *testing.T) {
	bounds, _ := gatherBounds(t, 0.1)

	if !sort.Float64sAreSorted(bounds) {
		t.Errorf("bucket bounds are not sorted: %v", bounds)
	}
	seen := map[float64]bool{}
	for _, b := range bounds {
		if seen[b] {
			t.Errorf("duplicate bucket bound %v in %v", b, bounds)
		}
		seen[b] = true
	}
}

// A 12s request must be resolvable in a finite bucket, not only in +Inf —
// the exact blindness the old ceiling created.
func TestSlowRequestResolvableBelowTopBucket(t *testing.T) {
	_, counts := gatherBounds(t, 12.0)

	if got := counts[10.0]; got != 0 {
		t.Errorf("le=10 cumulative count = %d, want 0 (12s must not count at or below 10s)", got)
	}
	if got := counts[15.0]; got != 1 {
		t.Errorf("le=15 cumulative count = %d, want 1 (12s must be resolvable, not just +Inf)", got)
	}
}
