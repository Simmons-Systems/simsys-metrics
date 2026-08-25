// Example: net/http ServeMux with simsys-metrics wired in.
//
// Run:
//
//	go run ./examples/stdlib
//	curl -s http://127.0.0.1:8080/hello
//	curl -s http://127.0.0.1:8080/metrics | head
package main

import (
	"context"
	"log"
	"net/http"
	"time"

	simsys "github.com/Simmons-Systems/simsys-metrics/go/v2"
)

func main() {
	m, err := simsys.Install(simsys.InstallOpts{
		Service: "example-stdlib",
		Version: "0.1.0",
	})
	if err != nil {
		log.Fatal(err)
	}

	mux := http.NewServeMux()
	mux.Handle("/metrics", m.MetricsHandler())
	mux.HandleFunc("/hello", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("hi\n"))
	})

	// Go 1.22+ extractor: r.Pattern is the matched pattern.
	extractor := func(r *http.Request) string {
		if r.Pattern != "" {
			return r.Pattern
		}
		return "unknown"
	}
	handler := m.Middleware(simsys.MiddlewareOpts{Extractor: extractor})(mux)

	// Example of TrackJob in action — this fires once per hello request.
	mux.HandleFunc("/work", func(w http.ResponseWriter, r *http.Request) {
		defer m.TrackJob("demo-work")()
		time.Sleep(50 * time.Millisecond)
		w.Write([]byte("done\n"))
	})

	// Example of TrackProgress + TrackQueue for background work.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	depth := 0
	stopQueue := m.TrackQueue(ctx, "demo-queue", 2*time.Second, func() int { return depth })
	defer stopQueue()

	log.Println("listening on :8080")
	log.Fatal(http.ListenAndServe(":8080", handler))
}
