"""Flask install path for simsys-metrics.

Uses ``prometheus_client`` directly — no third-party Flask exporter. A
``before_request`` / ``after_request`` pair records HTTP metrics; a single
``/metrics`` view exposes the default registry.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ._baseline import _peek_service, set_service
from ._registry import scrape_duration_seconds, scrape_errors_total
from ._http import (
    http_request_duration_seconds,
    http_requests_total,
    normalize_method,
    status_bucket,
)
from ._process import (
    ProcessCollectorRollbackState,
    register_process_collector,
    restore_process_collector,
)
from .build_info import register_build_info, unregister_build_info_if_owned

_log = logging.getLogger("simsys_metrics")

# Default exempt paths. Auth middleware can read
# `app.extensions["simsys_metrics"]["exempt_paths"]` to skip the metrics
# endpoint without hard-coding it. The actual set is built per-install
# so a user-supplied `metrics_path` (if non-default) is included.
_DEFAULT_HEALTH_PATHS = frozenset({"/health", "/ready", "/healthz"})
EXEMPT_PATHS = frozenset({"/metrics"}) | _DEFAULT_HEALTH_PATHS


def _build_exempt_paths(metrics_path: str) -> frozenset[str]:
    """Return the auth-exempt path set for an install, including the
    user-supplied metrics_path if it differs from the default."""
    return frozenset({metrics_path}) | _DEFAULT_HEALTH_PATHS


def install_flask(
    app,
    *,
    service: str,
    version: str,
    commit: Optional[str] = None,
    metrics_path: str = "/metrics",
) -> None:
    """Wire simsys baseline metrics into a Flask ``app``."""
    from flask import Flask, request  # local import: flask is an optional extra
    from flask import Response as FlaskResponse

    if not isinstance(app, Flask):
        raise TypeError(f"install_flask expected a flask.Flask, got {type(app)!r}")

    # Idempotent: a second install_flask() on the same app is a no-op.
    app.extensions = getattr(app, "extensions", {}) or {}
    existing = app.extensions.get("simsys_metrics") or {}
    if existing.get("installed"):
        if (existing.get("service"), existing.get("version")) != (service, version):
            _log.warning(
                "simsys_metrics.install() called again on the same Flask app "
                "with different service/version (%r/%r vs %r/%r); the new "
                "values are IGNORED. To re-init, drop "
                "app.extensions['simsys_metrics'] first.",
                existing.get("service"),
                existing.get("version"),
                service,
                version,
            )
        return

    # Set the extensions sentinel BEFORE side effects so consumer code
    # that races against install sees a consistent picture. If any of the
    # side-effecting calls below raises, the sentinel is CLEARED in the
    # except block — otherwise a transient failure (e.g. a `git rev-parse`
    # subprocess timeout in build_info.py) would permanently mark the app
    # as installed without actually wiring metrics, blocking retries.
    app.extensions["simsys_metrics"] = {
        "service": service,
        "version": version,
        "exempt_paths": _build_exempt_paths(metrics_path),
        "installed": True,
    }

    # Snapshot every piece of state install() is about to mutate so the
    # rollback can undo each one. This covers Flask's own state (hooks +
    # routes + view_functions) AND the process-wide prom-client state
    # (service global, process collector, build_info label-set).
    pre_before = list(app.before_request_funcs.get(None, []))
    pre_after = list(app.after_request_funcs.get(None, []))
    pre_url_rules = list(app.url_map.iter_rules())
    pre_view_funcs = dict(app.view_functions)
    pre_service = _peek_service()
    proc_collector_state: Optional[ProcessCollectorRollbackState] = None
    build_info_labels: Optional[tuple[str, str, str, str]] = None
    build_info_was_new = False

    try:
        set_service(service)
        _, proc_collector_state = register_process_collector(service)
        build_info_labels, build_info_was_new = register_build_info(
            service=service, version=version, commit=commit
        )

        @app.before_request
        def _simsys_before():
            # Store start time on the request context via flask.g
            from flask import g

            g.simsys_start = time.perf_counter()

        def _record(method: str, route: str, status: int, start) -> None:
            """Emit one observation. Used by both after_request and the
            got_request_exception handler so unhandled-exception 5xx are
            still counted instead of vanishing silently."""
            http_requests_total.labels(
                service=service,
                method=method,
                route=route,
                status=status_bucket(status),
            ).inc()
            # Skip the histogram observe when before_request never ran
            # (early error handlers, redirect chains): a 0.0 sample would
            # skew the smallest bucket.
            if start is not None:
                elapsed = time.perf_counter() - start
                http_request_duration_seconds.labels(
                    service=service,
                    method=method,
                    route=route,
                ).observe(elapsed)

        @app.after_request
        def _simsys_after(response):
            from flask import g

            if request.path == metrics_path:
                return response
            # If got_request_exception already recorded this request
            # (unhandled exception path that Flask still let through to
            # after_request via an error handler), skip — counting twice
            # would inflate the 5xx series.
            if getattr(g, "simsys_recorded", False):
                return response

            # url_rule.rule is the template ("/items/<id>"); when no rule
            # matched (404, exception path), fall back to a single bucket
            # label so 404 scanner traffic doesn't blow out cardinality.
            rule = request.url_rule
            route = rule.rule if rule is not None else "__unmatched__"
            _record(
                normalize_method(request.method),
                route,
                response.status_code,
                getattr(g, "simsys_start", None),
            )
            g.simsys_recorded = True
            return response

        # Flask invokes do_teardown_request (NOT after_request) for
        # unhandled exceptions, so without this signal handler 5xx caused
        # by uncaught exceptions would never be counted. Subscribe to
        # got_request_exception which fires BEFORE teardown.
        from flask.signals import got_request_exception

        def _simsys_on_exception(sender, exception, **_kwargs):
            from flask import g

            if request.path == metrics_path:
                return
            if getattr(g, "simsys_recorded", False):
                return
            rule = request.url_rule
            route = rule.rule if rule is not None else "__unmatched__"
            _record(
                normalize_method(request.method),
                route,
                500,
                getattr(g, "simsys_start", None),
            )
            g.simsys_recorded = True

        got_request_exception.connect(_simsys_on_exception, app)

        @app.route(metrics_path)
        def _simsys_metrics_view():
            start = time.perf_counter()
            try:
                body = generate_latest()
            except Exception:
                scrape_errors_total.labels(service=service).inc()
                raise
            finally:
                scrape_duration_seconds.labels(service=service).set(
                    time.perf_counter() - start
                )
            return FlaskResponse(body, mimetype=CONTENT_TYPE_LATEST)
    except BaseException:
        # Roll back the sentinel so a retry can attempt a fresh install.
        # Caller still sees the original exception.
        app.extensions.pop("simsys_metrics", None)

        # Truncate any hook + route side effects we added during this
        # install attempt. Without this, a partial install (e.g. the
        # before_request hook was added, but @app.route(metrics_path)
        # then raised because the rule conflicts) leaves the hooks
        # permanently in place — a retry would double-stack them and
        # every request would be counted twice.
        try:
            if None in app.before_request_funcs:
                app.before_request_funcs[None] = pre_before
            if None in app.after_request_funcs:
                app.after_request_funcs[None] = pre_after
        except Exception:  # pragma: no cover — defensive
            pass
        try:
            # Disconnect the got_request_exception signal handler if it
            # was connected. blinker's `connect` returns a receiver ref;
            # the easiest cleanup is to walk the signal's receivers and
            # drop anything bound to this app whose qualname matches
            # our handler.
            from flask.signals import got_request_exception

            for receiver_id in list(got_request_exception.receivers):
                ref = got_request_exception.receivers[receiver_id]
                fn = ref() if callable(ref) else ref
                if (
                    fn is not None
                    and getattr(fn, "__name__", "") == "_simsys_on_exception"
                ):
                    got_request_exception.disconnect(fn, sender=app)
        except Exception:  # pragma: no cover — defensive
            pass
        try:
            # Roll back url_map + view_functions to the pre-install
            # snapshot. Flask's url_map rebuild is normally append-only;
            # we recreate the Map from the pre-install rules.
            from werkzeug.routing import Map

            current_rules = list(app.url_map.iter_rules())
            added_endpoints = {r.endpoint for r in current_rules} - {
                r.endpoint for r in pre_url_rules
            }
            if added_endpoints:
                app.url_map = Map(
                    [r.empty() for r in pre_url_rules],
                    strict_slashes=app.url_map.strict_slashes,
                )
                app.view_functions = pre_view_funcs
        except Exception:  # pragma: no cover — defensive
            pass

        # Undo process-wide prom-client mutations so a retry doesn't
        # leave duplicate simsys_build_info samples and so a
        # service-swap-then-fail doesn't permanently break the prior
        # install's process metrics. See fastapi.py for the full
        # rationale; same shape applies here.
        if build_info_labels is not None:
            try:
                unregister_build_info_if_owned(build_info_labels, build_info_was_new)
            except Exception:  # pragma: no cover — defensive
                pass
        if proc_collector_state is not None:
            try:
                restore_process_collector(proc_collector_state)
            except Exception:  # pragma: no cover — defensive
                pass
        try:
            set_service(pre_service)  # type: ignore[arg-type]
        except Exception:  # pragma: no cover — defensive
            pass
        raise
