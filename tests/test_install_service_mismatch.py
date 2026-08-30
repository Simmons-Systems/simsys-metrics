"""install() should warn when re-called with a different service/version.

The previous behavior was silent no-op — which meant test fixtures or
plugins that legitimately wanted re-init under a different service name
got the original install with no signal that their args were ignored.
"""

from __future__ import annotations

import logging

import pytest

fastapi = pytest.importorskip("fastapi")
flask = pytest.importorskip("flask")


def test_fastapi_reinstall_with_different_service_warns(caplog):
    from fastapi import FastAPI

    from simsys_metrics import install

    app = FastAPI()
    install(app, service="first", version="1.0.0")

    with caplog.at_level(logging.WARNING, logger="simsys_metrics"):
        install(app, service="second", version="2.0.0")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("different service/version" in r.getMessage() for r in warnings), (
        f"expected a service-mismatch warning, got: {[r.getMessage() for r in warnings]}"
    )
    # Original service preserved (no-op semantics held).
    assert app.state.simsys_service == "first"
    assert app.state.simsys_version == "1.0.0"


def test_fastapi_reinstall_with_same_args_silent(caplog):
    from fastapi import FastAPI

    from simsys_metrics import install

    app = FastAPI()
    install(app, service="same", version="1.0.0")

    with caplog.at_level(logging.WARNING, logger="simsys_metrics"):
        install(app, service="same", version="1.0.0")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warnings, (
        f"re-install with identical args must be silent, got: {[r.getMessage() for r in warnings]}"
    )


def test_flask_reinstall_with_different_service_warns(caplog):
    from flask import Flask

    from simsys_metrics import install

    app = Flask("flask_mismatch_app")
    install(app, service="first_flask", version="1.0.0")

    with caplog.at_level(logging.WARNING, logger="simsys_metrics"):
        install(app, service="second_flask", version="2.0.0")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("different service/version" in r.getMessage() for r in warnings)
    assert app.extensions["simsys_metrics"]["service"] == "first_flask"
    assert app.extensions["simsys_metrics"]["version"] == "1.0.0"


# ── Different APP OBJECT — the case the per-app sentinel cannot see (#50321) ──
#
# The idempotence guard in fastapi.py / flask.py is keyed on a sentinel stored
# ON THE APP, so two installs against two different app objects never reach it.
# Service identity, though, is process-global (`_SERVICE` in _baseline), so the
# second install silently re-labels everything the first one started. The tests
# above cover re-install on the SAME app; these cover the shape that guard is
# structurally blind to, which is the one that bit the fleet.


def test_fastapi_install_second_app_different_service_logs_marker(caplog):
    from fastapi import FastAPI

    from simsys_metrics import install

    install(FastAPI(), service="app-a", version="1.0.0")

    with caplog.at_level(logging.ERROR, logger="simsys_metrics"):
        install(FastAPI(), service="app-b", version="1.0.0")

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    messages = [r.getMessage() for r in errors]
    assert any("SERVICE IDENTITY CHANGE" in m for m in messages), (
        "a second install() on a DIFFERENT app object with a different service "
        f"must log the marker; got: {messages}"
    )
    # The line has to name both identities or nobody can act on it.
    marker = next(m for m in messages if "SERVICE IDENTITY CHANGE" in m)
    assert "app-a" in marker and "app-b" in marker, (
        f"marker must name prior and new service; got: {marker!r}"
    )


def test_fastapi_install_second_app_same_service_silent(caplog):
    """Negative control: two apps under ONE identity is the supported shape."""
    from fastapi import FastAPI

    from simsys_metrics import install

    install(FastAPI(), service="shared", version="1.0.0")

    with caplog.at_level(logging.DEBUG, logger="simsys_metrics"):
        install(FastAPI(), service="shared", version="1.0.0")

    messages = [r.getMessage() for r in caplog.records]
    assert not any("SERVICE IDENTITY CHANGE" in m for m in messages), (
        f"same service across two apps must be silent; got: {messages}"
    )


def test_install_rollback_set_service_none_is_silent(caplog):
    """set_service(None) is install-rollback, not an identity change."""
    from fastapi import FastAPI

    from simsys_metrics import install
    from simsys_metrics._baseline import set_service

    install(FastAPI(), service="rollback-me", version="1.0.0")

    with caplog.at_level(logging.DEBUG, logger="simsys_metrics"):
        set_service(None)

    messages = [r.getMessage() for r in caplog.records]
    assert not any("SERVICE IDENTITY CHANGE" in m for m in messages), (
        f"set_service(None) must stay quiet; got: {messages}"
    )
