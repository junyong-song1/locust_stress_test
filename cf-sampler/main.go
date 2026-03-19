package main

import (
	"crypto/tls"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"math"
	"net/http"
	"sync"
	"time"
)

// Metrics holds EMA-smoothed and raw latency values
type Metrics struct {
	mu       sync.RWMutex
	HitMS    float64 `json:"hit_ms"`
	MissMS   float64 `json:"miss_ms"`
	HitRaw   float64 `json:"hit_raw"`
	MissRaw  float64 `json:"miss_raw"`
	HitCount int64   `json:"hit_count"`
	MissCount int64  `json:"miss_count"`
	LastUpdate string `json:"last_update"`
}

var (
	metrics Metrics
	alpha   = 0.3 // EMA smoothing factor
)

func ema(old, new float64) float64 {
	if old == 0 {
		return new
	}
	return math.Round((old*(1-alpha)+new*alpha)*10) / 10
}

func sample(client *http.Client, baseURL string) {
	// HIT sample: TM URL (long cache TTL → should be cached)
	hitURL := baseURL + "/playlist_3.m3u8?aws.manifestsettings=time_delay:3600"
	t0 := time.Now()
	resp1, err := client.Get(hitURL)
	ms1 := float64(time.Since(t0).Microseconds()) / 1000.0
	ms1 = math.Round(ms1*10) / 10

	if err == nil {
		defer resp1.Body.Close()
		xCache := resp1.Header.Get("X-Cache")
		if xCache != "" && (len(xCache) >= 3 && xCache[:3] == "Hit") {
			metrics.mu.Lock()
			metrics.HitRaw = ms1
			metrics.HitMS = ema(metrics.HitMS, ms1)
			metrics.HitCount++
			metrics.mu.Unlock()
		}
	}

	// MISS sample: live URL (max-age=1 → likely miss)
	missURL := baseURL + "/playlist_3.m3u8"
	t1 := time.Now()
	resp2, err := client.Get(missURL)
	ms2 := float64(time.Since(t1).Microseconds()) / 1000.0
	ms2 = math.Round(ms2*10) / 10

	if err == nil {
		defer resp2.Body.Close()
		xCache := resp2.Header.Get("X-Cache")
		if xCache != "" && (len(xCache) >= 4 && xCache[:4] == "Miss") {
			metrics.mu.Lock()
			metrics.MissRaw = ms2
			metrics.MissMS = ema(metrics.MissMS, ms2)
			metrics.MissCount++
			metrics.mu.Unlock()
		}
	}

	metrics.mu.Lock()
	metrics.LastUpdate = time.Now().Format("15:04:05")
	metrics.mu.Unlock()
}

func metricsHandler(w http.ResponseWriter, r *http.Request) {
	metrics.mu.RLock()
	defer metrics.mu.RUnlock()

	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Access-Control-Allow-Origin", "*")
	json.NewEncoder(w).Encode(metrics)
}

func resetHandler(w http.ResponseWriter, r *http.Request) {
	metrics.mu.Lock()
	metrics.HitMS = 0
	metrics.MissMS = 0
	metrics.HitRaw = 0
	metrics.MissRaw = 0
	metrics.HitCount = 0
	metrics.MissCount = 0
	metrics.mu.Unlock()

	w.Header().Set("Content-Type", "application/json")
	fmt.Fprintf(w, `{"status":"reset"}`)
}

func main() {
	baseURL := flag.String("url", "", "CloudFront base URL (e.g., https://xxx.cloudfront.net/out/v1/...)")
	interval := flag.Duration("interval", 3*time.Second, "Sampling interval")
	port := flag.Int("port", 9999, "HTTP server port for metrics")
	flag.Float64Var(&alpha, "alpha", 0.3, "EMA smoothing factor (0-1)")
	flag.Parse()

	if *baseURL == "" {
		log.Fatal("--url is required")
	}

	// HTTP client with keep-alive and connection pooling
	client := &http.Client{
		Timeout: 10 * time.Second,
		Transport: &http.Transport{
			MaxIdleConns:        10,
			MaxIdleConnsPerHost: 10,
			IdleConnTimeout:     90 * time.Second,
			TLSClientConfig:     &tls.Config{InsecureSkipVerify: false},
			DisableCompression:  true,
		},
	}

	log.Printf("CF Sampler starting: url=%s interval=%s port=%d alpha=%.1f", *baseURL, *interval, *port, alpha)

	// Sampling loop
	go func() {
		for {
			sample(client, *baseURL)
			time.Sleep(*interval)
		}
	}()

	// HTTP server for metrics
	http.HandleFunc("/metrics", metricsHandler)
	http.HandleFunc("/reset", resetHandler)
	http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, `{"status":"ok"}`)
	})

	addr := fmt.Sprintf(":%d", *port)
	log.Printf("Metrics server listening on %s", addr)
	log.Fatal(http.ListenAndServe(addr, nil))
}
