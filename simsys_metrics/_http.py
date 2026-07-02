"""Shared HTTP request/duration metrics used by both FastAPI and Flask paths.

Defined once at module level so re-install is cheap and both framework paths
emit metrics with identical names, labels, and buckets.
"""

from __future__ import annotations

from ._registry import make_counter, make_histogram

http_requests_total = make_counter(
    "simsys_http_requests_total",
    "Total HTTP requests handled, bucketed by status class.",
    labelnames=("service", "method", "route", "status"),
)

http_request_duration_seconds = make_histogram(
    "simsys_http_request_duration_seconds",
    "HTTP request duration in seconds, labelled by route template.",
    labelnames=("service", "method", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


def status_bucket(status_code: int | str) -> str:
    """Map a raw HTTP status to its class string (``2xx`` / ``3xx`` / ...)."""
    # ⚡ Bolt: Check strict int type before try/except block. Ordering 2xx before
    # 1xx optimizes the most common valid path.
    if type(status_code) is int:
        if 200 <= status_code < 300:
            return "2xx"
        if 100 <= status_code < 200:
            return "1xx"
        if 300 <= status_code < 400:
            return "3xx"
        if 400 <= status_code < 500:
            return "4xx"
        return "5xx"
    try:
        code = int(status_code)
        if 200 <= code < 300:
            return "2xx"
        if 100 <= code < 200:
            return "1xx"
        if 300 <= code < 400:
            return "3xx"
        if 400 <= code < 500:
            return "4xx"
        return "5xx"
    except (TypeError, ValueError):
        return "5xx"


# Allow-list of HTTP methods that get their own series. Anything outside
# this set (case-insensitive) collapses to ``OTHER`` so attacker-controlled
# garbage methods (e.g. ``X_AUDIT_1``, ``ASDF``) cannot blow out the label
# space. Includes all RFC 9110 standard methods plus PATCH (RFC 5789).
_ALLOWED_METHODS = frozenset(
    {
        "GET",
        "HEAD",
        "POST",
        "PUT",
        "DELETE",
        "CONNECT",
        "OPTIONS",
        "TRACE",
        "PATCH",
    }
)


def normalize_method(method: object) -> str:
    """Coerce a request method into a bounded label value.

    Returns the method upper-cased if it is in the standard allow-list,
    else ``OTHER``. Non-string inputs also return ``OTHER`` defensively.
    """
    if type(method) is str:
        # ⚡ Bolt: Fast path avoids allocating a new uppercase string if the input
        # method is already correctly capitalized and allowed.
        if method in _ALLOWED_METHODS:
            return method
        upper = method.upper()
        return upper if upper in _ALLOWED_METHODS else "OTHER"
    return "OTHER"
