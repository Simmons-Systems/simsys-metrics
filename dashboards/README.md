# Grafana dashboard + Prometheus alert pack

Ready-made observability for any service instrumented with `simsys-metrics`,
in any of the three runtimes. Both files template on `service` — the label the
package guarantees on every metric — so one dashboard covers a mixed
Python/Node/Go fleet.

| File | What it is |
|---|---|
| `simsys-services.json` | Grafana dashboard, 9 panels, uid `simsys-services` |
| `simsys-services.rules.yml` | 6 Prometheus alert rules |

These are extracted from a rule set that has been running across ~17 services,
with fleet-specific service names and thresholds removed. The numbers are
starting points, not recommendations.

## Dashboard

Import `simsys-services.json` in Grafana. Three template variables:

| Variable | Meaning |
|---|---|
| `$datasource` | your Prometheus datasource |
| `$job` | your scrape job name (default `simsys`) |
| `$service` | populated from `label_values(simsys_build_info, service)` |

No hand-editing required.

## Alert rules

The rules carry a `{{JOB}}` placeholder for your scrape job name. Substitute it
before loading, or every rule matches nothing:

```bash
sed 's/{{JOB}}/your-job-name/g' simsys-services.rules.yml > /etc/prometheus/rules/simsys.yml
promtool check rules /etc/prometheus/rules/simsys.yml
```

`promtool check rules` on the **rendered** file is the validation step — it
will reject the file with the placeholder still in it, which is the desired
behaviour.

## Three things in here worth keeping even if you rewrite the thresholds

**The minimum-rate guard** on `SimsysLatencySLO`
(`and on(service) ... rate(..._count[10m]) > 0.01`). Without it, a service
receiving almost no traffic produces a p95 computed from a handful of samples,
which swings wildly and alerts constantly. The guard restricts the SLO to
services actually serving traffic.

**`keep_firing_for`** on the same rule. `for` gates the *onset* only —
resolution is immediate the moment the expression stops matching. A service
sitting near its threshold therefore re-alerts on every dip, turning one
ongoing problem into a stream of firing/resolved pairs. `keep_firing_for`
holds the alert open through brief recoveries.

**The `unless on(...)` joins** in `SimsysMetricsEndpointStale` and
`SimsysTargetVanished`. These catch failures that `up == 0` structurally
cannot see: a proxy answering for a dead backend (target is UP, but
`simsys_build_info` is gone), and a service silently removed from service
discovery (nothing for `up` to be 0 about, because the series stopped
existing).

## Two traps to avoid when editing

**Always filter `simsys_process_memory_bytes` on `type="rss"`.** It is the only
memory type every runtime emits — Python and Go also emit `vms`, Node emits
`heapUsed`, `heapTotal` and `external`. An unfiltered query double-counts on
Node, where `heapUsed` ⊂ `heapTotal` ⊂ `rss`.

**A latency threshold above the highest histogram bucket is structurally
unfirable.** `histogram_quantile` can never return more than the top bound.
The schedule runs to 60s (see `spec/metrics-contract.json`). This exact
mistake shipped once: a 15s threshold against a schedule that stopped at 10s,
which could not fire at all.

## Per-service thresholds

Prefer a per-service override to exempting a service — an exemption removes
coverage, an override keeps it. `simsys-services.rules.yml` carries a worked
example under `SimsysLatencySLO`.

## Keeping these honest

`tests/test_dashboard_pack.py` asserts on every PR that every `simsys_` metric
these files reference actually exists in `spec/metrics-contract.json`. A
renamed metric would otherwise leave a panel silently rendering empty, which
looks identical to a quiet service.
