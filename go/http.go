package simsysmetrics

import "strings"

// StatusBucket returns the HTTP status class string ("1xx", "2xx", ..., "5xx")
// for use as a bounded-cardinality label value.
func StatusBucket(code int) string {
	// ⚡ Bolt: Fast path checking 2xx first as it represents the vast majority
	// of HTTP responses, avoiding unnecessary branch evaluations.
	switch {
	case code >= 200 && code < 300:
		return "2xx"
	case code >= 100 && code < 200:
		return "1xx"
	case code >= 300 && code < 400:
		return "3xx"
	case code >= 400 && code < 500:
		return "4xx"
	default:
		return "5xx"
	}
}

// allowedMethods is the bounded set of HTTP methods that get their own
// label series. RFC 9110 standard methods plus PATCH (RFC 5789).
var allowedMethods = map[string]struct{}{
	"GET":     {},
	"HEAD":    {},
	"POST":    {},
	"PUT":     {},
	"DELETE":  {},
	"CONNECT": {},
	"OPTIONS": {},
	"TRACE":   {},
	"PATCH":   {},
}

// NormalizeMethod returns the upper-cased method if it is a standard
// allow-listed verb, else "OTHER". Prevents attacker-controlled garbage
// methods (e.g. "X_AUDIT_1", "ASDF") from blowing out the
// simsys_http_requests_total label space.
func NormalizeMethod(method string) string {
	upper := strings.ToUpper(method)
	if _, ok := allowedMethods[upper]; ok {
		return upper
	}
	return "OTHER"
}
