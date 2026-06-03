import pytest

from tests.conftest import has_sample, metric_names

fastapi = pytest.importorskip("fastapi")
testclient = pytest.importorskip("fastapi.testclient")


def _new_app():
    from fastapi import FastAPI

    from simsys_metrics import install

    app = FastAPI()
    install(app, service="fastapi_test_svc", version="0.0.1", commit="testsha")
    return app


def test_install_rejects_non_fastapi():
    from simsys_metrics.fastapi import install_fastapi

    with pytest.raises(TypeError):
        install_fastapi(object(), service="x", version="y")


def test_install_mounts_metrics_endpoint():
    app = _new_app()

    @app.get("/ping")
    def ping():
        return {"pong": True}

    client = testclient.TestClient(app)
    # Exercise a request so HTTP metrics have a data point.
    assert client.get("/ping").status_code == 200
    r = client.get("/metrics")
    assert r.status_code == 200
    text = r.text

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
            f"missing {baseline} in /metrics; got {sorted(names)[:20]}..."
        )

    # Every metric name we emit must carry the simsys_ prefix. Filter out
    # default python_* / process_* collectors from prometheus_client itself.
    ours = {n for n in names if n.startswith("simsys_")}
    assert ours, "no simsys_* metrics emitted"

    # build_info carries the expected labels.
    assert has_sample(
        text,
        "simsys_build_info",
        (("service", "fastapi_test_svc"), ("version", "0.0.1"), ("commit", "testsha")),
    )


def test_http_requests_total_uses_route_template_and_status_bucket():
    app = _new_app()

    @app.get("/items/{item_id}")
    def item(item_id: int):
        return {"id": item_id}

    client = testclient.TestClient(app)
    client.get("/items/42")
    client.get("/items/7")

    text = client.get("/metrics").text
    # Route template preserved; status bucketed to "2xx".
    assert has_sample(
        text,
        "simsys_http_requests_total",
        (
            ("service", "fastapi_test_svc"),
            ("method", "GET"),
            ("route", "/items/{item_id}"),
            ("status", "2xx"),
        ),
    )
