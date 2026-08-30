package simsysmetrics

import (
	"bytes"
	"log/slog"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
)

// captureSlog swaps the default slog handler for one writing into a buffer,
// runs fn, and returns everything logged. Restores the previous default.
func captureSlog(t *testing.T, fn func()) string {
	t.Helper()
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{
		Level: slog.LevelDebug,
	})))
	defer slog.SetDefault(prev)
	fn()
	return buf.String()
}

// TestInstallDifferentServiceSameRegistryWarns is the acceptance test named in
// Redmine #50321. A second Install on the SAME Registry under a DIFFERENT
// Service must log the stable marker, and must not change behaviour.
func TestInstallDifferentServiceSameRegistryWarns(t *testing.T) {
	resetServiceIdentityForTest()
	reg := prometheus.NewRegistry()

	out := captureSlog(t, func() {
		if _, err := Install(InstallOpts{
			Service: "first", Version: "1.0.0", Registry: reg,
		}); err != nil {
			t.Fatalf("first Install: %v", err)
		}
		if _, err := Install(InstallOpts{
			Service: "second", Version: "2.0.0", Registry: reg,
		}); err != nil {
			t.Fatalf("second Install: %v", err)
		}
	})

	if !strings.Contains(out, serviceIdentityMarker) {
		t.Errorf("expected the %q marker, got:\n%s", serviceIdentityMarker, out)
	}
	// The message has to name BOTH identities or it cannot be acted on.
	for _, want := range []string{"first", "second"} {
		if !strings.Contains(out, want) {
			t.Errorf("warning does not name %q; got:\n%s", want, out)
		}
	}
}

// The warn must not fire when nothing changed — otherwise the fleet sweep this
// gates is pure noise. This is the negative control for the test above.
func TestInstallSameServiceSameRegistrySilent(t *testing.T) {
	resetServiceIdentityForTest()
	reg := prometheus.NewRegistry()

	out := captureSlog(t, func() {
		for i := 0; i < 3; i++ {
			if _, err := Install(InstallOpts{
				Service: "same", Version: "1.0.0", Registry: reg,
			}); err != nil {
				t.Fatalf("Install %d: %v", i, err)
			}
		}
	})

	if strings.Contains(out, serviceIdentityMarker) {
		t.Errorf("re-Install with an identical Service must be silent, got:\n%s", out)
	}
}

// Two identities in one process are legitimate when they use separate
// Registries — that is the documented escape hatch, so it must stay quiet.
func TestInstallDifferentServiceDifferentRegistriesSilent(t *testing.T) {
	resetServiceIdentityForTest()

	out := captureSlog(t, func() {
		if _, err := Install(InstallOpts{
			Service: "alpha", Version: "1.0.0", Registry: prometheus.NewRegistry(),
		}); err != nil {
			t.Fatalf("alpha Install: %v", err)
		}
		if _, err := Install(InstallOpts{
			Service: "beta", Version: "1.0.0", Registry: prometheus.NewRegistry(),
		}); err != nil {
			t.Fatalf("beta Install: %v", err)
		}
	})

	if strings.Contains(out, serviceIdentityMarker) {
		t.Errorf("separate Registries are the supported escape hatch and must "+
			"not warn, got:\n%s", out)
	}
}

// A caller re-Installing in a loop should produce ONE line per new identity,
// not one per call — same dedupe rule warnIfMissingService follows.
func TestServiceIdentityWarnsOncePerIdentity(t *testing.T) {
	resetServiceIdentityForTest()
	reg := prometheus.NewRegistry()

	out := captureSlog(t, func() {
		if _, err := Install(InstallOpts{
			Service: "first", Version: "1.0.0", Registry: reg,
		}); err != nil {
			t.Fatalf("first Install: %v", err)
		}
		for i := 0; i < 5; i++ {
			if _, err := Install(InstallOpts{
				Service: "second", Version: "1.0.0", Registry: reg,
			}); err != nil {
				t.Fatalf("repeat Install %d: %v", i, err)
			}
		}
	})

	if got := strings.Count(out, serviceIdentityMarker); got != 1 {
		t.Errorf("expected exactly 1 marker across 5 repeat installs, got %d:\n%s",
			got, out)
	}
}

// recordServiceIdentity reports; it does not change which service the
// already-registered collectors emit under. Assert the returned authoritative
// identity stays the first one, so a future change to behaviour reddens here.
func TestServiceIdentityKeepsFirstAuthoritative(t *testing.T) {
	resetServiceIdentityForTest()
	reg := prometheus.NewRegistry()

	if got := recordServiceIdentity(reg, "first"); got != "first" {
		t.Fatalf("first record: want %q, got %q", "first", got)
	}
	if got := recordServiceIdentity(reg, "second"); got != "first" {
		t.Errorf("after a differing Install the first identity must remain "+
			"authoritative: want %q, got %q", "first", got)
	}
}
