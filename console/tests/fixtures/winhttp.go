// Test-only HTTP server that runs in the Windows installer environment.
package main

import (
	"fmt"
	"net"
	"net/http"
)

func main() {
	http.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		addresses, err := net.LookupHost("linux-peer.endpoint.test")
		title := "Windows HTTP fixture"
		if err == nil && len(addresses) > 0 {
			title += " " + addresses[0]
		}
		fmt.Fprintf(w, "<title>%s</title><p>Served from Windows PE</p>", title)
	})
	if err := http.ListenAndServe(":8080", nil); err != nil {
		panic(err)
	}
}
