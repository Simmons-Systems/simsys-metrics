package simsysmetrics

// Go-lane conformance against ../spec/metrics-contract.json.
//
// Asserts against a LIVE registry (Gather()) rather than against the
// registration calls in metrics.go, which would be a tautology: the test
// would agree with the implementation by construction and could never catch a
// metric that fails to register.
//
// Three directions:
//  1. every metric the contract says go emits is registered, with the
//     declared type and label names
//  2. every core metric is present -- a core metric missing here is a broken
//     cross-runtime promise, not a Go quirk
//  3. no simsys_-prefixed metric is emitted that the contract does not
//     declare -- the additive-drift direction that was missing entirely, and
//     what would have caught simsys_pool_* shipping in all three lanes while
//     documented in none
//
// The file is read with os.ReadFile at a relative path rather than go:embed,
// because embed cannot reach outside the module root and spec/ is a sibling of
// go/. `go test` runs with cwd set to the package directory, so "../spec/..."
// resolves.

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	dto "github.com/prometheus/client_model/go"
)

type contractMetric struct {
	Type     string              `json:"type"`
	Tier     string              `json:"tier"`
	Runtimes []string            `json:"runtimes"`
	Labels   []string            `json:"labels"`
	Status   string              `json:"status"`
	Current  map[string]struct { // per-runtime reality for a tracked divergence
		Labels []string `json:"labels"`
	} `json:"current"`
}

type contractDoc struct {
	Prefix        string                    `json:"prefix"`
	RequiredLabel string                    `json:"required_label"`
	Metrics       map[string]contractMetric `json:"metrics"`
}

func loadContract(t *testing.T) contractDoc {
	t.Helper()
	path := filepath.Join("..", "spec", "metrics-contract.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("cannot read %s: %v -- every lane's conformance reads this file; "+
			"without it this test would silently assert nothing", path, err)
	}
	var doc contractDoc
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("contract is not valid JSON: %v", err)
	}
	if len(doc.Metrics) == 0 {
		t.Fatal("contract declares no metrics -- refusing to pass vacuously")
	}
	return doc
}

// labelsFor honours a tracked per-runtime divergence: a metric marked
// divergent describes what each lane ACTUALLY emits under `current`, so
// conformance asserts something true and stays green while the divergence is
// tracked (and ticket-gated) separately.
func labelsFor(t *testing.T, name string, m contractMetric) []string {
	t.Helper()
	if m.Status == "divergent" {
		cur, ok := m.Current["go"]
		if !ok {
			t.Fatalf("%s is divergent but has no `current.go` entry", name)
		}
		return cur.Labels
	}
	return m.Labels
}

// emitted drives a real Install plus every opt-in helper, because a collector
// with no samples exposes no label names -- a label comparison against zero
// series passes without testing anything.
func emitted(t *testing.T) map[string]*dto.MetricFamily {
	t.Helper()
	reg := prometheus.NewRegistry()
	m, err := Install(InstallOpts{Service: "contract-conformance", Version: "0.0.0", Registry: reg})
	if err != nil {
		t.Fatalf("install: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	stopQ := m.TrackQueue(ctx, "cq", time.Hour, func() int { return 1 })
	defer stopQ()
	// WaitingFn and Max are both optional and each gates its own gauge, so
	// omitting them leaves simsys_pool_waiting / simsys_pool_max with no
	// series and therefore invisible to Gather().
	stopP := m.TrackPool(ctx, "cp", time.Hour, PoolOpts{
		ActiveFn:  func() int { return 1 },
		IdleFn:    func() int { return 1 },
		WaitingFn: func() int { return 0 },
		Max:       10,
	})
	defer stopP()
	m.TrackJob("cj")()

	// prometheus's Gather() omits a MetricFamily with ZERO series, so a
	// registered-but-undriven *Vec is indistinguishable from an unregistered
	// one. Every family therefore has to be driven at least once or this
	// conformance test reports half the catalogue missing -- a fixture error
	// wearing a contract error's clothes.
	pt, err := m.TrackProgress(ctx, ProgressOpts{Operation: "cop", Total: 1, Interval: time.Hour})
	if err != nil {
		t.Fatalf("track progress: %v", err)
	}
	pt.Inc(1) // a counter with no Inc has no series
	pt.Stop()

	// One real request through the middleware for the HTTP families.
	h := m.Middleware(MiddlewareOpts{})(http.HandlerFunc(
		func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/", nil))

	// simsys_scrape_duration_seconds is only set by MetricsHandler.
	m.MetricsHandler().ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/metrics", nil))

	// TrackQueue/TrackPool poll on a goroutine; give the first tick a moment
	// to land, otherwise their gauges have no series yet.
	time.Sleep(50 * time.Millisecond)

	fams, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	out := map[string]*dto.MetricFamily{}
	for _, f := range fams {
		if n := f.GetName(); len(n) >= 7 && n[:7] == "simsys_" {
			out[n] = f
		}
	}
	return out
}

func typeName(f *dto.MetricFamily) string {
	switch f.GetType() {
	case dto.MetricType_COUNTER:
		return "counter"
	case dto.MetricType_GAUGE:
		return "gauge"
	case dto.MetricType_HISTOGRAM:
		return "histogram"
	default:
		return f.GetType().String()
	}
}

func TestContractMetricsAreRegisteredWithDeclaredTypeAndLabels(t *testing.T) {
	doc := loadContract(t)
	got := emitted(t)

	for name, spec := range doc.Metrics {
		if !contains(spec.Runtimes, "go") {
			continue
		}
		fam, ok := got[name]
		if !ok {
			t.Errorf("contract declares %s for go but the registry does not emit it; "+
				"either implement it or remove \"go\" from its runtimes", name)
			continue
		}
		if tn := typeName(fam); tn != spec.Type {
			t.Errorf("%s: contract says %s, registry emits %s", name, spec.Type, tn)
		}

		want := labelsFor(t, name, spec)
		if len(fam.GetMetric()) == 0 {
			continue // no series yet; presence already asserted above
		}
		var have []string
		for _, l := range fam.GetMetric()[0].GetLabel() {
			have = append(have, l.GetName())
		}
		sort.Strings(have)
		sorted := append([]string(nil), want...)
		sort.Strings(sorted)
		if !equal(have, sorted) {
			t.Errorf("%s: contract declares labels %v, registry emits %v", name, sorted, have)
		}
	}
}

func TestEveryCoreMetricIsPresentInGo(t *testing.T) {
	doc := loadContract(t)
	got := emitted(t)
	for name, spec := range doc.Metrics {
		if spec.Tier != "core" {
			continue
		}
		if _, ok := got[name]; !ok {
			t.Errorf("core metric %s is absent from the Go registry. core is the "+
				"cross-runtime guarantee a $service-templated dashboard relies on "+
				"unconditionally -- demote it to extension or implement it", name)
		}
	}
}

func TestNoUndeclaredSimsysMetricIsEmitted(t *testing.T) {
	doc := loadContract(t)
	for name := range emitted(t) {
		if _, ok := doc.Metrics[name]; !ok {
			t.Errorf("%s is emitted but not declared in spec/metrics-contract.json. "+
				"Add it to the contract and the README catalogues, or stop emitting it.", name)
		}
	}
}

func TestGoDoesNotClaimMetricsItCannotEmit(t *testing.T) {
	// Negative control. Without it, a contract listing every runtime on every
	// metric would satisfy the checks above and look complete while meaning
	// nothing.
	doc := loadContract(t)
	for _, name := range []string{"simsys_process_uptime_seconds"} {
		spec, ok := doc.Metrics[name]
		if !ok {
			t.Fatalf("%s vanished from the contract; this control now checks nothing", name)
		}
		if contains(spec.Runtimes, "go") {
			t.Errorf("%s is a node-only metric but the contract claims go", name)
		}
	}
}

func TestEveryGoMetricCarriesTheRequiredLabel(t *testing.T) {
	doc := loadContract(t)
	for name, spec := range doc.Metrics {
		if !contains(spec.Runtimes, "go") {
			continue
		}
		if !contains(labelsFor(t, name, spec), doc.RequiredLabel) {
			t.Errorf("%s does not carry the mandatory %q label", name, doc.RequiredLabel)
		}
	}
}

func contains(xs []string, s string) bool {
	for _, x := range xs {
		if x == s {
			return true
		}
	}
	return false
}

func equal(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
