# Contributing to simsys-metrics

Thanks for considering a contribution. This package is small and opinionated,
but bug reports, clearer docs, and new framework install paths are all
welcome.

## Ground rules

- **Scope:** the package instruments Python web apps with a fixed Prometheus
  metric catalogue. It is deliberately small — we won't add a metric unless
  it's useful across every consumer app.
- **Cardinality first:** any new label or metric has to have a bounded
  cardinality story. "User-controlled free-form string" is an immediate no;
  pipe it through `safe_label()` with an allow-list first.
- **Backwards compatibility:** within `0.x.y`, breaking the public API
  (`install`, `track_queue`, `track_job`, `safe_label`) requires a minor
  version bump and a `CHANGELOG.md` entry flagging it.

## Dev setup

```bash
git clone https://github.com/Simmons-Systems/simsys-metrics.git
cd simsys-metrics
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[fastapi,flask,test]'
```

## Running the tests

```bash
pytest                            # unit + integration, ~0.5s
bin/check-metrics-conformance.sh  # boots demo FastAPI app, scrapes /metrics
```

CI runs the same tests on Python 3.10, 3.11, 3.12, and 3.13 — make sure
they pass locally before opening a PR.

## Adding a metric

1. Define it via `make_counter / make_gauge / make_histogram` in
   `simsys_metrics/_registry.py` (or a sibling file for opt-in metrics).
   The prefix guard will reject anything not starting with `simsys_`.
2. Document labels + cardinality bounds in the metric's docstring and in
   the `Metric catalogue` table in [README.md](README.md).
3. Add a unit test that exercises the metric end-to-end.
4. Update `bin/check-metrics-conformance.sh` if the metric belongs in the
   baseline (visible with zero extra code).
5. Add a `CHANGELOG.md` entry under `[Unreleased]`.

## Adding a framework

The two existing install paths (FastAPI, Flask) live in
`simsys_metrics/fastapi.py` and `simsys_metrics/flask.py`. A new framework
should follow the same shape:

- Module-level `install_<framework>(app, *, service, version, commit=None, metrics_path="/metrics")` function.
- Reuse `_http.http_requests_total` and `_http.http_request_duration_seconds`
  so metric names and buckets stay consistent across frameworks.
- Mount `/metrics` via `prometheus_client.generate_latest()` with the
  `CONTENT_TYPE_LATEST` mimetype.
- Record route *templates*, not raw paths. If the framework doesn't expose
  the template, add a helper that derives it — don't silently use the raw
  path.
- Register auto-detection in `simsys_metrics/__init__.py`'s `install()`
  dispatcher.
- Add `tests/test_<framework>_install.py` covering the same assertions as
  `test_fastapi_install.py` and `test_flask_install.py`.
- List the framework as an optional dependency in `pyproject.toml`:
  `[project.optional-dependencies].<framework> = [...]`.

## PR checklist

- [ ] Tests added/updated; `pytest` is green locally.
- [ ] `bin/check-metrics-conformance.sh` is green (only relevant if the
      change touches FastAPI or baseline metrics).
- [ ] README updated if the public API or metric catalogue changed.
- [ ] CHANGELOG.md updated under `[Unreleased]`.
- [ ] No metric without the `simsys_` prefix; no new unbounded label.

## Code of Conduct

This project follows the org-level
[Contributor Covenant](https://github.com/Simmons-Systems/.github/blob/main/CODE_OF_CONDUCT.md).
Be respectful; assume good faith.
