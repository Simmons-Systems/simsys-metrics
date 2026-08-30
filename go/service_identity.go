package simsysmetrics

import (
	"log/slog"
	"sync"

	"github.com/prometheus/client_golang/prometheus"
)

// Service identity is per-Registry in the Go lane (per-process in the Python
// and Node lanes, which bind to a module-level registry). Install's doc
// comment has always DESCRIBED what a re-Install under a different Service
// does to the label set; this records it and says so at runtime.
//
// Why a warning and not an error (Redmine #50321): the contract change is
// phased. This release only makes the condition visible, so an existing
// consumer that is doing it today keeps working and shows up in a log sweep;
// promotion to ErrServiceIdentityConflict is gated on that sweep coming back
// clean against a fleet actually running this version.
//
// The marker string is stable and greppable on purpose — it is the thing the
// fleet sweep searches for, and it matches the Python and Node lanes so one
// query covers all three.
const serviceIdentityMarker = "simsys-metrics: SERVICE IDENTITY CHANGE"

var (
	registryServiceMu sync.Mutex
	// Keyed by Registry pointer, holding the FIRST Service installed into it.
	// A registry is a long-lived object owned by the caller, so this map does
	// not grow without bound in any realistic program; it is deliberately not
	// a sync.Map, because the warn-once bookkeeping below needs the same lock.
	registryService = map[*prometheus.Registry]string{}
	// Warn once per (registry, new service) pair. A caller that re-Installs in
	// a loop should get one line, not one per iteration — the same reason
	// warnIfMissingService dedupes.
	registryServiceWarned = map[registryServiceKey]struct{}{}
)

type registryServiceKey struct {
	reg     *prometheus.Registry
	service string
}

// recordServiceIdentity remembers the first Service installed into reg and
// warns if a later Install names a different one. Returns the service that
// remains authoritative for the already-registered collectors, which is
// always the FIRST one — this function reports, it does not change behaviour.
func recordServiceIdentity(reg *prometheus.Registry, service string) string {
	if reg == nil {
		return service
	}
	registryServiceMu.Lock()
	defer registryServiceMu.Unlock()

	prior, seen := registryService[reg]
	if !seen {
		registryService[reg] = service
		return service
	}
	if prior == service {
		return prior
	}

	key := registryServiceKey{reg: reg, service: service}
	if _, warned := registryServiceWarned[key]; warned {
		return prior
	}
	registryServiceWarned[key] = struct{}{}

	slog.Warn(
		serviceIdentityMarker+" — Install called again on the same Registry "+
			"with a different Service. The collectors already registered keep "+
			"emitting under the FIRST service label, so the two identities do "+
			"not separate; only simsys_build_info gains the new value. Use one "+
			"Service per Registry — allocate a fresh prometheus.Registry when "+
			"you need a second identity.",
		"prior_service", prior,
		"new_service", service,
	)
	return prior
}

// resetServiceIdentityForTest clears the per-Registry bookkeeping. Test-only:
// the maps are process-global, so tests that assert on the warning would
// otherwise depend on execution order.
func resetServiceIdentityForTest() {
	registryServiceMu.Lock()
	defer registryServiceMu.Unlock()
	registryService = map[*prometheus.Registry]string{}
	registryServiceWarned = map[registryServiceKey]struct{}{}
}
