package simsysmetrics

import (
	"errors"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
)

func TestInstallRequiresServiceAndVersion(t *testing.T) {
	if _, err := Install(InstallOpts{}); !errors.Is(err, ErrInvalidInstallOpts) {
		t.Fatalf("want ErrInvalidInstallOpts, got %v", err)
	}
	if _, err := Install(InstallOpts{Service: "x"}); !errors.Is(err, ErrInvalidInstallOpts) {
		t.Fatalf("missing version: want ErrInvalidInstallOpts, got %v", err)
	}
	if _, err := Install(InstallOpts{Version: "1.0.0"}); !errors.Is(err, ErrInvalidInstallOpts) {
		t.Fatalf("missing service: want ErrInvalidInstallOpts, got %v", err)
	}
}

func TestInstallExposesBaselineOnMetricsHandler(t *testing.T) {
	m := mustInstallForTest(t, "install-baseline")
	body := scrapeMetrics(t, m)

	// prometheus/client_golang only emits HELP/TYPE + samples for metrics that
	// have at least one observed label set. Install sets build_info and the
	// process collector always emits, so those are what we can assert
	// unconditionally. HTTP/queue/job/progress HELP lines appear only after
	// their first observation — covered by their dedicated tests.
	required := []string{
		"simsys_build_info",
		"simsys_process_cpu_seconds_total",
		"simsys_process_memory_bytes",
		"simsys_process_open_fds",
	}
	for _, name := range required {
		if !strings.Contains(body, name) {
			t.Errorf("expected %q in /metrics body; got:\n%s", name, body)
		}
	}
}

func TestInstallBuildInfoCarriesLabels(t *testing.T) {
	m, err := Install(InstallOpts{
		Service: "build-info-test",
		Version: "1.2.3",
		Commit:  "deadbee",
	})
	if err != nil {
		t.Fatalf("Install: %v", err)
	}
	body := scrapeMetrics(t, m)
	for _, want := range []string{
		`service="build-info-test"`,
		`version="1.2.3"`,
		`commit="deadbee"`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("simsys_build_info missing label %q; body:\n%s", want, body)
		}
	}
}

func TestInstallCommitFallbackDetects(t *testing.T) {
	m, err := Install(InstallOpts{
		Service: "commit-detect",
		Version: "1.0.0",
		// Commit left blank to exercise detectCommit fallback.
	})
	if err != nil {
		t.Fatalf("Install: %v", err)
	}
	body := scrapeMetrics(t, m)
	// Some commit label must be present (env, debug.ReadBuildInfo, git, or "unknown").
	if !strings.Contains(body, `commit="`) {
		t.Fatalf("no commit= label in build_info; body:\n%s", body)
	}
}

// Service is the join key with simsys-logevent, which trims its own copy of
// it. An all-whitespace Service must therefore not become a real identity --
// it folds into the existing empty-Service rejection rather than emitting
// series labelled with spaces. (Python and Node warn-and-continue here; Go
// already had a hard error to fold into, so it keeps it.)
func TestInstallTrimsServiceAndVersion(t *testing.T) {
	if _, err := Install(InstallOpts{Service: "   ", Version: "1.0.0"}); !errors.Is(err, ErrInvalidInstallOpts) {
		t.Fatalf("all-whitespace service: want ErrInvalidInstallOpts, got %v", err)
	}
	if _, err := Install(InstallOpts{Service: "svc", Version: "  "}); !errors.Is(err, ErrInvalidInstallOpts) {
		t.Fatalf("all-whitespace version: want ErrInvalidInstallOpts, got %v", err)
	}

	m, err := Install(InstallOpts{
		Service:  "  trim-svc  ",
		Version:  "  1.2.3  ",
		Registry: prometheus.NewRegistry(),
	})
	if err != nil {
		t.Fatalf("padded but valid opts should install: %v", err)
	}
	if m.service != "trim-svc" {
		t.Fatalf("service not trimmed: got %q, want %q", m.service, "trim-svc")
	}

	// Negative control: only the ends are trimmed. If inner whitespace were
	// collapsed too, this would be silently rewriting service names.
	m2, err := Install(InstallOpts{
		Service:  " my svc ",
		Version:  "1.0.0",
		Registry: prometheus.NewRegistry(),
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m2.service != "my svc" {
		t.Fatalf("inner whitespace not preserved: got %q", m2.service)
	}
}
