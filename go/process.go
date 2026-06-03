package simsysmetrics

import (
	"runtime"
	"sync"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/procfs"
)

// simsysProcessCollector emits simsys_process_* metrics by reading /proc/self.
// Mirrors the Python SimsysProcessCollector at simsys_metrics/_process.py.
// Non-Linux platforms return zeros (procfs.Self() fails gracefully on macOS).
type simsysProcessCollector struct {
	service string

	descCPU        *prometheus.Desc
	descMem        *prometheus.Desc
	descFDs        *prometheus.Desc
	descThreads    *prometheus.Desc
	descGoroutines *prometheus.Desc
	descGCTotal    *prometheus.Desc
	descGCPause    *prometheus.Desc

	mu   sync.Mutex
	proc procfs.Proc
	ok   bool
}

func newSimsysProcessCollector(service string) *simsysProcessCollector {
	c := &simsysProcessCollector{
		service: service,
		descCPU: prometheus.NewDesc(
			"simsys_process_cpu_seconds_total",
			"Process CPU time (user + system) in seconds.",
			[]string{"service"}, nil,
		),
		descMem: prometheus.NewDesc(
			"simsys_process_memory_bytes",
			"Process memory in bytes; type=rss (resident) or vms (virtual).",
			[]string{"service", "type"}, nil,
		),
		descFDs: prometheus.NewDesc(
			"simsys_process_open_fds",
			"Number of open file descriptors.",
			[]string{"service"}, nil,
		),
		descThreads: prometheus.NewDesc(
			"simsys_process_threads",
			"Number of OS threads in the process.",
			[]string{"service"}, nil,
		),
		descGoroutines: prometheus.NewDesc(
			"simsys_runtime_goroutines",
			"Number of live goroutines.",
			[]string{"service"}, nil,
		),
		descGCTotal: prometheus.NewDesc(
			"simsys_runtime_gc_collections_total",
			"Total number of completed GC cycles.",
			[]string{"service"}, nil,
		),
		descGCPause: prometheus.NewDesc(
			"simsys_runtime_gc_pause_total_seconds",
			"Cumulative time spent in GC stop-the-world pauses.",
			[]string{"service"}, nil,
		),
	}
	if proc, err := procfs.Self(); err == nil {
		c.proc = proc
		c.ok = true
	}
	return c
}

func (c *simsysProcessCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- c.descCPU
	ch <- c.descMem
	ch <- c.descFDs
	ch <- c.descThreads
	ch <- c.descGoroutines
	ch <- c.descGCTotal
	ch <- c.descGCPause
}

func (c *simsysProcessCollector) Collect(ch chan<- prometheus.Metric) {
	c.mu.Lock()
	defer c.mu.Unlock()

	// Default values for non-Linux or procfs failure. Keep the series
	// present so Grafana dashboards don't show "no data" for the whole row.
	var cpuSeconds, rssBytes, vmsBytes float64
	var openFDs float64
	var threads float64

	if c.ok {
		if stat, err := c.proc.Stat(); err == nil {
			cpuSeconds = stat.CPUTime()
			rssBytes = float64(stat.ResidentMemory())
			vmsBytes = float64(stat.VirtualMemory())
			threads = float64(stat.NumThreads)
		}
		if fds, err := c.proc.FileDescriptorsLen(); err == nil {
			openFDs = float64(fds)
		}
	}

	ch <- prometheus.MustNewConstMetric(c.descCPU, prometheus.CounterValue, cpuSeconds, c.service)
	ch <- prometheus.MustNewConstMetric(c.descMem, prometheus.GaugeValue, rssBytes, c.service, "rss")
	ch <- prometheus.MustNewConstMetric(c.descMem, prometheus.GaugeValue, vmsBytes, c.service, "vms")
	ch <- prometheus.MustNewConstMetric(c.descFDs, prometheus.GaugeValue, openFDs, c.service)
	ch <- prometheus.MustNewConstMetric(c.descThreads, prometheus.GaugeValue, threads, c.service)

	// Runtime metrics — not platform-dependent, always available.
	ch <- prometheus.MustNewConstMetric(c.descGoroutines, prometheus.GaugeValue, float64(runtime.NumGoroutine()), c.service)
	var memStats runtime.MemStats
	runtime.ReadMemStats(&memStats)
	ch <- prometheus.MustNewConstMetric(c.descGCTotal, prometheus.CounterValue, float64(memStats.NumGC), c.service)
	ch <- prometheus.MustNewConstMetric(c.descGCPause, prometheus.CounterValue, float64(memStats.PauseTotalNs)/1e9, c.service)
}
