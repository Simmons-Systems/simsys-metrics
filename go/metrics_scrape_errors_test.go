package simsysmetrics

import (
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
)

// A collector that always fails to gather, so promhttp's ErrorLog fires.
type brokenCollector struct{ desc *prometheus.Desc }

func (b brokenCollector) Describe(ch chan<- *prometheus.Desc) { ch <- b.desc }
func (b brokenCollector) Collect(ch chan<- prometheus.Metric) {
	ch <- prometheus.NewInvalidMetric(b.desc, errTestGather)
}

var errTestGather = &gatherFailure{}

type gatherFailure struct{}

func (g *gatherFailure) Error() string { return "deliberate gather failure" }

// simsys_scrape_errors_total was registered but never incremented, while
// MetricsHandler's doc comment claimed it was (Redmine #50320). This is the
// discriminating test: it must count 0 before a failing scrape and >0 after.
// Asserting only "the counter exists" would have passed against the bug.
func TestScrapeErrorsTotalIncrementsOnGatherFailure(t *testing.T) {
	m, err := Install(InstallOpts{
		Service: "scrape-err-test", Version: "1.0.0", Registry: prometheus.NewRegistry(),
	})
	if err != nil {
		t.Fatalf("install: %v", err)
	}

	before := readCounter(t, m, "scrape-err-test")
	if before != 0 {
		t.Fatalf("baseline should be 0, got %v", before)
	}

	m.registry.MustRegister(brokenCollector{
		desc: prometheus.NewDesc("simsys_broken_probe", "always fails", nil, nil),
	})

	rec := httptest.NewRecorder()
	m.MetricsHandler().ServeHTTP(rec, httptest.NewRequest("GET", "/metrics", nil))

	after := readCounter(t, m, "scrape-err-test")
	if after <= before {
		t.Fatalf("simsys_scrape_errors_total did not increment on a failing gather: %v -> %v", before, after)
	}
}

func readCounter(t *testing.T, m *Metrics, svc string) float64 {
	t.Helper()
	fams, err := m.registry.Gather()
	if err != nil && !strings.Contains(err.Error(), "deliberate") {
		t.Fatalf("gather: %v", err)
	}
	for _, f := range fams {
		if f.GetName() != "simsys_scrape_errors_total" {
			continue
		}
		for _, mm := range f.GetMetric() {
			for _, l := range mm.GetLabel() {
				if l.GetValue() == svc {
					return mm.GetCounter().GetValue()
				}
			}
		}
	}
	return 0
}
