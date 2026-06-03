"""Custom Prometheus collector for simsys_process_* metrics.

``prometheus_client`` ships a ProcessCollector by default but its metric names
don't carry the ``simsys_`` prefix and they don't have a ``service`` label.
Rather than re-wrap those, we emit our own via psutil in a single Collector.

Registered once per process by ``install()``. Collectors are idempotent to
register: calling ``register_process_collector(service)`` again with the same
service reuses the existing collector.
"""

from __future__ import annotations

import gc
import threading
from dataclasses import dataclass
from threading import Lock
from typing import Optional

import psutil
from prometheus_client import REGISTRY
from prometheus_client.metrics_core import CounterMetricFamily, GaugeMetricFamily

_lock = Lock()
_collector: Optional["SimsysProcessCollector"] = None


@dataclass(frozen=True)
class ProcessCollectorRollbackState:
    """Snapshot of the process-collector singleton immediately before
    register_process_collector() mutated it.

    Adapter rollback passes this back to restore_process_collector() so
    a service-swap-then-fail sequence doesn't leave a prior install's
    process metrics broken (entire collector unregistered) and a fresh
    registration doesn't leave dangling metrics in the registry.

    ``action`` describes what register_process_collector did:
      * ``"reused"``       — singleton already had this service; nothing changed.
      * ``"registered"``   — no collector existed; we registered fresh. Rollback drops it.
      * ``"service_swap"`` — collector existed but for a different service; we
                              unregistered it, registered a new one. Rollback
                              drops the new one and re-registers the prior.

    ``installed_collector`` is the collector instance THIS install put in
    the singleton slot. Rollback verifies the live ``_collector`` is
    still that exact instance before unregistering — if a concurrent
    install has since swapped the singleton out, rollback no-ops on the
    live collector to avoid clobbering the concurrent install's
    metrics. Without this identity check, restore_process_collector
    can silently unregister a different service's collector.
    """

    action: str  # "reused" | "registered" | "service_swap"
    prior_collector: Optional["SimsysProcessCollector"] = None
    installed_collector: Optional["SimsysProcessCollector"] = None


class SimsysProcessCollector:
    """Emits simsys_process_* and simsys_runtime_* metrics."""

    def __init__(self, service: str) -> None:
        self._service = service
        self._proc = psutil.Process()

    def collect(self):
        try:
            cpu = self._proc.cpu_times()
            cpu_total = float(cpu.user) + float(cpu.system)
        except (psutil.Error, OSError):
            cpu_total = 0.0

        cpu_family = CounterMetricFamily(
            "simsys_process_cpu_seconds_total",
            "Process CPU seconds (user + system) consumed since process start.",
            labels=["service"],
        )
        cpu_family.add_metric([self._service], cpu_total)
        yield cpu_family

        try:
            mem = self._proc.memory_info()
            rss = float(mem.rss)
            vms = float(mem.vms)
        except (psutil.Error, OSError):
            rss = 0.0
            vms = 0.0

        mem_family = GaugeMetricFamily(
            "simsys_process_memory_bytes",
            "Process memory in bytes. type=rss is resident set; type=vms is virtual.",
            labels=["service", "type"],
        )
        mem_family.add_metric([self._service, "rss"], rss)
        mem_family.add_metric([self._service, "vms"], vms)
        yield mem_family

        try:
            num_fds = float(self._proc.num_fds())
        except (psutil.Error, OSError, AttributeError):
            num_fds = 0.0

        fds_family = GaugeMetricFamily(
            "simsys_process_open_fds",
            "Open file descriptors for this process.",
            labels=["service"],
        )
        fds_family.add_metric([self._service], num_fds)
        yield fds_family

        # Thread count
        threads_family = GaugeMetricFamily(
            "simsys_process_threads",
            "Number of active threads in the process.",
            labels=["service"],
        )
        threads_family.add_metric([self._service], float(threading.active_count()))
        yield threads_family

        # GC collections per generation
        gc_family = CounterMetricFamily(
            "simsys_runtime_gc_collections_total",
            "Total garbage collection runs since process start.",
            labels=["service", "generation"],
        )
        try:
            for i, stat in enumerate(gc.get_stats()):
                gc_family.add_metric(
                    [self._service, str(i)], float(stat["collections"])
                )
        except (KeyError, TypeError):
            pass
        yield gc_family


def register_process_collector(
    service: str,
) -> tuple["SimsysProcessCollector", ProcessCollectorRollbackState]:
    """Register (or re-use) the singleton process collector for ``service``.

    Returns ``(collector, rollback_state)`` where ``rollback_state``
    captures exactly what changed so adapter rollback can undo it
    precisely. See :class:`ProcessCollectorRollbackState` for the
    semantics of each action; install() rollback passes the state back
    into :func:`restore_process_collector` on failure.
    """
    global _collector
    with _lock:
        if _collector is not None:
            if _collector._service != service:
                # Service-swap: drop the prior collector but remember it
                # so rollback can re-register it if a later install step
                # fails. Without this capture, a swap-then-fail would
                # leave the PRIOR install's process metrics permanently
                # broken — its collector unregistered, no replacement.
                prior = _collector
                REGISTRY.unregister(prior)
                _collector = SimsysProcessCollector(service)
                REGISTRY.register(_collector)
                return _collector, ProcessCollectorRollbackState(
                    action="service_swap",
                    prior_collector=prior,
                    installed_collector=_collector,
                )
            return _collector, ProcessCollectorRollbackState(
                action="reused", installed_collector=_collector
            )
        _collector = SimsysProcessCollector(service)
        REGISTRY.register(_collector)
        return _collector, ProcessCollectorRollbackState(
            action="registered", installed_collector=_collector
        )


def restore_process_collector(state: ProcessCollectorRollbackState) -> None:
    """Undo whatever register_process_collector() did, given its returned
    rollback state. Called from adapter install rollback when a later
    step fails.

    Identity-checked: only unregisters / replaces the live ``_collector``
    if it's still the exact instance THIS install put there. If a
    concurrent install has swapped the singleton out from under us
    between registration and rollback, leave the live collector alone
    — the concurrent install owns it now, and rolling back the live
    collector would silently break the concurrent install's metrics.
    """
    global _collector
    if state.action == "reused":
        # Singleton was already correct — nothing to undo.
        return
    with _lock:
        if state.action == "registered":
            # We registered fresh. Drop the collector ONLY if the live
            # one is still our instance — a concurrent install may have
            # swapped it out, in which case our rollback isn't entitled
            # to touch the new owner's collector.
            if _collector is not None and _collector is state.installed_collector:
                try:
                    REGISTRY.unregister(_collector)
                except KeyError:
                    pass
                _collector = None
            return
        if state.action == "service_swap":
            # Drop the new collector and re-register the prior one so
            # the previously-installed service's process metrics keep
            # flowing — but only if the live collector is still the
            # one we installed. If a third install has since swapped
            # again, we leave that install's collector in place.
            if _collector is not None and _collector is state.installed_collector:
                try:
                    REGISTRY.unregister(_collector)
                except KeyError:
                    pass
                if state.prior_collector is not None:
                    try:
                        REGISTRY.register(state.prior_collector)
                    except ValueError:
                        # Already registered — shouldn't happen since we
                        # just unregistered our installed collector and
                        # the prior was unregistered at swap time, but
                        # be defensive.
                        pass
                    _collector = state.prior_collector
                else:
                    _collector = None
            return


def unregister_process_collector() -> None:
    """Test-only helper: drop the registered process collector."""
    global _collector
    with _lock:
        if _collector is not None:
            try:
                REGISTRY.unregister(_collector)
            except KeyError:
                pass
            _collector = None
