import pytest

from tests.conftest import has_sample, metric_names

flask = pytest.importorskip("flask")


def _new_app():
    from flask import Flask

    from simsys_metrics import install

    app = Flask(__name__)
    install(app, service="flask_test_svc", version="0.0.1", commit="testsha")

    @app.route("/ping")
    def ping():
        return {"pong": True}

    @app.route("/items/<int:item_id>")
    def item(item_id):
        return {"id": item_id}

    return app


def test_install_rejects_non_flask():
    from simsys_metrics.flask import install_flask

    with pytest.raises(TypeError):
        install_flask(object(), service="x", version="y")


def test_flask_metrics_endpoint_has_all_baseline_metrics():
    app = _new_app()
    client = app.test_client()

    assert client.get("/ping").status_code == 200
    assert client.get("/items/42").status_code == 200

    r = client.get("/metrics")
    assert r.status_code == 200
    text = r.get_data(as_text=True)
    names = metric_names(text)

    for baseline in (
        "simsys_http_requests_total",
        "simsys_http_request_duration_seconds",
        "simsys_process_cpu_seconds_total",
        "simsys_process_memory_bytes",
        "simsys_process_open_fds",
        "simsys_process_threads",
        "simsys_runtime_gc_collections_total",
        "simsys_build_info",
    ):
        assert baseline in names or any(n.startswith(baseline) for n in names), (
            f"missing {baseline} in /metrics"
        )

    assert has_sample(
        text,
        "simsys_http_requests_total",
        (
            ("service", "flask_test_svc"),
            ("method", "GET"),
            ("route", "/items/<int:item_id>"),
            ("status", "2xx"),
        ),
    )
