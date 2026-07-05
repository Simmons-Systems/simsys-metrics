package simsysmetrics

// Native Go fuzz targets for the pure label-bucketing helpers. These are
// the functions that face attacker-controlled input (methods, IPs, status
// codes from arbitrary clients), so they're the right surface to fuzz:
// the invariant in every case is bounded-cardinality output that can be
// used safely as a Prometheus label value.
//
// CI runs the seed corpus on every `go test` and a short -fuzz smoke per
// target; run `go test -fuzz=FuzzX` locally for longer exploration.

import (
	"net"
	"regexp"
	"strings"
	"testing"
)

func FuzzStatusBucket(f *testing.F) {
	for _, seed := range []int{-1, 0, 99, 100, 199, 200, 299, 300, 404, 500, 599, 600, 1 << 30} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, code int) {
		got := StatusBucket(code)
		var want string
		switch {
		case code >= 100 && code < 200:
			want = "1xx"
		case code >= 200 && code < 300:
			want = "2xx"
		case code >= 300 && code < 400:
			want = "3xx"
		case code >= 400 && code < 500:
			want = "4xx"
		default:
			want = "5xx"
		}
		if got != want {
			t.Fatalf("StatusBucket(%d) = %q, want %q", code, got, want)
		}
	})
}

func FuzzNormalizeMethod(f *testing.F) {
	for _, seed := range []string{"GET", "get", "Post", "PATCH", "X_AUDIT_1", "", "ASDF", "gEt\x00", "ＧＥＴ"} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, method string) {
		got := NormalizeMethod(method)
		if _, allowed := allowedMethods[got]; !allowed && got != "OTHER" {
			t.Fatalf("NormalizeMethod(%q) = %q: not in the allow set and not OTHER", method, got)
		}
		// Idempotence: normalizing an already-normalized value is a no-op.
		if again := NormalizeMethod(got); again != got {
			t.Fatalf("NormalizeMethod not idempotent: %q -> %q -> %q", method, got, again)
		}
		// Allow-listed verbs must pass through case-insensitively.
		if _, ok := allowedMethods[strings.ToUpper(method)]; ok && got != strings.ToUpper(method) {
			t.Fatalf("NormalizeMethod(%q) = %q, want %q", method, got, strings.ToUpper(method))
		}
	})
}

func FuzzSafeLabel(f *testing.F) {
	f.Add("AAPL", "AAPL", "GOOG")
	f.Add("EVIL", "AAPL", "GOOG")
	f.Add("", "", "x")
	f.Add("other", "a", "b")
	f.Fuzz(func(t *testing.T, value, allowedA, allowedB string) {
		allowed := []string{allowedA, allowedB}
		got := SafeLabel(value, allowed)
		if value == allowedA || value == allowedB {
			if got != value {
				t.Fatalf("SafeLabel(%q, %q) = %q, want passthrough", value, allowed, got)
			}
		} else if got != OtherLabel {
			t.Fatalf("SafeLabel(%q, %q) = %q, want %q", value, allowed, got, OtherLabel)
		}
	})
}

var subnet24Shape = regexp.MustCompile(`^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.0/24$`)

func FuzzIPToSubnet24(f *testing.F) {
	for _, seed := range []string{
		"192.0.2.55", "0.0.0.0", "255.255.255.255", "::1",
		"2001:db8::1", "not-an-ip", "", "192.0.2", "192.0.2.256",
		"１９２.0.2.1", "0177.0.0.1",
	} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, ip string) {
		got := IPToSubnet24(ip)
		if got == OtherLabel {
			// "other" is only legitimate when the input is not a parseable
			// IPv4 address.
			if p := net.ParseIP(ip); p != nil && p.To4() != nil {
				t.Fatalf("IPToSubnet24(%q) = other, but input is valid IPv4", ip)
			}
			return
		}
		m := subnet24Shape.FindStringSubmatch(got)
		if m == nil {
			t.Fatalf("IPToSubnet24(%q) = %q: not 'other' and not a /24 shape", ip, got)
		}
		// Round-trip: the returned network must contain the parsed input.
		p := net.ParseIP(ip)
		if p == nil || p.To4() == nil {
			t.Fatalf("IPToSubnet24(%q) = %q, but input does not parse as IPv4", ip, got)
		}
		_, cidr, err := net.ParseCIDR(got)
		if err != nil || !cidr.Contains(p.To4()) {
			t.Fatalf("IPToSubnet24(%q) = %q: network does not contain input (err=%v)", ip, got, err)
		}
	})
}
