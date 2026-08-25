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

**Start with the contract, not the code.** `spec/metrics-contract.json` is the
single source of truth; all three lanes read it, and the conformance tests take
their expectations from it. Editing a lane first just makes CI red.

1. **Declare it in `spec/metrics-contract.json`** — name (must carry the
   `simsys_` prefix), type, label list (must include `service`), any enum or
   bucket-schedule references, `tier` (`core` = guaranteed in every runtime;
   `extension` = runtime-specific), and the explicit `runtimes` array.
   Coverage is genuinely three-valued — some metrics live in two of three
   lanes — so `runtimes` is a list, never a boolean.
2. **Implement it in EVERY lane listed in `runtimes`:**
   - Python — `make_counter` / `make_gauge` / `make_histogram` (exported from
     the package root; the prefix guard rejects anything outside `simsys_`)
   - Node — `node/src/registry.ts`
   - Go — the `MakeCounter` / `MakeGauge` / `MakeHistogram` helpers in
     `go/metrics.go`
   A metric declared `tier: core` that is missing from a lane fails CI, because
   `core` is the promise a `$service`-templated dashboard relies on.
3. **Regenerate the README catalogue.** The table in `README.md` is fenced in
   `BEGIN/END GENERATED CATALOGUE` markers and is asserted against the contract
   by `tests/test_catalogue_matches_contract.py`. Do not hand-edit rows.
4. **Add a unit test that exercises the metric end-to-end** in each implementing
   lane. The per-lane conformance tests assert against a *live registry*, never
   against the source that defines the metric — a test that reads the
   definition back agrees with the implementation by construction and can never
   catch a metric that fails to register.
5. **Add a `CHANGELOG.md` entry under `[Unreleased]`** in the root file, plus
   the per-lane changelog for any lane whose behaviour changed.

> **Why the contract exists.** `simsys_pool_*` was implemented in all three
> lanes and documented in none — the old version of this section said to update
> the README catalogue, so the process existed and was simply skipped. Four
> metric families went missing that way. Steps 1 and 3 are now enforced by CI
> rather than by remembering.

> **Changing an existing metric's value or labels is a BREAKING change.** It
> ships alone as a major, never alongside additive work. If two lanes
> legitimately differ for now, declare it in the contract as
> `status: divergent` with a ticket — the divergence count is budgeted, so
> adding one is a deliberate edit a reviewer sees.

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
