"""simsys-metrics — drop-in Prometheus /metrics template for Python web apps.

Public API::

    from simsys_metrics import install, track_queue, track_job, safe_label

    install(app, service="my-api", version="1.2.3")           # auto-detects
    install(app, service="my-api", version="1.2.3", commit="abc123")

    track_queue("inference", depth_fn=lambda: q.qsize())
    @track_job("inference")
    def run_inference(...): ...

See the README for the metric catalogue, label allow-list, and cardinality
rules.
"""

from __future__ import annotations

from typing import Optional

from ._baseline import get_service, set_service, track_job, track_pool, track_queue
from .helpers import safe_label
from .progress import ProgressOpts, ProgressTracker, track_progress

__version__ = "0.3.8"

__all__ = [
    "install",
    "track_job",
    "track_pool",
    "track_queue",
    "track_progress",
    "ProgressOpts",
    "ProgressTracker",
    "safe_label",
    "get_service",
    "set_service",
    "__version__",
]


def _is_fastapi(app: object) -> bool:
    try:
        from fastapi import FastAPI
    except ImportError:
        return False
    return isinstance(app, FastAPI)


def _is_flask(app: object) -> bool:
    try:
        from flask import Flask
    except ImportError:
        return False
    return isinstance(app, Flask)


def install(
    app,
    *,
    service: str,
    version: str,
    commit: Optional[str] = None,
    metrics_path: str = "/metrics",
) -> None:
    """Install simsys-metrics on a FastAPI or Flask ``app``.

    The framework is auto-detected. Raises ``TypeError`` if neither FastAPI
    nor Flask is importable or if the object matches neither.
    """
    if not service or not isinstance(service, str):
        raise ValueError("install() requires a non-empty service= keyword argument.")
    if not version or not isinstance(version, str):
        raise ValueError("install() requires a non-empty version= keyword argument.")

    if _is_fastapi(app):
        from .fastapi import install_fastapi

        install_fastapi(
            app,
            service=service,
            version=version,
            commit=commit,
            metrics_path=metrics_path,
        )
        return

    if _is_flask(app):
        from .flask import install_flask

        install_flask(
            app,
            service=service,
            version=version,
            commit=commit,
            metrics_path=metrics_path,
        )
        return

    raise TypeError(
        "simsys_metrics.install(): unrecognised app object. "
        f"Expected fastapi.FastAPI or flask.Flask, got {type(app)!r}. "
        "Install the matching extra (simsys-metrics[fastapi] or [flask]) and pass the framework app."
    )


# `get_service` and `set_service` are imported above and listed in
# __all__ for advanced users who manage metrics without going through
# install(). The previous no-op self-assignment is unnecessary now that
# they're both in __all__.
