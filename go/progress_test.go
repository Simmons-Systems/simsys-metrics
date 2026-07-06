package simsysmetrics

import (
	"context"
	"fmt"
	"regexp"
	"strings"
	"testing"
	"time"
)

// sampleValue returns the float value of a metric line matching name + labels.
// Returns NaN if not found. Labels are matched as substrings (unordered).
func sampleValue(t *testing.T, body, name string, labelSubstrings ...string) float64 {
	t.Helper()
	re := regexp.MustCompile(`^` + regexp.QuoteMeta(name) + `\{([^}]*)\}\s+(\S+)`)
	for _, line := range strings.Split(body, "\n") {
		m := re.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		ok := true
		for _, ls := range labelSubstrings {
			if !strings.Contains(m[1], ls) {
				ok = false
				break
			}
		}
		if !ok {
			continue
		}
		var v float64
		if _, err := fmt.Sscanf(m[2], "%g", &v); err != nil {
			t.Fatalf("parse value from %q: %v", line, err)
		}
		return v
	}
	return -1
}

func TestTrackProgressSeedsGauges(t *testing.T) {
	m := mustInstallForTest(t, "prog-seed")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	tr, err := m.TrackProgress(ctx, ProgressOpts{
		Operation: "scan",
		Total:     100,
		Interval:  50 * time.Millisecond,
	})
	if err != nil {
		t.Fatalf("TrackProgress: %v", err)
	}
	defer tr.Stop()

	body := scrapeMetrics(t, m)
	if got := sampleValue(t, body, "simsys_progress_remaining", `operation="scan"`); got != 100 {
		t.Errorf("remaining = %v; want 100", got)
	}
	if got := sampleValue(t, body, "simsys_progress_rate_per_second", `operation="scan"`); got != 0 {
		t.Errorf("rate = %v; want 0", got)
	}
	if got := sampleValue(t, body, "simsys_progress_estimated_completion_timestamp", `operation="scan"`); got != 0 {
		t.Errorf("eta = %v; want 0", got)
	}
}

func TestTrackProgressIncUpdatesCounter(t *testing.T) {
	m := mustInstallForTest(t, "prog-inc")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	tr, err := m.TrackProgress(ctx, ProgressOpts{
		Operation: "scan",
		Total:     10,
		Interval:  50 * time.Millisecond,
	})
	if err != nil {
		t.Fatalf("TrackProgress: %v", err)
	}
	defer tr.Stop()

	tr.Inc(3)
	tr.Inc(2)

	body := scrapeMetrics(t, m)
	if got := sampleValue(t, body, "simsys_progress_processed_total", `operation="scan"`); got != 5 {
		t.Errorf("processed = %v; want 5", got)
	}

	// Wait for a tick to update remaining.
	deadline := time.Now().Add(1 * time.Second)
	for time.Now().Before(deadline) {
		body = scrapeMetrics(t, m)
		if sampleValue(t, body, "simsys_progress_remaining", `operation="scan"`) == 5 {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("remaining never updated to 5; body:\n%s", body)
}

func TestTrackProgressSetTotalUpdatesRemaining(t *testing.T) {
	m := mustInstallForTest(t, "prog-settotal")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	tr, err := m.TrackProgress(ctx, ProgressOpts{
		Operation: "scan",
		Total:     100,
		Interval:  50 * time.Millisecond,
	})
	if err != nil {
		t.Fatalf("TrackProgress: %v", err)
	}
	defer tr.Stop()

	tr.Inc(10)
	tr.SetTotal(500)

	deadline := time.Now().Add(1 * time.Second)
	for time.Now().Before(deadline) {
		body := scrapeMetrics(t, m)
		if sampleValue(t, body, "simsys_progress_remaining", `operation="scan"`) == 490 {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("remaining never updated to 490")
}

func TestTrackProgressStopIsIdempotent(t *testing.T) {
	m := mustInstallForTest(t, "prog-stop")
	tr, err := m.TrackProgress(context.Background(), ProgressOpts{
		Operation: "scan",
		Total:     10,
		Interval:  50 * time.Millisecond,
	})
	if err != nil {
		t.Fatalf("TrackProgress: %v", err)
	}
	tr.Stop()
	tr.Stop()
}

func TestTrackProgressRejectsBadOpts(t *testing.T) {
	m := mustInstallForTest(t, "prog-bad")
	cases := []struct {
		name string
		opts ProgressOpts
	}{
		{"empty-operation", ProgressOpts{Operation: "", Total: 1}},
		{"negative-total", ProgressOpts{Operation: "x", Total: -1}},
		{"negative-window", ProgressOpts{Operation: "x", Total: 1, Window: -time.Second}},
		{"negative-interval", ProgressOpts{Operation: "x", Total: 1, Interval: -time.Second}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			tr, err := m.TrackProgress(context.Background(), c.opts)
			if err == nil {
				tr.Stop()
				t.Fatalf("expected error for %+v", c.opts)
			}
			if tr != nil {
				t.Fatalf("expected nil tracker on error, got %v", tr)
			}
		})
	}
}

func TestTrackProgressIncRejectsNegative(t *testing.T) {
	m := mustInstallForTest(t, "prog-neg")
	tr, err := m.TrackProgress(context.Background(), ProgressOpts{
		Operation: "x", Total: 10, Interval: 50 * time.Millisecond,
	})
	if err != nil {
		t.Fatalf("TrackProgress: %v", err)
	}
	defer tr.Stop()
	defer func() {
		if recover() == nil {
			t.Fatal("expected panic on negative Inc")
		}
	}()
	tr.Inc(-1)
}
