try:
    import orjson
    def _dumps(obj):
        return orjson.dumps(obj).decode("utf-8")
except ImportError:
    import json
    def _dumps(obj):
        return json.dumps(obj, ensure_ascii=False)

import bisect
import itertools
import json  # keep for json.load/json.dumps in non-hot paths
import random
import re
import time
from collections import deque
from datetime import datetime, timezone
from time import time as timestamp
from urllib.parse import urlparse

import gevent
import threading
from flask import Response, make_response
from flask import request as flask_request
from locust import LoadTestShape, between, constant, constant_pacing, events, task
from locust.contrib.fasthttp import FastHttpUser

import config
from cache_metrics import CacheMetricsCollector
from hls_client import parse_media_playlist

collector = CacheMetricsCollector()

# PDT regex for HLS m3u8
_PDT_RE = re.compile(r'#EXT-X-PROGRAM-DATE-TIME:(\S+)')

# Per-request cache stats for Locust native tab (keyed by request name)
cache_stats = {}

# Recent request URL log (circular buffer) — H5: 50K→10K to save ~55MB/worker
REQUEST_LOG_MAX = 10000
request_log = deque(maxlen=REQUEST_LOG_MAX)
request_log_lock = threading.Lock()

# C2: Pre-compute base path to eliminate urlparse() from hot path
# config.STREAM_PATH 사용 — Locust --host 또는 STREAM_PATH 환경변수로 변경 가능
_BASE_PATH = config.STREAM_PATH  # e.g. "/out/v1/stress-test-ch/..."

# CF → ALB 우회를 위한 Host 헤더 (빈 문자열이면 헤더 미설정)
# config.HOST_HEADER = "aws-kbo-smart-cloudfront.tving.com" 기본값
_HOST_OVERRIDE = (getattr(config, "HOST_HEADER", "") or "").strip()
_DEFAULT_HEADERS = {"Host": _HOST_OVERRIDE} if _HOST_OVERRIDE else {}

# H2: Pre-compute rendition name→label lookup + request name cache
_RENDITION_LABEL = {r["name"]: r.get("label", r["name"]) for r in config.RENDITIONS}
_RENDITION_CODEC = {r["name"]: r["codec"] for r in config.RENDITIONS}
_NAME_CACHE = {}
for _ut in ("live", "timemachine"):
    for _r in config.RENDITIONS:
        _NAME_CACHE[(_ut, _r["name"])] = f"[{_ut}] {_r.get('label', _r['name'])}"

# L1: Pre-compute cumulative weights for bisect (replaces random.choices overhead)
_CUM_WEIGHTS = list(itertools.accumulate(config.RENDITION_WEIGHTS))
_TOTAL_WEIGHT = _CUM_WEIGHTS[-1]

# Current phase tracking
current_phase = {"name": "-", "started_at": None}

# ---------------------------------------------------------------------------
# Metrics Time-Series Collector (periodic snapshots for charts)
# ---------------------------------------------------------------------------
class MetricsTimeSeries:
    """Collects periodic metric snapshots for time-series charts."""

    def __init__(self, max_points=10800):  # 30hr at 10s interval
        self._lock = threading.Lock()
        self._points = deque(maxlen=max_points)

    def add(self, point):
        with self._lock:
            self._points.append(point)

    def get(self, last_n=120):
        with self._lock:
            n = min(last_n, len(self._points))
            start = len(self._points) - n
            return [self._points[i] for i in range(start, len(self._points))]

    def reset(self):
        with self._lock:
            self._points.clear()

metrics_ts = MetricsTimeSeries()


# ---------------------------------------------------------------------------
# Throughput tracker
# ---------------------------------------------------------------------------
class ThroughputTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._total_bytes = 0
        self._window_bytes = 0
        self._window_start = time.time()
        self._per_name_bytes = {}

    def record(self, name, size_bytes):
        with self._lock:
            self._total_bytes += size_bytes
            self._window_bytes += size_bytes
            self._per_name_bytes.setdefault(name, 0)
            self._per_name_bytes[name] += size_bytes

    def get_throughput_mbps(self):
        with self._lock:
            elapsed = time.time() - self._window_start
            if elapsed < 1:
                return self._last_mbps if hasattr(self, "_last_mbps") else 0.0
            mbps = (self._window_bytes * 8) / (elapsed * 1_000_000)
            self._window_bytes = 0
            self._window_start = time.time()
            self._last_mbps = round(mbps, 2)
            return self._last_mbps

    def get_total_bytes(self):
        with self._lock:
            return self._total_bytes

    def get_per_name(self):
        with self._lock:
            return dict(self._per_name_bytes)

    def reset(self):
        with self._lock:
            self._total_bytes = 0
            self._window_bytes = 0
            self._window_start = time.time()
            self._per_name_bytes.clear()

throughput = ThroughputTracker()


# ---------------------------------------------------------------------------
# HIT/MISS Response Time Tracker
# ---------------------------------------------------------------------------
class _AggStats:
    """Accumulates min/max/sum/count without storing individual values."""
    __slots__ = ("min", "max", "sum", "count")

    def __init__(self):
        self.min = float("inf")
        self.max = 0.0
        self.sum = 0.0
        self.count = 0

    def add(self, v):
        self.count += 1
        self.sum += v
        if v < self.min:
            self.min = v
        if v > self.max:
            self.max = v

    def to_dict(self):
        if self.count == 0:
            return {"min": 0, "max": 0, "avg": 0, "count": 0}
        return {
            "min": round(self.min, 1),
            "max": round(self.max, 1),
            "avg": round(self.sum / self.count, 1),
            "count": self.count,
        }


class ResponseTimeTracker:
    """Thread-safe tracker for HIT vs MISS response times.

    Uses running min/max/sum/count instead of storing every value
    to prevent unbounded memory growth at high CCU.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._hit = _AggStats()
        self._miss = _AggStats()
        # Per rendition: {name: {"hit": _AggStats, "miss": _AggStats}}
        self._by_rendition = {}
        # Per codec: {codec: {"hit": _AggStats, "miss": _AggStats}}
        self._by_codec = {}
        # Error tracking
        self._total_requests = 0
        self._error_count = 0

    def _get_group(self, group_dict, key):
        if key not in group_dict:
            group_dict[key] = {"hit": _AggStats(), "miss": _AggStats()}
        return group_dict[key]

    def record(self, response_time_ms, is_hit, rendition=None, codec=None, is_error=False):
        with self._lock:
            self._total_requests += 1
            if is_error:
                self._error_count += 1
                return

            kind = "hit" if is_hit else "miss"
            agg = self._hit if is_hit else self._miss
            agg.add(response_time_ms)
            if rendition:
                self._get_group(self._by_rendition, rendition)[kind].add(response_time_ms)
            if codec:
                self._get_group(self._by_codec, codec)[kind].add(response_time_ms)

    def snapshot(self):
        with self._lock:
            result = {
                "hit": self._hit.to_dict(),
                "miss": self._miss.to_dict(),
                "total_requests": self._total_requests,
                "error_count": self._error_count,
                "error_rate": round(self._error_count / self._total_requests * 100, 3) if self._total_requests > 0 else 0,
            }
            # Per rendition
            rend = {}
            for name in sorted(self._by_rendition):
                g = self._by_rendition[name]
                h, m = g["hit"], g["miss"]
                total = h.count + m.count
                rend[name] = {
                    "hit": h.to_dict(),
                    "miss": m.to_dict(),
                    "hit_rate": round(h.count / total * 100, 1) if total > 0 else 0,
                    "total": total,
                }
            result["by_rendition"] = rend
            # Per codec
            codec = {}
            for c in sorted(self._by_codec):
                g = self._by_codec[c]
                h, m = g["hit"], g["miss"]
                total = h.count + m.count
                codec[c] = {
                    "hit": h.to_dict(),
                    "miss": m.to_dict(),
                    "hit_rate": round(h.count / total * 100, 1) if total > 0 else 0,
                    "total": total,
                }
            result["by_codec"] = codec
            return result

    def reset(self):
        with self._lock:
            self._hit = _AggStats()
            self._miss = _AggStats()
            self._by_rendition.clear()
            self._by_codec.clear()
            self._total_requests = 0
            self._error_count = 0

    def snapshot_and_reset(self):
        """Atomic snapshot + reset to avoid race window between separate calls."""
        with self._lock:
            result = {
                "hit": self._hit.to_dict(),
                "miss": self._miss.to_dict(),
                "total_requests": self._total_requests,
                "error_count": self._error_count,
                "error_rate": round(self._error_count / self._total_requests * 100, 3) if self._total_requests > 0 else 0,
            }
            rend = {}
            for name in sorted(self._by_rendition):
                g = self._by_rendition[name]
                h, m = g["hit"], g["miss"]
                total = h.count + m.count
                rend[name] = {
                    "hit": h.to_dict(), "miss": m.to_dict(),
                    "hit_rate": round(h.count / total * 100, 1) if total > 0 else 0,
                    "total": total,
                }
            result["by_rendition"] = rend
            codec = {}
            for c in sorted(self._by_codec):
                g = self._by_codec[c]
                h, m = g["hit"], g["miss"]
                total = h.count + m.count
                codec[c] = {
                    "hit": h.to_dict(), "miss": m.to_dict(),
                    "hit_rate": round(h.count / total * 100, 1) if total > 0 else 0,
                    "total": total,
                }
            result["by_codec"] = codec
            # Reset while still holding lock
            self._hit = _AggStats()
            self._miss = _AggStats()
            self._by_rendition.clear()
            self._by_codec.clear()
            self._total_requests = 0
            self._error_count = 0
            return result

resp_tracker = ResponseTimeTracker()


# ---------------------------------------------------------------------------
# Origin Req/s Tracker (MISS = origin request)
# ---------------------------------------------------------------------------
class OriginReqTracker:
    """Track MISS count over time windows to calculate Origin Req/s."""

    def __init__(self, window_size=60, bucket_sec=5):
        self._lock = threading.Lock()
        self._bucket_sec = bucket_sec
        self._max_buckets = window_size // bucket_sec
        self._buckets = deque(maxlen=self._max_buckets)  # (timestamp, miss_count, total_count)
        self._current_bucket_ts = 0
        self._current_miss = 0
        self._current_total = 0

    def record(self, is_miss):
        now = int(time.time()) // self._bucket_sec * self._bucket_sec
        with self._lock:
            if now != self._current_bucket_ts:
                if self._current_bucket_ts > 0:
                    self._buckets.append((self._current_bucket_ts, self._current_miss, self._current_total))
                self._current_bucket_ts = now
                self._current_miss = 0
                self._current_total = 0
            self._current_total += 1
            if is_miss:
                self._current_miss += 1

    def snapshot(self):
        """Return time-series of origin req/s and total req/s."""
        with self._lock:
            buckets = list(self._buckets)
            if self._current_bucket_ts > 0:
                buckets.append((self._current_bucket_ts, self._current_miss, self._current_total))

        series = []
        for ts, miss, total in buckets:
            series.append({
                "ts": time.strftime("%H:%M:%S", time.localtime(ts)),
                "origin_rps": round(miss / self._bucket_sec, 1),
                "total_rps": round(total / self._bucket_sec, 1),
            })

        # Current origin req/s (last bucket)
        current_origin_rps = series[-1]["origin_rps"] if series else 0
        current_total_rps = series[-1]["total_rps"] if series else 0

        return {
            "series": series[-24:],  # last 2 minutes (24 x 5s)
            "current_origin_rps": current_origin_rps,
            "current_total_rps": current_total_rps,
        }

    def reset(self):
        with self._lock:
            self._buckets.clear()
            self._current_bucket_ts = 0
            self._current_miss = 0
            self._current_total = 0

origin_tracker = OriginReqTracker()


# ---------------------------------------------------------------------------
# CF Latency Sampler: native thread (outside gevent) for accurate latency
# ---------------------------------------------------------------------------
class CFLatencySampler:
    """Collects CF latency from SamplerUser (shares Locust's connection pool).
    No separate process/thread needed — SamplerUser reports directly.
    Filters out greenlet scheduling noise (>500ms) to show true CF response time.
    """

    HIT_THRESHOLD_MS = 300   # HIT > 300ms = greenlet noise (real CF HIT ~4-50ms)
    MISS_THRESHOLD_MS = 400  # MISS > 400ms = greenlet noise (real CF MISS ~30-100ms)

    def __init__(self, alpha=0.3):
        self._alpha = alpha
        self._lock = threading.Lock()
        self._hit_ms = 0.0
        self._miss_ms = 0.0
        self._hit_raw = 0.0
        self._miss_raw = 0.0

    def _ema(self, old, new):
        if old == 0:
            return new
        return round(old * (1 - self._alpha) + new * self._alpha, 1)

    def record_hit(self, ms):
        with self._lock:
            self._hit_raw = ms
            if ms <= self.HIT_THRESHOLD_MS:
                self._hit_ms = self._ema(self._hit_ms, ms)

    def record_miss(self, ms):
        with self._lock:
            self._miss_raw = ms
            if ms <= self.MISS_THRESHOLD_MS:
                self._miss_ms = self._ema(self._miss_ms, ms)

    def start(self, base_url):
        pass  # SamplerUser handles sampling — no thread/process needed

    def stop(self):
        pass

    def get(self):
        with self._lock:
            return {
                "hit_ms": self._hit_ms,
                "miss_ms": self._miss_ms,
                "hit_raw": self._hit_raw,
                "miss_raw": self._miss_raw,
            }

    def reset(self):
        with self._lock:
            self._hit_ms = 0.0
            self._miss_ms = 0.0
            self._hit_raw = 0.0
            self._miss_raw = 0.0

cf_sampler = CFLatencySampler()


# ---------------------------------------------------------------------------
# Period Tracker: 10-second sliding window for real-time bar
# ---------------------------------------------------------------------------
class PeriodTracker:
    """Track per-interval stats (reset each snapshot) for real-time display."""

    def __init__(self):
        self._lock = threading.Lock()
        self._total = 0
        self._hits = 0
        self._misses = 0
        self._errors = 0
        self._hit_latency_sum = 0.0
        self._hit_latency_count = 0
        self._miss_latency_sum = 0.0
        self._miss_latency_count = 0
        # Actual CF response time (response.elapsed, excludes Locust overhead)
        self._hit_actual_sum = 0.0
        self._hit_actual_count = 0
        self._miss_actual_sum = 0.0
        self._miss_actual_count = 0
        self._total_bytes = 0
        self._last_reset_time = time.time()
        self._snapshot = {}  # last snapshot result

    def record(self, is_hit, is_error, response_time_ms, resp_bytes, actual_ms=None):
        with self._lock:
            self._total += 1
            if is_error:
                self._errors += 1
                return
            self._total_bytes += resp_bytes
            if is_hit:
                self._hits += 1
                self._hit_latency_sum += response_time_ms
                self._hit_latency_count += 1
                if actual_ms is not None:
                    self._hit_actual_sum += actual_ms
                    self._hit_actual_count += 1
            else:
                self._misses += 1
                self._miss_latency_sum += response_time_ms
                self._miss_latency_count += 1
                if actual_ms is not None:
                    self._miss_actual_sum += actual_ms
                    self._miss_actual_count += 1

    def snapshot_and_reset(self, interval_sec=None):
        with self._lock:
            now = time.time()
            # Use actual elapsed time instead of fixed interval
            elapsed = now - self._last_reset_time
            if elapsed < 0.1:
                elapsed = interval_sec or 1.0  # fallback
            total = self._total
            hits = self._hits
            misses = self._misses
            errors = self._errors
            hit_lat_avg = round(self._hit_latency_sum / self._hit_latency_count, 1) if self._hit_latency_count > 0 else 0
            miss_lat_avg = round(self._miss_latency_sum / self._miss_latency_count, 1) if self._miss_latency_count > 0 else 0
            hit_actual_avg = round(self._hit_actual_sum / self._hit_actual_count, 1) if self._hit_actual_count > 0 else 0
            miss_actual_avg = round(self._miss_actual_sum / self._miss_actual_count, 1) if self._miss_actual_count > 0 else 0
            total_bytes = self._total_bytes
            # Reset
            self._total = 0
            self._hits = 0
            self._misses = 0
            self._errors = 0
            self._hit_latency_sum = 0.0
            self._hit_latency_count = 0
            self._miss_latency_sum = 0.0
            self._miss_latency_count = 0
            self._hit_actual_sum = 0.0
            self._hit_actual_count = 0
            self._miss_actual_sum = 0.0
            self._miss_actual_count = 0
            self._total_bytes = 0
            self._last_reset_time = now

        rps = round(total / elapsed, 1) if elapsed > 0 else 0
        hit_rps = round(hits / elapsed, 1) if elapsed > 0 else 0
        miss_rps = round(misses / elapsed, 1) if elapsed > 0 else 0
        hit_rate = round(hits / (hits + misses) * 100, 1) if (hits + misses) > 0 else 0
        error_rate = round(errors / total * 100, 3) if total > 0 else 0
        throughput_mbps = round(total_bytes * 8 / (elapsed * 1_000_000), 2) if elapsed > 0 else 0

        self._snapshot = {
            "ts": time.strftime("%H:%M:%S"),
            "interval_sec": round(elapsed, 2),
            "total_requests": total,
            "hits": hits,
            "misses": misses,
            "errors": errors,
            "rps": rps,
            "hit_rps": hit_rps,
            "miss_rps": miss_rps,
            "hit_rate": hit_rate,
            "hit_avg_ms": hit_lat_avg,
            "miss_avg_ms": miss_lat_avg,
            "hit_actual_ms": hit_actual_avg,
            "miss_actual_ms": miss_actual_avg,
            "error_rate": error_rate,
            "throughput_mbps": throughput_mbps,
        }
        return self._snapshot

    def get_last_snapshot(self):
        return self._snapshot

    def reset(self):
        with self._lock:
            self._total = 0
            self._hits = 0
            self._misses = 0
            self._errors = 0
            self._hit_latency_sum = 0.0
            self._hit_latency_count = 0
            self._miss_latency_sum = 0.0
            self._miss_latency_count = 0
            self._hit_actual_sum = 0.0
            self._hit_actual_count = 0
            self._miss_actual_sum = 0.0
            self._miss_actual_count = 0
            self._total_bytes = 0
            self._last_reset_time = time.time()
            self._snapshot = {}

period_tracker = PeriodTracker()


# ---------------------------------------------------------------------------
# Response Time Histogram: counts per latency bucket (m3u8 응답속도 분포)
# ---------------------------------------------------------------------------
class ResponseTimeHistogram:
    """Cumulative latency distribution — resets on stats reset."""
    BUCKETS = [500, 1000, 2000, 3000]   # ms upper bounds
    LABELS  = ["≤500ms", "500ms-1s", "1s-2s", "2s-3s", "3s+"]

    def __init__(self):
        self._lock = threading.Lock()
        n = len(self.BUCKETS) + 1
        self._total  = [0] * n
        self._hits   = [0] * n
        self._misses = [0] * n

    def record(self, ms: float, is_hit: bool):
        idx = len(self.BUCKETS)
        for i, thr in enumerate(self.BUCKETS):
            if ms <= thr:
                idx = i
                break
        with self._lock:
            self._total[idx] += 1
            if is_hit:
                self._hits[idx] += 1
            else:
                self._misses[idx] += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "labels":  self.LABELS,
                "total":   list(self._total),
                "hits":    list(self._hits),
                "misses":  list(self._misses),
            }

    def merge(self, snap: dict):
        with self._lock:
            for i, v in enumerate(snap.get("total",  [])):
                if i < len(self._total):   self._total[i]  += v
            for i, v in enumerate(snap.get("hits",   [])):
                if i < len(self._hits):    self._hits[i]   += v
            for i, v in enumerate(snap.get("misses", [])):
                if i < len(self._misses):  self._misses[i] += v

    def reset(self):
        with self._lock:
            n = len(self.BUCKETS) + 1
            self._total  = [0] * n
            self._hits   = [0] * n
            self._misses = [0] * n

resp_histogram = ResponseTimeHistogram()       # rendition m3u8 응답시간
seg_histogram = ResponseTimeHistogram()        # segment HEAD 응답시간


# ---------------------------------------------------------------------------
# M3U8 응답시간 1초 버킷 히스토그램 (master / child 전용, 막대차트용)
# "1초 이내, 1~2초, 2~3초, 3~4초, 4초+" 버킷 건수 집계
# ---------------------------------------------------------------------------
class SecondBucketHistogram:
    BUCKETS = getattr(config, "RESP_BUCKETS_MS", [1000, 2000, 3000, 4000])
    LABELS  = getattr(config, "RESP_BUCKET_LABELS", ["≤1s", "1~2s", "2~3s", "3~4s", "4s+"])

    def __init__(self):
        self._lock = threading.Lock()
        n = len(self.BUCKETS) + 1
        self._ok  = [0] * n
        self._err = [0] * n

    def record(self, ms: float, is_error: bool = False):
        idx = len(self.BUCKETS)
        for i, thr in enumerate(self.BUCKETS):
            if ms <= thr:
                idx = i
                break
        with self._lock:
            if is_error:
                self._err[idx] += 1
            else:
                self._ok[idx] += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "labels": list(self.LABELS),
                "ok":     list(self._ok),
                "error":  list(self._err),
                "total":  [o + e for o, e in zip(self._ok, self._err)],
            }

    def merge(self, snap: dict):
        with self._lock:
            for i, v in enumerate(snap.get("ok", [])):
                if i < len(self._ok):  self._ok[i]  += v
            for i, v in enumerate(snap.get("error", [])):
                if i < len(self._err): self._err[i] += v

    def reset(self):
        with self._lock:
            n = len(self.BUCKETS) + 1
            self._ok  = [0] * n
            self._err = [0] * n

master_sec_hist = SecondBucketHistogram()   # master m3u8 1초버킷
child_sec_hist  = SecondBucketHistogram()   # child(rendition) m3u8 1초버킷


# ---------------------------------------------------------------------------
# Master m3u8 latency tracker: sliding-window percentiles (p50/p95/p99)
# SSAI 환경에서는 hit rate 지표가 무의미 → master 응답속도가 핵심 KPI
# ---------------------------------------------------------------------------
class MasterLatencyTracker:
    def __init__(self, maxlen: int = 5000):
        self._lock = threading.Lock()
        self._samples = deque(maxlen=maxlen)
        self._total = 0
        self._error = 0

    def record(self, ms: float, is_error: bool = False):
        with self._lock:
            self._total += 1
            if is_error:
                self._error += 1
                return
            self._samples.append(float(ms))

    def snapshot(self) -> dict:
        with self._lock:
            s = sorted(self._samples)
            total = self._total
            err = self._error
        n = len(s)
        if n == 0:
            return {"total": total, "error_count": err,
                    "error_rate": round(err / total * 100, 3) if total else 0,
                    "count": 0, "p50": 0, "p95": 0, "p99": 0, "avg": 0, "min": 0, "max": 0}
        def pct(p):
            idx = min(n - 1, int(n * p))
            return round(s[idx], 1)
        return {
            "total": total, "error_count": err,
            "error_rate": round(err / total * 100, 3) if total else 0,
            "count": n,
            "p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99),
            "avg": round(sum(s) / n, 1),
            "min": round(s[0], 1), "max": round(s[-1], 1),
        }

    def snapshot_raw(self) -> dict:
        with self._lock:
            return {"samples": list(self._samples), "total": self._total, "error_count": self._error}

    def merge(self, other: dict):
        with self._lock:
            for v in other.get("samples", []):
                self._samples.append(float(v))
            self._total += other.get("total", 0)
            self._error += other.get("error_count", 0)

    def reset(self):
        with self._lock:
            self._samples.clear()
            self._total = 0
            self._error = 0

master_latency = MasterLatencyTracker()
child_latency = MasterLatencyTracker()  # rendition(child) m3u8 p50/p95/p99


# ---------------------------------------------------------------------------
# Distributed mode: Worker → Master data sync
# ---------------------------------------------------------------------------
# Worker-local buffers (reset after each report)
_worker_cache_stats = {}
_worker_request_log_buf = deque(maxlen=200)
_worker_lock = threading.Lock()


@events.report_to_master.add_listener
def on_report_to_master(client_id, data):
    """Worker sends its collected data to master."""
    with _worker_lock:
        # cache_stats snapshot — deep copy then reset to avoid double-counting
        data["custom_cache_stats"] = {k: dict(v) for k, v in cache_stats.items()}
        cache_stats.clear()
        # H1+M5: request_log — only copy last 50 entries (master takes 50 anyway)
        with request_log_lock:
            n = len(request_log)
            start = max(0, n - 50)
            data["custom_request_log"] = [request_log[i] for i in range(start, n)]
        # H6: atomic snapshot+reset to avoid race window
        data["custom_resp"] = resp_tracker.snapshot_and_reset()
        # origin_tracker snapshot
        data["custom_origin"] = origin_tracker.snapshot()
        # C3: reset cumulative only — preserve periodic stats for console reporter
        data["custom_collector"] = collector.snapshot_cumulative()
        collector.reset_cumulative()
        # throughput
        data["custom_throughput_bytes"] = throughput.get_total_bytes()
        # period tracker — snapshot_and_reset so worker accumulates fresh data each report
        data["custom_period"] = period_tracker.snapshot_and_reset(config.CACHE_REPORT_INTERVAL)
        # CF sampler data from SamplerUser (only the worker running SamplerUser has data)
        data["custom_cf_sampler"] = cf_sampler.get()
        # response time histograms — snapshot+reset per report cycle
        data["custom_histogram"] = resp_histogram.snapshot()
        resp_histogram.reset()
        data["custom_seg_histogram"] = seg_histogram.snapshot()
        seg_histogram.reset()
        data["custom_master_latency"] = master_latency.snapshot_raw()
        master_latency.reset()
        data["custom_child_latency"] = child_latency.snapshot_raw()
        child_latency.reset()
        # 1초 버킷 히스토그램 (master/child m3u8 응답분포)
        data["custom_master_sec_hist"] = master_sec_hist.snapshot()
        master_sec_hist.reset()
        data["custom_child_sec_hist"] = child_sec_hist.snapshot()
        child_sec_hist.reset()
        # reset tracking
        data["custom_reset_ts"] = _worker_reset_ts


# Master-side aggregation buffers
_master_cache_stats = {}
_master_request_log = deque(maxlen=REQUEST_LOG_MAX)
_master_resp = None
_master_origin_by_worker = {}   # client_id -> latest origin snapshot
_master_collector = None
_master_period_by_worker = {}  # client_id -> latest period snapshot
_master_histogram = ResponseTimeHistogram()       # rendition 집계
_master_seg_histogram = ResponseTimeHistogram()   # segment 집계
_master_master_latency = MasterLatencyTracker()   # master m3u8 p50/p95/p99
_master_child_latency = MasterLatencyTracker()    # child m3u8 p50/p95/p99
_master_master_sec_hist = SecondBucketHistogram()  # master m3u8 1초버킷 집계
_master_child_sec_hist  = SecondBucketHistogram()  # child m3u8 1초버킷 집계
_master_lock = threading.Lock()
_master_reset_ts = 0  # timestamp of last reset
_worker_reset_ts = 0  # worker-side reset tracking


@events.worker_report.add_listener
def on_worker_report(client_id, data):
    """Master receives data from a worker and merges it."""
    global _master_resp, _master_collector

    # Ignore stale data from before last reset (5s tolerance for distributed event timing)
    worker_rts = data.get("custom_reset_ts", 0)
    if _master_reset_ts > 0 and worker_rts < (_master_reset_ts - 5):
        return  # Worker hasn't processed reset yet, skip stale data

    # Merge cache_stats (accumulate, not overwrite)
    wcs = data.get("custom_cache_stats", {})
    for name, stats in wcs.items():
        if name not in _master_cache_stats:
            _master_cache_stats[name] = {"hit": 0, "miss": 0, "noinfo": 0, "ages": [], "last_pop": "-"}
        _master_cache_stats[name]["hit"] += stats.get("hit", 0)
        _master_cache_stats[name]["miss"] += stats.get("miss", 0)
        _master_cache_stats[name]["noinfo"] += stats.get("noinfo", 0)
        _master_cache_stats[name]["ages"] = (_master_cache_stats[name]["ages"] + stats.get("ages", []))[-100:]
        _master_cache_stats[name]["last_pop"] = stats.get("last_pop", "-")

    # Merge request_log (append recent)
    wrl = data.get("custom_request_log", [])
    with request_log_lock:
        for entry in wrl[-50:]:
            _master_request_log.append(entry)

    # Store latest resp_tracker & origin snapshots (per-worker overwrite, good enough)
    with _master_lock:
        wr = data.get("custom_resp")
        if wr:
            if _master_resp is None:
                _master_resp = wr
            else:
                # Merge hit/miss counts and times (weighted average)
                for key in ("hit", "miss"):
                    old_count = _master_resp[key].get("count", 0)
                    new_count = wr[key].get("count", 0)
                    merged_count = old_count + new_count
                    if new_count > 0:
                        old_avg = _master_resp[key].get("avg", 0)
                        new_avg = wr[key]["avg"]
                        _master_resp[key]["avg"] = round((old_avg * old_count + new_avg * new_count) / merged_count, 1) if merged_count > 0 else 0
                        _master_resp[key]["min"] = min(_master_resp[key].get("min", 999999), wr[key].get("min", 999999))
                        _master_resp[key]["max"] = max(_master_resp[key].get("max", 0), wr[key].get("max", 0))
                    _master_resp[key]["count"] = merged_count
                _master_resp["total_requests"] = _master_resp.get("total_requests", 0) + wr.get("total_requests", 0)
                _master_resp["error_count"] = _master_resp.get("error_count", 0) + wr.get("error_count", 0)
                if _master_resp["total_requests"] > 0:
                    _master_resp["error_rate"] = round(_master_resp["error_count"] / _master_resp["total_requests"] * 100, 3)
                # Merge by_rendition, by_codec
                for group_key in ("by_rendition", "by_codec"):
                    existing = _master_resp.get(group_key, {})
                    incoming = wr.get(group_key, {})
                    for k, v in incoming.items():
                        if k not in existing:
                            existing[k] = v
                        else:
                            for hm in ("hit", "miss"):
                                old_count = existing[k][hm].get("count", 0)
                                new_count = v[hm].get("count", 0)
                                merged_count = old_count + new_count
                                existing[k][hm]["count"] = merged_count
                                if new_count > 0:
                                    # Weighted average
                                    old_avg = existing[k][hm].get("avg", 0)
                                    new_avg = v[hm]["avg"]
                                    existing[k][hm]["avg"] = round((old_avg * old_count + new_avg * new_count) / merged_count, 1) if merged_count > 0 else 0
                                    existing[k][hm]["min"] = min(existing[k][hm].get("min", 999999), v[hm].get("min", 999999))
                                    existing[k][hm]["max"] = max(existing[k][hm].get("max", 0), v[hm].get("max", 0))
                            existing[k]["total"] = existing[k].get("total", 0) + v.get("total", 0)
                            total = existing[k]["total"]
                            hit_count = existing[k]["hit"]["count"]
                            existing[k]["hit_rate"] = round(hit_count / total * 100, 1) if total > 0 else 0
                    _master_resp[group_key] = existing

        wo = data.get("custom_origin")
        if wo:
            _master_origin_by_worker[client_id] = wo

        wc = data.get("custom_collector")
        if wc:
            if _master_collector is None:
                _master_collector = wc
            else:
                # Merge collector: accumulate overall counts
                mo = _master_collector.get("overall", {})
                wo_o = wc.get("overall", {})
                mo["hits"] = mo.get("hits", 0) + wo_o.get("hits", 0)
                mo["misses"] = mo.get("misses", 0) + wo_o.get("misses", 0)
                mo["total"] = mo.get("total", 0) + wo_o.get("total", 0)
                mo["hit_rate"] = round(mo["hits"] / mo["total"] * 100, 1) if mo["total"] > 0 else 0
                _master_collector["overall"] = mo

        wp = data.get("custom_period")
        if wp and wp:
            _master_period_by_worker[client_id] = wp

        # CF sampler — send raw values so master applies its own threshold + EMA
        wcs = data.get("custom_cf_sampler")
        if wcs:
            if wcs.get("hit_raw", 0) > 0:
                cf_sampler.record_hit(wcs["hit_raw"])
            if wcs.get("miss_raw", 0) > 0:
                cf_sampler.record_miss(wcs["miss_raw"])

        # response time histograms merge
        wh = data.get("custom_histogram")
        if wh:
            _master_histogram.merge(wh)
        wsh = data.get("custom_seg_histogram")
        if wsh:
            _master_seg_histogram.merge(wsh)
        wml = data.get("custom_master_latency")
        if wml:
            _master_master_latency.merge(wml)
        wcl = data.get("custom_child_latency")
        if wcl:
            _master_child_latency.merge(wcl)
        wmsh = data.get("custom_master_sec_hist")
        if wmsh:
            _master_master_sec_hist.merge(wmsh)
        wcsh = data.get("custom_child_sec_hist")
        if wcsh:
            _master_child_sec_hist.merge(wcsh)


_global_environment = None  # Set in on_init
_is_master_cached = None    # M6: cached after first determination


def _is_master():
    """Check if current process is running as master (cached after first call)."""
    global _is_master_cached
    if _is_master_cached is not None:
        return _is_master_cached
    if _global_environment and _global_environment.runner:
        from locust.runners import MasterRunner
        _is_master_cached = isinstance(_global_environment.runner, MasterRunner)
        return _is_master_cached
    # Fallback: check argv or received worker data (not cached — transient state)
    return "--master" in __import__("sys").argv or bool(_master_cache_stats or _master_period_by_worker)


def _get_cache_stats_data():
    """Return cache_stats from master aggregation or local."""
    if _is_master() and _master_cache_stats:
        return _master_cache_stats
    return cache_stats


def _get_request_log_data():
    """Return request_log from master aggregation or local."""
    if _is_master() and _master_request_log:
        return _master_request_log
    return request_log


def _get_resp_snapshot():
    """Return resp_tracker snapshot from master aggregation or local."""
    if _is_master() and _master_resp:
        return _master_resp
    return resp_tracker.snapshot()


def _get_master_latency_snapshot():
    """Return master m3u8 latency snapshot (percentiles)."""
    if _is_master():
        return _master_master_latency.snapshot()
    return master_latency.snapshot()


def _get_child_latency_snapshot():
    """Return child m3u8 latency snapshot (percentiles)."""
    if _is_master():
        return _master_child_latency.snapshot()
    return child_latency.snapshot()


def _get_master_sec_hist_snapshot():
    """Master m3u8 1초버킷 히스토그램."""
    if _is_master():
        return _master_master_sec_hist.snapshot()
    return master_sec_hist.snapshot()


def _get_child_sec_hist_snapshot():
    """Child m3u8 1초버킷 히스토그램."""
    if _is_master():
        return _master_child_sec_hist.snapshot()
    return child_sec_hist.snapshot()


def _get_origin_snapshot():
    """Return origin snapshot from master aggregation or local."""
    if _is_master() and _master_origin_by_worker:
        total_origin_rps = 0
        total_rps = 0
        for wo in _master_origin_by_worker.values():
            total_origin_rps += wo.get("current_origin_rps", 0)
            total_rps += wo.get("current_total_rps", 0)
        return {
            "current_origin_rps": round(total_origin_rps, 1),
            "current_total_rps": round(total_rps, 1),
            "series": [],  # series not merged across workers
        }
    return origin_tracker.snapshot()


def _get_collector_snapshot():
    """Return collector snapshot from master aggregation or local."""
    if _is_master() and _master_collector:
        return _master_collector
    return collector.snapshot_cumulative()


def _get_period_snapshot():
    """Return period tracker snapshot from master aggregation or local."""
    if _is_master() and _master_period_by_worker:
        # Merge all workers' latest snapshots
        merged = {"total_requests": 0, "hits": 0, "misses": 0, "errors": 0,
                  "rps": 0, "hit_rps": 0, "miss_rps": 0, "throughput_mbps": 0,
                  "hit_avg_ms": 0, "miss_avg_ms": 0, "hit_actual_ms": 0, "miss_actual_ms": 0,
                  "ts": "", "interval_sec": 10.0}
        hit_lat_weighted = 0.0
        miss_lat_weighted = 0.0
        hit_actual_weighted = 0.0
        miss_actual_weighted = 0.0
        for wp in _master_period_by_worker.values():
            if not wp:
                continue
            for k in ("total_requests", "hits", "misses", "errors"):
                merged[k] += wp.get(k, 0)
            for k in ("rps", "hit_rps", "miss_rps", "throughput_mbps"):
                merged[k] = round(merged[k] + wp.get(k, 0), 2)
            # Weighted latency sum for proper averaging
            if wp.get("hits", 0) > 0:
                hit_lat_weighted += wp["hit_avg_ms"] * wp["hits"]
                hit_actual_weighted += wp.get("hit_actual_ms", 0) * wp["hits"]
            if wp.get("misses", 0) > 0:
                miss_lat_weighted += wp["miss_avg_ms"] * wp["misses"]
                miss_actual_weighted += wp.get("miss_actual_ms", 0) * wp["misses"]
            merged["ts"] = wp.get("ts", merged["ts"])
            merged["interval_sec"] = wp.get("interval_sec", 10.0)
        h = merged["hits"]
        m = merged["misses"]
        merged["hit_rate"] = round(h / (h + m) * 100, 1) if (h + m) > 0 else 0
        merged["error_rate"] = round(merged["errors"] / merged["total_requests"] * 100, 3) if merged["total_requests"] > 0 else 0
        merged["hit_avg_ms"] = round(hit_lat_weighted / h, 1) if h > 0 else 0
        merged["miss_avg_ms"] = round(miss_lat_weighted / m, 1) if m > 0 else 0
        merged["hit_actual_ms"] = round(hit_actual_weighted / h, 1) if h > 0 else 0
        merged["miss_actual_ms"] = round(miss_actual_weighted / m, 1) if m > 0 else 0
        return merged
    return period_tracker.get_last_snapshot()


def _get_histogram() -> dict:
    """Return rendition m3u8 response time histogram from master or local."""
    if _is_master():
        return _master_histogram.snapshot()
    return resp_histogram.snapshot()


def _get_seg_histogram() -> dict:
    """Return segment HEAD response time histogram from master or local."""
    if _is_master():
        return _master_seg_histogram.snapshot()
    return seg_histogram.snapshot()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _hdr(v):
    """Unwrap tuple/list values from FastHttpUser headers. Format: (key, value) or [(k,v),...]."""
    if isinstance(v, list):
        return v[0][1] if v and isinstance(v[0], tuple) else v[0] if v else ""
    if isinstance(v, tuple):
        return v[1]
    return v


# Headers we care about — try common casings to avoid full iteration
_HEADER_KEYS = {
    "x-cache": ("X-Cache", "x-cache"),
    "age": ("Age", "age"),
    "x-amz-cf-pop": ("X-Amz-Cf-Pop", "x-amz-cf-pop"),
    "cache-control": ("Cache-Control", "cache-control"),
    "via": ("Via", "via"),
    "x-amzn-requestid": ("X-Amzn-Requestid", "x-amzn-requestid"),
    "x-mediapackage-request-id": ("X-MediaPackage-Request-Id", "x-mediapackage-request-id"),
}


def _get_hdr(headers, key):
    """Fast header lookup — try exact casings before falling back to full scan."""
    if headers is None:
        return None
    candidates = _HEADER_KEYS.get(key)
    if candidates:
        for c in candidates:
            try:
                v = headers.get(c)
            except Exception:
                return None
            if v is not None:
                return str(_hdr(v))
    return None


# M1: Probabilistic sampling — no lock needed (was _req_counter_lock)
# 소규모(<1K CCU): 10, 대규모(50K+): 100 권장
LOG_SAMPLE_RATE = 10


def _relative_path(url: str) -> str:
    """Used only in non-hot paths (sampler, segment fetch). Hot path uses _BASE_PATH."""
    parsed = urlparse(url)
    path = parsed.path
    if parsed.query:
        path += "?" + parsed.query
    return path


def _format_bytes(b):
    if b >= 1_073_741_824:
        return f"{b / 1_073_741_824:.2f} GB"
    if b >= 1_048_576:
        return f"{b / 1_048_576:.1f} MB"
    if b >= 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b} B"


def _pick_rendition():
    """Weighted random selection via bisect (avoids random.choices list alloc)."""
    return config.RENDITIONS[bisect.bisect_right(_CUM_WEIGHTS, random.random() * _TOTAL_WEIGHT)]


# ---------------------------------------------------------------------------
# User classes
# ---------------------------------------------------------------------------

# Regex to extract rendition index — handles both:
#   SSAI/MediaTailor:    .../3.m3u8       → "/3.m3u8"
#   MediaPackage direct: playlist_3.m3u8  → "playlist_3.m3u8"
_MT_REND_RE = re.compile(r'(?:playlist_|/)(\d+)\.m3u8')


def _fetch_session(user):
    """Fetch master playlist from sogne-live → parse MediaTailor rendition URLs.
    Cached on user instance, refreshed every SESSION_REFRESH_INTERVAL seconds.
    """
    now = time.time()
    # Return cached session if still fresh
    # TimeMachine은 매 요청마다 minus가 달라지므로 캐시 안 함
    cached = getattr(user, "_session_urls", None)
    cached_ts = getattr(user, "_session_ts", 0)
    td = getattr(user, "time_delay", None)
    if td is None and cached and (now - cached_ts) < config.SESSION_REFRESH_INTERVAL:
        return cached  # Live만 세션 캐시 (minus 없으니 OK)

    qs_parts = []
    if config.AUTH_TOKEN:
        qs_parts.append(f"token={config.AUTH_TOKEN}")
    if td is not None:
        if config.TM_PARAM_MODE == "minus":
            qs_parts.append(f"minus={td}")
        else:
            qs_parts.append(f"aws.manifestsettings=time_delay:{td}")
    qs = "&".join(qs_parts)

    master_path = f"{_BASE_PATH}/playlist.m3u8"
    if qs:
        master_path += f"?{qs}"

    t0 = time.time()
    with user.client.get(
        master_path,
        name=f"[{user.user_type}] master",
        catch_response=True,
        headers=_DEFAULT_HEADERS if _DEFAULT_HEADERS else None,
    ) as resp:
        if resp.status_code != 200:
            resp.failure(f"master HTTP {resp.status_code}")
            _ms_err = (time.time() - t0) * 1000
            master_latency.record(_ms_err, is_error=True)
            master_sec_hist.record(_ms_err, is_error=True)
            return cached  # fallback to old session
        resp.success()
        body = resp.text or ""

        # Track master playlist metrics (sogne-live → CF 캐시 측정)
        ms = (time.time() - t0) * 1000
        # SSAI 환경에서는 hit rate 무의미 → p50/p95/p99 latency가 핵심 KPI
        master_latency.record(ms, is_error=False)
        master_sec_hist.record(ms, is_error=False)
        hdrs = getattr(resp, "headers", None)
        x_cache = (_get_hdr(hdrs, "x-cache") or "").lower()
        is_hit = "hit" in x_cache
        resp_len = len(getattr(resp, "content", b"") or b"")
        # master는 collector만 기록 (대시보드 히트율/응답속도는 segment 기준)
        collector.record(user.user_type, "master",
                         _get_hdr(hdrs, "x-cache") or "",
                         _get_hdr(hdrs, "age") or "",
                         _get_hdr(hdrs, "x-amz-cf-pop") or "")

    # Parse rendition URLs from master playlist
    # Handles both absolute (SSAI/MediaTailor) and relative (MediaPackage direct) URLs
    session = {}
    _rend_base = config.BASE_URL.rstrip("/") + "/"
    _token_qs = f"token={config.AUTH_TOKEN}" if config.AUTH_TOKEN else ""
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Resolve relative URL → absolute
        # MediaPackage already embeds TM params (aws.manifestsettings) in relative URLs,
        # so only add token if not present
        if not line.startswith("http"):
            line = _rend_base + line
            if _token_qs and "token=" not in line:
                sep = "&" if "?" in line else "?"
                line += sep + _token_qs
        m = _MT_REND_RE.search(line)
        if m:
            rend_idx = m.group(1)  # "3" from "playlist_3.m3u8" or ".../3.m3u8"
            session[rend_idx] = line

    if session:
        user._session_urls = session
        user._session_ts = now
    return session or cached



# (_track_response removed — segment tracking is inline in _watch_stream)


_user_id_counter = 0

def _watch_stream(user):
    """B mode: master playlist (sogne-live) → rendition playlist (MediaTailor)."""
    global _user_id_counter
    # 유저별 고유 ID 부여
    if not hasattr(user, "_uid"):
        _user_id_counter += 1
        user._uid = _user_id_counter

    td = getattr(user, "time_delay", None)

    # 1. Get session (master playlist → MediaTailor rendition URLs)
    session = _fetch_session(user)
    if not session:
        return  # master failed, skip

    # 2. Pick rendition
    rendition = _pick_rendition()
    rend_name = rendition["name"]  # "2", "3", "4", ...
    rend_label = _RENDITION_LABEL.get(rend_name, rend_name)
    codec = _RENDITION_CODEC.get(rend_name, "AVC")
    req_name = _NAME_CACHE.get((user.user_type, rend_name), f"[{user.user_type}] {rend_label}")

    # 3. Get rendition URL from session
    rend_url = session.get(rend_name)
    if not rend_url:
        # Fallback: pick any available rendition
        rend_name = random.choice(list(session.keys()))
        rend_url = session[rend_name]
        rend_label = _RENDITION_LABEL.get(rend_name, rend_name)
        codec = _RENDITION_CODEC.get(rend_name, "AVC")
        req_name = f"[{user.user_type}] {rend_label}"

    # TM 파라미터가 rendition URL에 없으면 명시적으로 추가
    # (CF가 master cache key에서 aws.manifestsettings를 제외할 경우 live master가 캐시되어 TM 파라미터 없는 rendition URL이 반환될 수 있음)
    if rend_url and td is not None:
        if config.TM_PARAM_MODE == "aws" and "aws.manifestsettings" not in rend_url:
            sep = "&" if "?" in rend_url else "?"
            rend_url += f"{sep}aws.manifestsettings=time_delay:{td}"
        elif config.TM_PARAM_MODE == "minus" and "minus=" not in rend_url:
            sep = "&" if "?" in rend_url else "?"
            rend_url += f"{sep}minus={td}"

    # 4. Fetch rendition playlist — user별 Session으로 connection 재사용 (새 connection마다 SSL overhead 방지)
    if not hasattr(user, "_rend_session"):
        import requests as _requests
        s = _requests.Session()
        if _DEFAULT_HEADERS:
            s.headers.update(_DEFAULT_HEADERS)
        user._rend_session = s
    t0 = time.time()
    try:
        mt_resp = user._rend_session.get(rend_url, timeout=(5, 10))
        response_time_ms = (time.time() - t0) * 1000

        # Report to Locust stats manually
        if mt_resp.status_code == 200:
            child_latency.record(response_time_ms, is_error=False)
            child_sec_hist.record(response_time_ms, is_error=False)
            events.request.fire(
                request_type="GET", name=req_name,
                response_time=response_time_ms,
                response_length=len(mt_resp.content),
                response=mt_resp, context={}, exception=None,
            )
        else:
            child_latency.record(response_time_ms, is_error=True)
            child_sec_hist.record(response_time_ms, is_error=True)
            events.request.fire(
                request_type="GET", name=req_name,
                response_time=response_time_ms,
                response_length=0,
                response=mt_resp, context={},
                exception=Exception(f"HTTP {mt_resp.status_code}"),
            )
            return

        # Inline tracking — requests.Response.headers is case-insensitive
        rh = mt_resp.headers
        x_cache_raw = rh.get("X-Cache", "")
        age_raw = rh.get("Age", "")
        pop_raw = rh.get("X-Amz-Cf-Pop", "")
        cache_control = rh.get("Cache-Control", "")
        x_cache = x_cache_raw.lower()
        is_hit = "hit" in x_cache
        resp_length = len(mt_resp.content)

        collector.record(user.user_type, "rendition", x_cache_raw, age_raw, pop_raw)
        resp_histogram.record(response_time_ms, is_hit)
        # 실시간 대시보드 RPS/응답속도: rendition 기준으로 피딩 (SEGMENT_CACHE_SAMPLE=0 대응)
        resp_tracker.record(response_time_ms, is_hit, "rendition", codec, is_error=False)
        origin_tracker.record(not is_hit)
        period_tracker.record(is_hit, False, response_time_ms, resp_length, actual_ms=response_time_ms)

        # 5. Segment CF cache sampling + rendition m3u8 메타데이터 로그
        if mt_resp.status_code == 200:
            body = mt_resp.text or ""
            lines = body.splitlines()

            # Parse m3u8 metadata
            seg_files = []
            pdts = []
            media_seq = "-"
            _seg_base = config.BASE_URL.rstrip("/") + "/"
            _seg_qs = f"token={config.AUTH_TOKEN}" if config.AUTH_TOKEN else ""
            for line in lines:
                ls = line.strip()
                if not ls or ls.startswith("#"):
                    if ls.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                        pdts.append(ls.split(":", 1)[1])
                    elif ls.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                        media_seq = ls.split(":")[1]
                    continue
                if ".mp4" in ls:
                    if ls.startswith("http"):
                        seg_files.append(ls)
                    else:
                        seg_url = _seg_base + ls.split("?")[0]
                        if _seg_qs:
                            seg_url += "?" + _seg_qs
                        seg_files.append(seg_url)

            # Segment CF cache HEAD sampling
            _last_seg_xcache = "-"
            _last_seg_pop = "-"
            _last_seg_age = "-"
            _last_seg_cc = "-"
            seg_urls = seg_files
            for seg_url in (seg_urls[-config.SEGMENT_CACHE_SAMPLE:] if config.SEGMENT_CACHE_SAMPLE > 0 else []):
                try:
                    st0 = time.time()
                    seg_resp = _requests.head(seg_url, timeout=(3, 5),
                                              headers=_DEFAULT_HEADERS if _DEFAULT_HEADERS else None)
                    seg_ms = (time.time() - st0) * 1000
                    seg_h = seg_resp.headers
                    seg_xcache = seg_h.get("X-Cache", "")
                    seg_age = seg_h.get("Age", "")
                    seg_pop = seg_h.get("X-Amz-Cf-Pop", "")
                    seg_is_hit = "hit" in seg_xcache.lower()

                    seg_name = f"[{user.user_type}] segment"
                    events.request.fire(
                        request_type="HEAD", name=seg_name,
                        response_time=seg_ms,
                        response_length=int(seg_h.get("Content-Length", 0)),
                        response=seg_resp, context={}, exception=None,
                    )

                    resp_tracker.record(seg_ms, seg_is_hit, "segment", codec, is_error=False)
                    seg_histogram.record(seg_ms, seg_is_hit)
                    origin_tracker.record(not seg_is_hit)
                    period_tracker.record(seg_is_hit, False, seg_ms, 0, actual_ms=seg_ms)
                    collector.record(user.user_type, "segment", seg_xcache, seg_age, seg_pop)

                    # 로그용 segment CF 결과 저장
                    _last_seg_xcache = seg_xcache or "-"
                    _last_seg_pop = seg_pop or "-"
                    _last_seg_age = seg_age or "-"
                    _last_seg_cc = seg_h.get("Cache-Control", "-")
                except Exception:
                    pass

            # 요청 로그: rendition m3u8 메타데이터 + segment CF 캐시 결과
            first_seg = seg_files[0].rsplit("/", 1)[-1].split("?")[0] if seg_files else "-"
            last_seg = seg_files[-1].rsplit("/", 1)[-1].split("?")[0] if seg_files else "-"
            first_pdt = pdts[0] if pdts else "-"
            last_pdt = pdts[-1] if pdts else "-"
            # PDT diff (마지막 PDT ~ 현재)
            # Live baseline: 마지막 PDT ~ 현재 차이에서 HLS 기본 지연 보정
            # HLS live 지연 = ~3*target_duration + 패키징 ≈ 10-14초 → 별도 보정 없이 raw diff 표시
            # TM accuracy = (diff - minus) → 0에 가까울수록 정확
            # 단, diff 자체에 HLS 기본 지연(~12초)이 포함되므로 (diff - HLS지연 - minus)로 계산
            pdt_info = None
            try:
                if pdts:
                    pdt_dt = datetime.fromisoformat(pdts[-1].replace("Z", "+00:00"))
                    raw_diff = (datetime.now(timezone.utc) - pdt_dt).total_seconds()
                    # live_latency = raw_diff 그대로 (E2E 지연: 패키징+CF 포함 총 지연)
                    # tm_accuracy = raw_diff - td → 0에 가까울수록 정확
                    if td is not None:
                        pdt_info = {"pdt": last_pdt, "diff_sec": round(raw_diff, 1), "tm_accuracy": round(raw_diff - td, 1)}
                    else:
                        pdt_info = {"pdt": last_pdt, "diff_sec": round(raw_diff, 1), "live_latency": round(raw_diff, 1)}
            except Exception:
                pass

            # URL 표시: 유저ID + 마지막 세그먼트 + minus값
            disp_url = f"U{user._uid} | {last_seg} | seq:{media_seq}"
            if td is not None:
                disp_url += f" | minus={td}"

            with request_log_lock:
                request_log.append({
                    "ts": time.strftime("%H:%M:%S"),
                    "name": req_name,
                    "url": disp_url,
                    "status": mt_resp.status_code,
                    "size": f"{len(body)} B" if len(body) < 1024 else f"{len(body) / 1024:.1f} KB",
                    "size_bytes": len(body),
                    "cf_cache": x_cache_raw or "-",
                    "cf_pop": pop_raw or "-",
                    "cf_age": age_raw or "-",
                    "cache_control": cache_control or "-",
                    "via": rh.get("Via", "-"),
                    "origin_id": rh.get("x-amzn-RequestId", "-"),
                    "latency_ms": round(response_time_ms, 1),
                    "actual_ms": round(response_time_ms, 1),
                    "overhead_ms": 0.0,
                    "pdt": pdt_info.get("pdt", "-") if pdt_info else first_pdt,
                    "pdt_diff": pdt_info.get("diff_sec") if pdt_info else None,
                    "tm_accuracy": pdt_info.get("tm_accuracy") if pdt_info else None,
                    "live_latency": pdt_info.get("live_latency") if pdt_info else None,
                })

    except Exception as e:
        response_time_ms = (time.time() - t0) * 1000
        child_latency.record(response_time_ms, is_error=True)
        child_sec_hist.record(response_time_ms, is_error=True)
        events.request.fire(
            request_type="GET", name=req_name,
            response_time=response_time_ms,
            response_length=0, response=None, context={},
            exception=e,
        )

    gevent.sleep(config.PLAYLIST_REFRESH_INTERVAL)


class LiveUser(FastHttpUser):
    weight = config.LIVE_USER_WEIGHT
    wait_time = constant_pacing(1)  # open workload: 전체 사이클 1초 유지
    concurrency = 1                 # C1: 1 conn/user (was 10 → 500K users = 5M conns!)
    max_retries = 0                 # 재시도 없음
    max_redirects = 0               # 리다이렉트 없음
    connection_timeout = 5.0        # M2: CF connect < 100ms; 5s catches DNS issues
    network_timeout = 10.0          # M2: manifest < 500ms; 10s catches slow origins
    user_type = "live"
    time_delay = None

    @task
    def watch_stream(self):
        _watch_stream(self)


class TimeMachineUser(FastHttpUser):
    weight = config.TIME_MACHINE_USER_WEIGHT
    wait_time = constant_pacing(1)  # open workload: 전체 사이클 1초 유지
    concurrency = 1                 # C1: 1 conn/user
    max_retries = 0                 # 재시도 없음
    max_redirects = 0               # 리다이렉트 없음
    connection_timeout = 5.0        # M2: CF connect timeout
    network_timeout = 10.0          # M2: manifest read timeout
    user_type = "timemachine"
    time_delay = 1  # placeholder, randomized per request

    @task
    def watch_stream(self):
        # time_delay grows with test elapsed time, capped at TIME_DELAY_MAX (10800s)
        # Phase 1 (elapsed < 10800s): range = 1 ~ elapsed (DVR window growing)
        # Phase 2 (elapsed >= 10800s): range = 1 ~ 10800 (full 3hr DVR window)
        elapsed = int(time.time() - _tm_base_time) if _tm_base_time > 0 else 1
        max_delay = max(config.TIME_DELAY_MIN, min(elapsed, config.TIME_DELAY_MAX))
        self.time_delay = random.randint(config.TIME_DELAY_MIN, max_delay)
        _watch_stream(self)


# ---------------------------------------------------------------------------
# CF Latency Sampler User — shares Locust's connection pool
# ---------------------------------------------------------------------------
class SamplerUser(FastHttpUser):
    """Dedicated user for CF latency sampling. fixed_count=1 → exactly 1 instance."""
    fixed_count = 1
    wait_time = constant(3)  # sample every 3 seconds
    concurrency = 1           # C1: only 1 connection needed
    max_retries = 0
    max_redirects = 0
    connection_timeout = 5.0
    network_timeout = 10.0

    @task
    def sample_latency(self):
        rend = config.RENDITIONS[2]["name"]
        token_qs = f"token={config.AUTH_TOKEN}" if config.AUTH_TOKEN else ""

        # HIT sample: TM URL with large time_delay (likely cached)
        tm_param = f"minus={config.TIME_DELAY_MAX // 2}" if config.TM_PARAM_MODE == "minus" else f"aws.manifestsettings=time_delay:{config.TIME_DELAY_MAX // 2}"
        hit_qs = "&".join(filter(None, [token_qs, tm_param]))
        hit_path = f"{_BASE_PATH}/{rend}.m3u8?{hit_qs}"
        t0 = time.time()
        try:
            with self.client.get(hit_path, name="[sampler] HIT", catch_response=True) as resp:
                ms = round((time.time() - t0) * 1000, 1)
                hdrs = getattr(resp, "headers", None)
                x_cache = (_get_hdr(hdrs, "x-cache") or "").lower()
                if "hit" in x_cache:
                    cf_sampler.record_hit(ms)
                resp.success()
        except Exception:
            pass

        # MISS sample: live URL (no TM param → likely miss)
        miss_path = f"{_BASE_PATH}/{rend}.m3u8"
        if token_qs:
            miss_path += f"?{token_qs}"
        t1 = time.time()
        try:
            with self.client.get(miss_path, name="[sampler] MISS", catch_response=True) as resp:
                ms = round((time.time() - t1) * 1000, 1)
                hdrs = getattr(resp, "headers", None)
                x_cache = (_get_hdr(hdrs, "x-cache") or "").lower()
                if "miss" in x_cache:
                    cf_sampler.record_miss(ms)
                resp.success()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Load Shape: staged ramp-up (only active when USE_LOAD_SHAPE=true)
# ---------------------------------------------------------------------------
if config.USE_LOAD_SHAPE:
    class StagedLoadShape(LoadTestShape):
        """
        Stage-based load shape:
          S1  0→100K   (5min, 500/s spawn)
          S2  100K→300K (5min, 1000/s spawn)
          S3  300K→500K (5min, 1000/s spawn)
          S4  500K hold (10min)
        """
        stages = config.LOAD_STAGES

        def tick(self):
            run_time = self.get_run_time()
            elapsed = 0
            for stage in self.stages:
                elapsed += stage["duration"]
                if run_time < elapsed:
                    return (stage["users"], stage["spawn_rate"] or 1)
            # All stages done → stop
            return None


# ---------------------------------------------------------------------------
# Per-request stats collection
# ---------------------------------------------------------------------------
@events.request.add_listener
def on_request(name, response, exception, response_time, **kwargs):
    """Lightweight handler — cache hit/miss counting only. No locks.
    Heavy tracking (resp_tracker, period_tracker, request_log) moved to _watch_stream.
    """
    if response is None:
        return

    hdrs = getattr(response, "headers", None)
    x_cache = (_get_hdr(hdrs, "x-cache") or "").lower()

    cs = cache_stats.get(name)
    if cs is None:
        cs = {"hit": 0, "miss": 0, "noinfo": 0}
        cache_stats[name] = cs

    if "hit" in x_cache:
        cs["hit"] += 1
    elif "miss" in x_cache:
        cs["miss"] += 1
    else:
        cs["noinfo"] += 1


@events.reset_stats.add_listener
def on_reset_stats():
    global _master_resp, _master_collector
    global _master_reset_ts, _worker_reset_ts
    now = time.time()
    _master_reset_ts = now
    _worker_reset_ts = now  # Workers also get reset_stats event
    cache_stats.clear()
    _master_cache_stats.clear()
    _master_resp = None
    _master_origin_by_worker.clear()
    _master_collector = None
    _master_period_by_worker.clear()
    with request_log_lock:
        request_log.clear()
        _master_request_log.clear()
    collector.reset()
    throughput.reset()
    resp_tracker.reset()
    origin_tracker.reset()
    metrics_ts.reset()
    period_tracker.reset()
    cf_sampler.reset()
    resp_histogram.reset()
    _master_histogram.reset()
    seg_histogram.reset()
    _master_seg_histogram.reset()
    master_latency.reset()
    _master_master_latency.reset()
    child_latency.reset()
    _master_child_latency.reset()
    master_sec_hist.reset()
    _master_master_sec_hist.reset()
    child_sec_hist.reset()
    _master_child_sec_hist.reset()


# ---------------------------------------------------------------------------
# PASS/FAIL judgment
# ---------------------------------------------------------------------------
def _evaluate_pass_fail():
    snap = _get_resp_snapshot()
    criteria = config.PASS_CRITERIA

    # Compute current values
    total_hit = snap["hit"]["count"]
    total_miss = snap["miss"]["count"]
    total = total_hit + total_miss
    hit_rate = (total_hit / total * 100) if total > 0 else 0

    results = []

    # Cache hit rate
    c = criteria["cache_hit_rate"]
    val = round(hit_rate, 1)
    verdict = "PASS" if val >= c["pass"] else ("FAIL" if val < c["fail"] else "WARN")
    results.append({"id": "1-1", "label": c["label"], "value": f"{val}{c['unit']}", "pass": c["pass"], "fail": c["fail"], "verdict": verdict})

    # HIT avg latency — CF Sampler EMA (native thread, no greenlet overhead)
    cf_lat = cf_sampler.get()
    c = criteria["hit_avg_latency"]
    val = cf_lat["hit_ms"] if cf_lat["hit_ms"] > 0 else snap["hit"]["avg"]
    verdict = "PASS" if val <= c["pass"] else ("FAIL" if val > c["fail"] else "WARN")
    results.append({"id": "1-2", "label": c["label"], "value": f"{val}{c['unit']}", "pass": f"<={c['pass']}", "fail": f">{c['fail']}", "verdict": verdict})

    # MISS avg latency — CF Sampler EMA
    c = criteria["miss_avg_latency"]
    val = cf_lat["miss_ms"] if cf_lat["miss_ms"] > 0 else snap["miss"]["avg"]
    verdict = "PASS" if val <= c["pass"] else ("FAIL" if val > c["fail"] else "WARN")
    results.append({"id": "1-3", "label": c["label"], "value": f"{val}{c['unit']}", "pass": f"<={c['pass']}", "fail": f">{c['fail']}", "verdict": verdict})

    # Error rate
    c = criteria["error_rate"]
    val = snap["error_rate"]
    verdict = "PASS" if val <= c["pass"] else ("FAIL" if val > c["fail"] else "WARN")
    results.append({"id": "1-7", "label": c["label"], "value": f"{val}{c['unit']}", "pass": f"<={c['pass']}", "fail": f">{c['fail']}", "verdict": verdict})

    # Master/Child m3u8 latency percentiles (SSAI 환경 핵심 KPI)
    ml_snap = _get_master_latency_snapshot()
    cl_snap = _get_child_latency_snapshot()
    for layer, snap_lat, prefix, id_base in (
        ("master", ml_snap, "master", "2"),
        ("child",  cl_snap, "child",  "3"),
    ):
        for pct in ("p50", "p95", "p99"):
            key = f"{prefix}_{pct}"
            if key not in criteria:
                continue
            c = criteria[key]
            val = snap_lat.get(pct, 0)
            verdict = "PASS" if val <= c["pass"] else ("FAIL" if val > c["fail"] else "WARN")
            # No data yet → show N/A without verdict
            if snap_lat.get("count", 0) == 0:
                verdict = "WARN"
                val_disp = "N/A"
            else:
                val_disp = f"{val}{c['unit']}"
            idx = {"p50": "1", "p95": "2", "p99": "3"}[pct]
            results.append({"id": f"{id_base}-{idx}", "label": c["label"], "value": val_disp,
                            "pass": f"<={c['pass']}", "fail": f">{c['fail']}", "verdict": verdict})

    # Origin Req/s flat check (MISS = origin request to MP/ML)
    origin_snap = _get_origin_snapshot()
    series = origin_snap.get("series", [])
    if len(series) >= 4:
        origin_values = [s["origin_rps"] for s in series[-8:]]  # last ~40s
        avg_origin = sum(origin_values) / len(origin_values) if origin_values else 0
        max_origin = max(origin_values) if origin_values else 0
        # "flat" = max is not more than 3x the average (spike detection)
        is_flat = max_origin <= max(avg_origin * 3, 5)  # allow small absolute values
        verdict = "PASS" if is_flat else "WARN"
        results.append({"id": "1-4", "label": "Origin Req/s flat", "value": f"{avg_origin:.1f}/s (max:{max_origin:.1f})", "pass": "CCU 비례 아님", "fail": "CCU 비례 증가", "verdict": verdict})

    return results


# ---------------------------------------------------------------------------
# Console cache reporter (greenlet)
# ---------------------------------------------------------------------------
def cache_reporter(environment):
    # config.AVG_CYCLE_SEC: playlist_refresh + wait_time(pacing) + request overhead
    avg_cycle = config.AVG_CYCLE_SEC

    while True:
        gevent.sleep(config.CACHE_REPORT_INTERVAL)
        # Snapshot period tracker (10s window)
        period_tracker.snapshot_and_reset(config.CACHE_REPORT_INTERVAL)
        snapshot = collector.snapshot_periodic()
        # standalone 모드: resp_tracker가 worker report로 리셋되지 않으므로 직접 리셋
        if not _is_master():
            lat_snap = resp_tracker.snapshot_and_reset()
        else:
            lat_snap = _get_resp_snapshot()
        origin_snap = _get_origin_snapshot()
        tp_mbps = throughput.get_throughput_mbps()

        # Current user count & target RPS
        user_count = environment.runner.user_count if environment.runner else 0
        target_rps = round(user_count / avg_cycle, 1) if avg_cycle > 0 else 0

        # Use period snapshot for accurate RPS in distributed mode
        period_snap = _get_period_snapshot()
        p_rps = period_snap.get("rps", 0) if period_snap else 0
        p_hit_rps = period_snap.get("hit_rps", 0) if period_snap else 0
        p_miss_rps = period_snap.get("miss_rps", 0) if period_snap else 0
        p_hit_rate = period_snap.get("hit_rate", 0) if period_snap else 0
        p_hit_avg = period_snap.get("hit_avg_ms", 0) if period_snap else 0
        p_miss_avg = period_snap.get("miss_avg_ms", 0) if period_snap else 0
        p_error_rate = period_snap.get("error_rate", 0) if period_snap else 0
        p_throughput = period_snap.get("throughput_mbps", 0) if period_snap else 0

        # Fallback to origin_tracker if period has no data (single mode)
        if p_rps == 0:
            p_rps = origin_snap["current_total_rps"]
            p_miss_rps = origin_snap["current_origin_rps"]
            p_hit_rps = max(0, p_rps - p_miss_rps)
            total_hit = lat_snap["hit"]["count"]
            total_miss = lat_snap["miss"]["count"]
            total_all = total_hit + total_miss
            p_hit_rate = round(total_hit / total_all * 100, 1) if total_all > 0 else 0
            p_hit_avg = lat_snap["hit"]["avg"]
            p_miss_avg = lat_snap["miss"]["avg"]
            p_error_rate = lat_snap["error_rate"]
            p_throughput = tp_mbps

        # Segment CF 응답속도 (resp_tracker 기준, cf_sampler 폴백)
        _seg_snap = lat_snap  # 위에서 이미 snapshot_and_reset 했으므로 재활용
        _seg_hit_ms = _seg_snap["hit"]["avg"] if _seg_snap["hit"]["count"] > 0 else cf_sampler.get().get("hit_ms", 0)
        _seg_miss_ms = _seg_snap["miss"]["avg"] if _seg_snap["miss"]["count"] > 0 else cf_sampler.get().get("miss_ms", 0)
        # Per-interval call counts (not RPS)
        p_hit_count = period_snap.get("hits", 0) if period_snap else 0
        p_miss_count = period_snap.get("misses", 0) if period_snap else 0
        p_total_count = period_snap.get("total_requests", 0) if period_snap else 0
        metrics_ts.add({
            "ts": time.strftime("%H:%M:%S"),
            "hit_rate": p_hit_rate,
            "hit_avg": p_hit_avg,
            "miss_avg": p_miss_avg,
            "cf_hit_ms": _seg_hit_ms,
            "cf_miss_ms": _seg_miss_ms,
            "origin_rps": origin_snap["current_origin_rps"],
            "total_rps": p_rps,
            "error_rate": p_error_rate,
            "throughput_mbps": p_throughput,
            "hit_count": p_hit_count,
            "miss_count": p_miss_count,
            "total_count": p_total_count,
            "phase": current_phase["name"],
            "user_count": user_count,
            "target_rps": target_rps,
        })

        if not snapshot:
            continue

        print("\n" + "=" * 95)
        print(f"  CloudFront Cache Report (last {config.CACHE_REPORT_INTERVAL:.0f}s)  [{time.strftime('%H:%M:%S')}]  Phase: {current_phase['name']}")
        print("=" * 95)
        print(
            f"{'User Type':<14} {'Request':<10} {'Total':>6} {'Hits':>6} "
            f"{'Miss':>6} {'Hit%':>7} {'AvgAge':>8} {'Top POP':<12}"
        )
        print("-" * 95)
        for r in snapshot:
            print(
                f"{r['user_type']:<14} {r['request_type']:<10} "
                f"{r['total']:>6} {r['hits']:>6} {r['misses']:>6} "
                f"{r['hit_rate']:>6.1f}% {r['avg_age']:>7.1f}s {r['top_pop']:<12}"
            )
        h, m = lat_snap["hit"], lat_snap["miss"]
        print("-" * 95)
        print(f"  HIT  latency: avg={h['avg']:.0f}ms  min={h['min']:.0f}ms  max={h['max']:.0f}ms  (n={h['count']})")
        print(f"  MISS latency: avg={m['avg']:.0f}ms  min={m['min']:.0f}ms  max={m['max']:.0f}ms  (n={m['count']})")
        print(f"  Error rate: {lat_snap['error_rate']:.3f}%  |  Origin Req/s: {origin_snap['current_origin_rps']}  |  Throughput: {tp_mbps:.1f} Mbps")
        print("=" * 95 + "\n")


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

REQUEST_LOG_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>요청 URL 로그</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f172a;color:#e2e8f0;padding:10px;max-width:100%;margin:0 auto;min-height:100vh;display:flex;flex-direction:column}
  h1{font-size:1.4rem;color:#38bdf8;margin-bottom:8px}
  .top-nav{display:flex;gap:0;margin-bottom:10px;background:#1e293b;border-radius:8px;overflow:hidden;border:1px solid #334155}
  .table-wrap{flex:1;overflow-y:auto;overflow-x:auto}
  .top-nav a{color:#94a3b8;text-decoration:none;padding:10px 20px;font-size:.85rem;font-weight:600;transition:all .2s;border-right:1px solid #334155}
  .top-nav a:last-child{border-right:none}
  .top-nav a:hover{color:#e2e8f0;background:#334155}
  .top-nav a.active{color:#38bdf8;background:#0f172a;border-bottom:2px solid #38bdf8}
  .controls{display:flex;gap:12px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
  .controls select,.controls input{background:#1e293b;border:1px solid #334155;color:#e2e8f0;padding:6px 10px;border-radius:4px;font-size:.85rem}
  .controls input{width:200px}
  .controls button{background:#334155;color:#e2e8f0;border:none;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:.85rem}
  .controls button:hover{background:#475569}
  .controls button:disabled{opacity:.4;cursor:not-allowed}
  .controls .info{color:#64748b;font-size:.8rem;margin-left:auto}
  table{border-collapse:collapse;font-size:.8rem;white-space:nowrap}
  th{text-align:left;padding:6px 8px;border-bottom:2px solid #334155;color:#94a3b8;font-weight:600;position:sticky;top:0;background:#0f172a;cursor:pointer;user-select:none;white-space:nowrap;resize:horizontal;overflow:hidden}
  th:hover{color:#e2e8f0}
  th .sort-arrow{font-size:.7rem;margin-left:4px;color:#475569}
  th.sort-active .sort-arrow{color:#38bdf8}
  td{padding:5px 8px;border-bottom:1px solid #1e293b;white-space:nowrap}
  tr:hover td{background:#1e293b}
  .hit{color:#4ade80} .miss{color:#f87171} .refresh{color:#facc15}
  .pager{display:flex;gap:8px;align-items:center;padding:8px 0;margin-top:auto}
  .pager button{background:#1e293b;border:1px solid #334155;color:#e2e8f0;padding:4px 12px;border-radius:4px;cursor:pointer}
  .pager button:hover{background:#334155}
  .pager button:disabled{opacity:.3;cursor:not-allowed}
  .pager .page-info{color:#94a3b8;font-size:.85rem}
  td.url-cell{cursor:pointer} td.url-cell:hover{color:#38bdf8;text-decoration:underline}
  .legend{display:flex;gap:16px;margin-bottom:12px;font-size:.8rem;color:#94a3b8}
  .legend span{display:flex;align-items:center;gap:4px}
  .legend .dot{width:10px;height:10px;border-radius:50%;display:inline-block}
</style>
</head>
<body>
<nav class="top-nav">
  <a href="/">Locust UI</a>
  <a href="/cache-dashboard">캐시 대시보드</a>
  <a href="/request-log-view" class="active">요청 로그</a>
</nav>
<h1>요청 URL 로그</h1>
<div class="legend">
  <span><span class="dot" style="background:#4ade80"></span> CF 히트</span>
  <span><span class="dot" style="background:#f87171"></span> CF 미스</span>
  <span><span class="dot" style="background:#facc15"></span> CF 리프레시히트</span>
  <span>| CF = CloudFront 캐시 &nbsp; Cache-Control = 오리진(MediaPackage) 캐시 정책</span>
</div>
<div class="controls">
  <select id="filterType">
    <option value="">전체</option>
    <option value="[live]">라이브만</option>
    <option value="[timemachine]">타임머신만</option>
    <option value="variant">변환 플레이리스트</option>
    <option value="segment">세그먼트</option>
  </select>
  <select id="filterCache">
    <option value="">CF 캐시 전체</option>
    <option value="Hit">HIT만</option>
    <option value="Miss">MISS만</option>
    <option value="RefreshHit">RefreshHit만</option>
  </select>
  <input type="text" id="filterText" placeholder="URL 또는 이름 검색...">
  <select id="perPage"><option value="50">50개/페이지</option><option value="100">100개/페이지</option><option value="200">200개/페이지</option></select>
  <button id="refreshBtn">새로고침</button>
  <button id="exportBtn" style="background:#0d9488;font-weight:600">Excel 다운로드</button>
  <label style="font-size:.8rem;color:#94a3b8"><input type="checkbox" id="autoRefresh" checked> 자동 (3초)</label>
  <span class="info" id="totalInfo"></span>
</div>
<div class="table-wrap">
<table><thead><tr>
  <th class="col-ts" data-col="ts" data-type="str">시간 <span class="sort-arrow"></span></th>
  <th class="col-name" data-col="name" data-type="str">이름 <span class="sort-arrow"></span></th>
  <th class="col-url" data-col="url" data-type="str">URL <span class="sort-arrow"></span></th>
  <th class="col-status" data-col="status" data-type="num">상태 <span class="sort-arrow"></span></th>
  <th class="col-actual" data-col="actual_ms" data-type="num">CF응답 <span class="sort-arrow"></span></th>
  <th class="col-overhead" data-col="overhead_ms" data-type="num">지연 <span class="sort-arrow"></span></th>
  <th class="col-size" data-col="size" data-type="num">크기 <span class="sort-arrow"></span></th>
  <th class="col-cf-cache" data-col="cf_cache" data-type="str">CF 캐시 <span class="sort-arrow"></span></th>
  <th class="col-cf-pop" data-col="cf_pop" data-type="str">CF POP <span class="sort-arrow"></span></th>
  <th class="col-pdt" data-col="pdt_diff" data-type="num">PDT(KST) <span class="sort-arrow"></span></th>
  <th class="col-cc" data-col="cache_control" data-type="str">캐시정책 <span class="sort-arrow"></span></th>
</tr></thead><tbody id="logBody"></tbody></table>
</div>
<div class="pager">
  <button id="prevBtn">&larr; 이전</button>
  <span class="page-info" id="pageInfo">-</span>
  <button id="nextBtn">다음 &rarr;</button>
</div>
<script>
let page=1, totalPages=1, sortCol='', sortDir='';
let lastData=[];
const body=document.getElementById('logBody'),filterType=document.getElementById('filterType'),
  filterText=document.getElementById('filterText'),perPage=document.getElementById('perPage'),
  prevBtn=document.getElementById('prevBtn'),nextBtn=document.getElementById('nextBtn'),
  pageInfo=document.getElementById('pageInfo'),totalInfo=document.getElementById('totalInfo'),
  autoCheck=document.getElementById('autoRefresh'),
  filterCache=document.getElementById('filterCache');
function cacheClass(v){v=(v||'').toLowerCase();if(v.includes('hit')&&!v.includes('miss'))return v.includes('refresh')?'refresh':'hit';if(v.includes('miss'))return 'miss';return '';}
function parseNum(v){if(v==null||v==='-'||v==='')return -1;const n=parseFloat(String(v).replace(/[^0-9.\-]/g,''));return isNaN(n)?-1:n;}
function sortData(data){
  if(!sortCol)return data;
  const th=document.querySelector('th[data-col="'+sortCol+'"]');
  const isNum=th&&th.dataset.type==='num';
  return [...data].sort((a,b)=>{
    let va=a[sortCol]||'',vb=b[sortCol]||'';
    if(isNum){va=parseNum(va);vb=parseNum(vb);}else{va=String(va).toLowerCase();vb=String(vb).toLowerCase();}
    if(va<vb)return sortDir==='asc'?-1:1;if(va>vb)return sortDir==='asc'?1:-1;return 0;
  });
}
function fmtSec(s){if(s==null||s==='-'||s==='')return'-';var n=parseFloat(s);if(isNaN(n))return s;if(Math.abs(n)>=3600)return Math.floor(n/3600)+'시간 '+Math.floor((Math.abs(n)%3600)/60)+'분';if(Math.abs(n)>=60)return Math.floor(n/60)+'분 '+Math.round(n%60)+'초';return Math.round(n*10)/10+'초';}
function fmtPdt(e){
  if(!e.pdt||e.pdt==='-')return{html:'-',sort:0};
  var d=new Date(e.pdt);var kst=new Date(d.getTime()+9*3600000);
  var t=('0'+kst.getUTCHours()).slice(-2)+':'+('0'+kst.getUTCMinutes()).slice(-2)+':'+('0'+kst.getUTCSeconds()).slice(-2);
  if(e.tm_accuracy!=null){var a=e.tm_accuracy;var c=Math.abs(a)<=5?'#4ade80':Math.abs(a)<=30?'#facc15':'#f87171';return{html:'<span title="UTC:'+e.pdt+'">'+t+'</span> <span style="color:'+c+';font-size:.8em">오차 '+fmtSec(a)+'</span>',sort:e.pdt_diff||0};}
  if(e.live_latency!=null){var c2=e.live_latency<=10?'#4ade80':e.live_latency<=30?'#facc15':'#f87171';return{html:'<span title="UTC:'+e.pdt+'">'+t+'</span> <span style="color:'+c2+';font-size:.8em">지연 '+fmtSec(e.live_latency)+'</span>',sort:e.pdt_diff||0};}
  return{html:t,sort:e.pdt_diff||0};
}
function renderRows(entries){
  let h='';entries.forEach(e=>{const cc=cacheClass(e.cf_cache);
    h+='<tr><td>'+e.ts+'</td><td>'+e.name+'</td><td class="url-cell" title="'+e.url+'">'+e.url+'</td>';
    h+='<td>'+e.status+'</td>';
    var am=e.actual_ms!=null?e.actual_ms+'ms':'-';
    var oh=e.overhead_ms!=null?'<span style="color:#64748b">+'+e.overhead_ms+'ms</span>':'-';
    h+='<td style="color:#4ade80">'+am+'</td><td>'+oh+'</td><td>'+(e.size||'-')+'</td>';
    h+='<td class="'+(cc||'')+'">'+e.cf_cache+'</td><td>'+e.cf_pop+'</td>';
    var pdt=fmtPdt(e);
    h+='<td>'+pdt.html+'</td>';
    h+='<td>'+e.cache_control+'</td></tr>';
  });body.innerHTML=h||'<tr><td colspan="11" style="color:#64748b;text-align:center">요청 데이터 없음</td></tr>';
}
function updateSortUI(){
  document.querySelectorAll('th[data-col]').forEach(th=>{
    const arrow=th.querySelector('.sort-arrow');
    if(th.dataset.col===sortCol){th.classList.add('sort-active');arrow.textContent=sortDir==='asc'?' ▲':' ▼';}
    else{th.classList.remove('sort-active');arrow.textContent='';}
  });
}
function load(){
  let f=filterType.value,t=filterText.value,cf=filterCache.value,filter=f;if(t)filter=filter?filter+' '+t:t;
  fetch('/request-log?page='+page+'&per_page='+perPage.value+'&filter='+encodeURIComponent(filter)+'&cache_filter='+encodeURIComponent(cf))
    .then(r=>r.json()).then(d=>{
      totalPages=d.total_pages;page=d.page;
      pageInfo.textContent='Page '+d.page+' / '+d.total_pages;
      totalInfo.textContent=d.total.toLocaleString()+' requests';
      prevBtn.disabled=page<=1;nextBtn.disabled=page>=totalPages;
      lastData=d.entries;
      renderRows(sortData(lastData));
    }).catch(()=>{});
}
document.querySelectorAll('th[data-col]').forEach(th=>{
  th.onclick=()=>{
    const col=th.dataset.col;
    if(sortCol===col){sortDir=sortDir==='asc'?'desc':'asc';}else{sortCol=col;sortDir='asc';}
    updateSortUI();renderRows(sortData(lastData));
  };
});
prevBtn.onclick=()=>{if(page>1){page--;load();}};nextBtn.onclick=()=>{if(page<totalPages){page++;load();}};
filterType.onchange=()=>{page=1;load();};filterCache.onchange=()=>{page=1;load();};filterText.oninput=()=>{page=1;load();};perPage.onchange=()=>{page=1;load();};
document.getElementById('refreshBtn').onclick=load;
document.getElementById('exportBtn').onclick=function(){
  let f=filterType.value,t=filterText.value,cf=filterCache.value,filter=f;if(t)filter=filter?filter+' '+t:t;
  window.location.href='/request-log-export?filter='+encodeURIComponent(filter)+'&cache_filter='+encodeURIComponent(cf);
};
load();
let timer=setInterval(()=>{load();},3000);
autoCheck.onchange=()=>{if(autoCheck.checked)timer=setInterval(()=>{load();},3000);else clearInterval(timer);};
document.addEventListener('click',e=>{if(e.target.classList.contains('url-cell')){
  navigator.clipboard.writeText(e.target.title).then(()=>{e.target.style.color='#4ade80';setTimeout(()=>{e.target.style.color='';},500);});}});
</script></body></html>"""


def _get_cache_table_data():
    rows = []
    cs = _get_cache_stats_data()
    for name, stats in cs.items():
        total = stats["hit"] + stats["miss"] + stats["noinfo"]
        hit_rate = f'{stats["hit"] / total * 100:.1f}%' if total > 0 else "0.0%"
        ages = stats.get("ages", [])
        avg_age = f'{sum(ages) / len(ages):.1f}s' if ages else "-"
        pop = stats.get("last_pop", "-") or "-"
        rows.append({
            "name": name, "hit": stats["hit"], "miss": stats["miss"], "noinfo": stats["noinfo"],
            "hit_rate": hit_rate, "avg_age": avg_age, "top_pop": pop,
        })
    return rows[:500]


# ---------------------------------------------------------------------------
# Ratio panel + Phase preset inject script
# ---------------------------------------------------------------------------
RATIO_PANEL_INJECT = """
<script>
(function(){
  var PANEL_HTML=`
    <style>
      #rp-panel{background:#f5f5f5;border:1px solid #e0e0e0;border-radius:8px;padding:16px 20px;margin:16px auto;max-width:540px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;}
      #rp-panel h3{margin:0 0 12px;font-size:14px;color:#333;font-weight:600;}
      #rp-panel .rp-row{display:flex;align-items:center;gap:10px;margin:8px 0;}
      #rp-panel .rp-label{min-width:90px;font-size:13px;color:#555;}
      #rp-panel input[type=range]{flex:1;height:4px;accent-color:#4caf50;}
      #rp-panel .rp-val{font-weight:700;min-width:24px;text-align:center;font-size:14px;color:#333;}
      #rp-panel .rp-info{text-align:center;font-size:12px;color:#777;margin:8px 0;}
      #rp-panel .rp-info b{color:#333;}
      #rp-panel .rp-bar{display:flex;height:6px;border-radius:3px;overflow:hidden;margin:4px 0 10px;}
      #rp-panel .rp-bar .b1{background:#4caf50;transition:width .3s;} .rp-bar .b2{background:#ff9800;transition:width .3s;}
      #rp-panel .rp-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
      #rp-panel .rp-btn{padding:6px 16px;background:#4caf50;color:#fff;border:none;border-radius:4px;font-weight:600;font-size:12px;cursor:pointer;}
      #rp-panel .rp-btn:hover{background:#43a047;} #rp-panel .rp-btn:disabled{background:#ccc;cursor:not-allowed;}
      #rp-panel .rp-msg{font-size:11px;color:#4caf50;} #rp-panel .rp-msg.err{color:#f44336;}
      #rp-panel .rp-link{font-size:11px;margin-left:auto;} #rp-panel .rp-link a{color:#1976d2;text-decoration:none;}
      #rp-panel .phase-btns{display:flex;gap:6px;margin:10px 0 4px;}
      #rp-panel .phase-btn{padding:5px 14px;border:2px solid #ccc;border-radius:4px;background:#fff;color:#333;font-weight:600;font-size:12px;cursor:pointer;}
      #rp-panel .phase-btn:hover{border-color:#4caf50;background:#e8f5e9;}
      #rp-panel .phase-btn.active{border-color:#4caf50;background:#4caf50;color:#fff;}
    </style>
    <h3>페이즈 프리셋 / 사용자 비율</h3>
    <div class="phase-btns">
      <button type="button" class="phase-btn" data-live="0" data-tm="10">P1 TM 단독</button>
      <button type="button" class="phase-btn" data-live="8" data-tm="2">P2 라이브 8:TM 2</button>
      <button type="button" class="phase-btn" data-live="10" data-tm="0">P3 라이브 단독</button>
    </div>
    <div class="rp-row"><span class="rp-label">라이브</span><input type="range" id="rp-live" min="0" max="10" value="5"><span class="rp-val" id="rp-lv">5</span></div>
    <div class="rp-row"><span class="rp-label">타임머신</span><input type="range" id="rp-tm" min="0" max="10" value="5"><span class="rp-val" id="rp-tv">5</span></div>
    <div class="rp-info"><b id="rp-lp">50</b>% live / <b id="rp-tp">50</b>% timemachine</div>
    <div class="rp-bar"><div class="b1" id="rp-b1" style="width:50%"></div><div class="b2" id="rp-b2" style="width:50%"></div></div>
    <div style="margin:10px 0 4px;border-top:1px solid #e0e0e0;padding-top:10px">
      <div style="font-size:12px;color:#555;font-weight:600;margin-bottom:6px">타임딜레이 범위 (초) — 최대 10,800 (3시간)</div>
      <div style="display:flex;gap:8px;align-items:center">
        <input type="number" id="rp-td-min" min="1" max="10800" value="1" style="width:80px;padding:4px 8px;border:1px solid #ccc;border-radius:4px;font-size:13px">
        <span style="color:#999">~</span>
        <input type="number" id="rp-td-max" min="1" max="10800" value="10800" style="width:80px;padding:4px 8px;border:1px solid #ccc;border-radius:4px;font-size:13px">
        <span style="font-size:11px;color:#999" id="rp-td-info">= 1500 캐시키</span>
      </div>
    </div>
    <div style="margin:10px 0 4px;border-top:1px solid #e0e0e0;padding-top:10px">
      <div style="font-size:12px;color:#555;font-weight:600;margin-bottom:6px">스트림 URL 설정</div>
      <div style="display:flex;flex-direction:column;gap:6px">
        <div style="display:flex;gap:6px;align-items:center">
          <span style="font-size:11px;color:#999;min-width:40px">Host</span>
          <input type="text" id="rp-host" placeholder="https://your-host.com" style="flex:1;padding:4px 8px;border:1px solid #ccc;border-radius:4px;font-size:12px;font-family:monospace">
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <span style="font-size:11px;color:#999;min-width:40px">Path</span>
          <input type="text" id="rp-stream-path" placeholder="/out/v1/channel/vc12" style="flex:1;padding:4px 8px;border:1px solid #ccc;border-radius:4px;font-size:12px;font-family:monospace">
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <span style="font-size:11px;color:#999;min-width:40px">HostHdr</span>
          <input type="text" id="rp-host-header" placeholder="aws-kbo-smart-cloudfront.tving.com (빈칸이면 미사용)" style="flex:1;padding:4px 8px;border:1px solid #ccc;border-radius:4px;font-size:12px;font-family:monospace">
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <span style="font-size:11px;color:#999;min-width:40px">Token</span>
          <input type="text" id="rp-token" placeholder="JWT 토큰 (빈칸이면 토큰 없이 요청)" style="flex:1;padding:4px 8px;border:1px solid #ccc;border-radius:4px;font-size:11px;font-family:monospace">
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <span style="font-size:11px;color:#999;min-width:40px">TM</span>
          <select id="rp-tm-mode" style="padding:4px 6px;border:1px solid #ccc;border-radius:4px;font-size:12px">
            <option value="minus">minus=N (sogne)</option>
            <option value="aws">aws.manifestsettings (MediaPackage)</option>
          </select>
        </div>
        <div style="font-size:11px;color:#999" id="rp-url-preview">전체 URL: -</div>
      </div>
    </div>
    <div style="margin:10px 0 4px;border-top:1px solid #e0e0e0;padding-top:10px">
      <div style="font-size:12px;color:#555;font-weight:600;margin-bottom:6px">Ramp-up 계산기</div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <input type="number" id="rp-calc-users" min="1" value="1000" style="width:80px;padding:4px 8px;border:1px solid #ccc;border-radius:4px;font-size:13px" placeholder="사용자수">
        <span style="font-size:12px;color:#999">명을</span>
        <input type="number" id="rp-calc-time" min="1" value="1" style="width:50px;padding:4px 8px;border:1px solid #ccc;border-radius:4px;font-size:13px">
        <select id="rp-calc-unit" style="padding:4px 6px;border:1px solid #ccc;border-radius:4px;font-size:12px"><option value="min">분</option><option value="sec">초</option></select>
        <span style="font-size:12px;color:#999">동안 =</span>
        <span style="font-size:13px;font-weight:700;color:#333" id="rp-calc-result">17/s</span>
      </div>
      <div style="font-size:11px;color:#999;margin-top:4px" id="rp-calc-hint">→ 사용자수: 1000 / Ramp up: 17</div>
    </div>
    <div class="rp-actions">
      <button type="button" class="rp-btn" id="rp-apply">적용</button>
      <span class="rp-msg" id="rp-msg"></span>
      <span class="rp-link"><a href="/cache-dashboard" target="_blank">캐시 대시보드</a> | <a href="/request-log-view" target="_blank">요청 로그</a></span>
    </div>
  `;
  function removePanel(){var el=document.getElementById('rp-panel');if(el)el.remove();}
  function initSliders(){
    var ls=document.getElementById('rp-live'),ts=document.getElementById('rp-tm');if(!ls)return;
    var lv=document.getElementById('rp-lv'),tv=document.getElementById('rp-tv');
    var lp=document.getElementById('rp-lp'),tp=document.getElementById('rp-tp');
    var b1=document.getElementById('rp-b1'),b2=document.getElementById('rp-b2');
    var btn=document.getElementById('rp-apply'),msg=document.getElementById('rp-msg');
    function upd(){var a=+ls.value,b=+ts.value;lv.textContent=a;tv.textContent=b;var t=a+b||1,p=Math.round(a/t*100);lp.textContent=p;tp.textContent=100-p;b1.style.width=p+'%';b2.style.width=(100-p)+'%';
      document.querySelectorAll('#rp-panel .phase-btn').forEach(function(pb){pb.classList.toggle('active',+pb.dataset.live===a&&+pb.dataset.tm===b);});
    }
    ls.oninput=upd;ts.oninput=upd;
    var tdMin=document.getElementById('rp-td-min'),tdMax=document.getElementById('rp-td-max'),tdInfo=document.getElementById('rp-td-info');
    function updTd(){var mn=+tdMin.value,mx=+tdMax.value;var keys=(mx-mn+1)*15;tdInfo.textContent='= '+keys+' 캐시키';}
    tdMin.oninput=updTd;tdMax.oninput=updTd;
    var calcUsers=document.getElementById('rp-calc-users'),calcTime=document.getElementById('rp-calc-time'),calcUnit=document.getElementById('rp-calc-unit'),calcResult=document.getElementById('rp-calc-result'),calcHint=document.getElementById('rp-calc-hint');
    function getLocustInputs(){var inputs=document.querySelectorAll('input[name]');var u=null,r=null;inputs.forEach(function(el){if(el.name==='userCount')u=el;if(el.name==='spawnRate')r=el;});return{users:u,rate:r};}
    function setNativeValue(el,val){var nativeSetter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;nativeSetter.call(el,val);el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));}
    function updCalc(){var u=+calcUsers.value,t=+calcTime.value,sec=calcUnit.value==='min'?t*60:t;var rate=Math.max(1,Math.ceil(u/sec));calcResult.textContent=rate+'/초';calcHint.textContent='→ 사용자수: '+u+'명 / Ramp up: '+rate+' ('+sec+'초)';}
    function syncCalcToLocust(){var u=+calcUsers.value,t=+calcTime.value,sec=calcUnit.value==='min'?t*60:t;var rate=Math.max(1,Math.ceil(u/sec));var li=getLocustInputs();if(li.users)setNativeValue(li.users,u);if(li.rate)setNativeValue(li.rate,rate);}
    function syncLocustToCalc(){var li=getLocustInputs();if(li.users&&li.rate){calcUsers.value=li.users.value;var rate=+li.rate.value;if(rate>0){var u=+li.users.value;var sec=Math.max(1,Math.ceil(u/rate));if(sec>=60&&sec%60===0){calcTime.value=sec/60;calcUnit.value='min';}else{calcTime.value=sec;calcUnit.value='sec';}updCalc();}}}
    calcUsers.oninput=updCalc;calcTime.oninput=updCalc;calcUnit.onchange=updCalc;
    setInterval(function(){var li=getLocustInputs();if(li.users){li.users.addEventListener('change',syncLocustToCalc);li.users.addEventListener('input',syncLocustToCalc);li.rate.addEventListener('change',syncLocustToCalc);li.rate.addEventListener('input',syncLocustToCalc);}},2000);
    var hostInput=document.getElementById('rp-host'),pathInput=document.getElementById('rp-stream-path'),urlPreview=document.getElementById('rp-url-preview');
    var tokenInput=document.getElementById('rp-token'),tmModeSelect=document.getElementById('rp-tm-mode');
    var hostHeaderInput=document.getElementById('rp-host-header');
    function updUrlPreview(){var h=hostInput.value.replace(/\/+$/,''),p=pathInput.value;if(p&&!p.startsWith('/'))p='/'+p;var qs=[];if(tokenInput.value)qs.push('token=***');var mode=tmModeSelect.value;qs.push(mode==='minus'?'minus={N}':'aws.manifestsettings=time_delay:{N}');urlPreview.textContent='Live: '+(h||'(host)')+p+'/playlist_3.m3u8?'+qs[0]+' | TM: +'+qs[1];urlPreview.style.color=h?'#4caf50':'#999';}
    hostInput.oninput=updUrlPreview;pathInput.oninput=updUrlPreview;tokenInput.oninput=updUrlPreview;tmModeSelect.onchange=updUrlPreview;
    fetch('/weight-config').then(function(r){return r.json()}).then(function(d){ls.value=d.live_weight;ts.value=d.timemachine_weight;tdMin.value=d.time_delay_min||1;tdMax.value=d.time_delay_max||60;if(d.base_url){try{var u=new URL(d.base_url);hostInput.value=u.origin;pathInput.value=u.pathname;}catch(e){}}if(d.auth_token)tokenInput.value=d.auth_token;if(d.tm_param_mode)tmModeSelect.value=d.tm_param_mode;if(hostHeaderInput)hostHeaderInput.value=d.host_header||'';upd();updTd();updUrlPreview();syncLocustToCalc();}).catch(function(){});
    // Phase preset buttons
    document.querySelectorAll('#rp-panel .phase-btn').forEach(function(pb){
      pb.onclick=function(){ls.value=pb.dataset.live;ts.value=pb.dataset.tm;upd();};
    });
    btn.onclick=function(){btn.disabled=true;msg.className='rp-msg';msg.textContent='적용중...';
      syncCalcToLocust();
      var payload={live_weight:+ls.value,timemachine_weight:+ts.value,time_delay_min:+tdMin.value,time_delay_max:+tdMax.value};
      if(hostInput.value.trim())payload.host=hostInput.value.trim().replace(/\/+$/,'');
      if(pathInput.value.trim())payload.stream_path=pathInput.value.trim();
      payload.auth_token=tokenInput.value.trim();
      payload.tm_param_mode=tmModeSelect.value;
      if(hostHeaderInput)payload.host_header=hostHeaderInput.value.trim();
      fetch('/weight-config',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload)
      }).then(function(r){return r.json()}).then(function(d){msg.className='rp-msg';
        var urlMsg=d.url_changed?' | URL 변경됨':'';
        msg.textContent='완료! L:'+d.live_weight+' T:'+d.timemachine_weight+' 딜레이:'+d.time_delay_min+'~'+d.time_delay_max+'초'+urlMsg;
        if(d.base_url){var u=new URL(d.base_url);hostInput.value=u.origin;pathInput.value=u.pathname;updUrlPreview();}
        setTimeout(function(){msg.textContent='';},5000);
      }).catch(function(){msg.className='rp-msg err';msg.textContent='실패';}).finally(function(){btn.disabled=false;});
    };
  }
  function findStartForm(){var btns=document.querySelectorAll('button[type="submit"]');for(var i=0;i<btns.length;i++){if(btns[i].textContent.trim().toUpperCase()==='START')return btns[i];}return null;}
  function syncPanel(){var startBtn=findStartForm();if(startBtn){if(!document.getElementById('rp-panel')){var panel=document.createElement('div');panel.id='rp-panel';panel.innerHTML=PANEL_HTML;startBtn.parentElement.insertBefore(panel,startBtn);initSliders();}}else{removePanel();}}
  var _syncing=false,_timer=null;
  function startObserver(){syncPanel();new MutationObserver(function(){if(_syncing)return;if(_timer)clearTimeout(_timer);_timer=setTimeout(function(){_syncing=true;try{syncPanel();}finally{_syncing=false;}},300);}).observe(document.getElementById('root'),{childList:true,subtree:true});}
  // --- Inject extra tabs into Locust top nav bar ---
  function injectNavTabs(){
    var navBars=document.querySelectorAll('div.MuiTabs-flexContainer, nav a, [role="tablist"]');
    // Find the actual tab container (Locust uses MUI Tabs)
    var tabContainer=document.querySelector('.MuiTabs-flexContainer');
    if(!tabContainer||document.getElementById('nav-cache-dash'))return;
    var style=window.getComputedStyle(tabContainer.children[0]);
    var baseClass=tabContainer.children[0]?tabContainer.children[0].className:'';
    function makeTab(id,label,href){
      var a=document.createElement('a');
      a.id=id;a.href=href;a.target='_blank';a.textContent=label;
      a.className=baseClass;
      a.style.cssText='color:#a4abaf;text-decoration:none;padding:6px 12px;font-size:13px;font-weight:400;text-transform:uppercase;min-width:auto;letter-spacing:0.02em;display:inline-flex;align-items:center;cursor:pointer;opacity:0.7;border:1px solid #444;border-radius:4px;margin-left:4px;';
      a.onmouseenter=function(){a.style.opacity='1';a.style.color='#fff';};
      a.onmouseleave=function(){a.style.opacity='0.7';a.style.color='#a4abaf';};
      return a;
    }
    tabContainer.appendChild(makeTab('nav-cache-dash','캐시 대시보드','/cache-dashboard'));
    tabContainer.appendChild(makeTab('nav-req-log','요청 로그','/request-log-view'));
  }
  function startNavObserver(){
    injectNavTabs();
    new MutationObserver(function(){injectNavTabs();}).observe(document.getElementById('root'),{childList:true,subtree:true});
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',function(){setTimeout(function(){startObserver();startNavObserver();},500);});
  else setTimeout(function(){startObserver();startNavObserver();},500);
})();
</script>
"""


# ---------------------------------------------------------------------------
# Cache Dashboard HTML (with HIT/MISS latency, rendition, codec, PASS/FAIL)
# ---------------------------------------------------------------------------
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>CloudFront 캐시 대시보드</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f172a;color:#e2e8f0;padding:10px;max-width:100%;margin:0 auto;min-height:100vh}
  .top-nav{display:flex;gap:0;margin-bottom:16px;background:#1e293b;border-radius:8px;overflow:hidden;border:1px solid #334155}
  .top-nav a{color:#94a3b8;text-decoration:none;padding:10px 20px;font-size:.85rem;font-weight:600;transition:all .2s;border-right:1px solid #334155}
  .top-nav a:last-child{border-right:none}
  .top-nav a:hover{color:#e2e8f0;background:#334155}
  .top-nav a.active{color:#38bdf8;background:#0f172a;border-bottom:2px solid #38bdf8}
  .header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
  .header h1{font-size:1.3rem;color:#38bdf8;margin:0}
  .phase-bar{display:flex;gap:6px;align-items:center}
  .phase-bar .pb{padding:6px 16px;border:2px solid #334155;border-radius:6px;background:#1e293b;color:#94a3b8;font-weight:600;cursor:pointer;font-size:.8rem}
  .phase-bar .pb:hover{border-color:#38bdf8;color:#e2e8f0}
  .phase-bar .pb.active{border-color:#4ade80;background:#166534;color:#4ade80}
  .phase-info{color:#64748b;font-size:.75rem}
  .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:8px}
  .card{background:#1e293b;border-radius:8px;padding:14px;text-align:center}
  .card .label{color:#64748b;font-size:.7rem;margin-bottom:2px;text-transform:uppercase;letter-spacing:.5px}
  .card .value{font-size:1.6rem;font-weight:700}
  .card .sub{color:#64748b;font-size:.7rem;margin-top:2px}
  .hit{color:#4ade80} .miss{color:#f87171}
  .rate{font-weight:700} .rate.high{color:#4ade80} .rate.mid{color:#facc15} .rate.low{color:#f87171}
  .tabs{display:flex;gap:0;margin-bottom:8px;border-bottom:2px solid #1e293b}
  .tabs button{background:none;border:none;color:#64748b;padding:8px 20px;font-size:.85rem;font-weight:600;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-2px}
  .tabs button:hover{color:#e2e8f0}
  .tabs button.active{color:#38bdf8;border-bottom-color:#38bdf8}
  .tab-content{display:none} .tab-content.active{display:block}
  .section{background:#1e293b;border-radius:8px;padding:14px;margin-bottom:12px}
  .section h2{font-size:.9rem;color:#94a3b8;margin:0 0 10px}
  table{width:100%;border-collapse:collapse;font-size:.8rem}
  th{text-align:left;padding:6px 10px;border-bottom:2px solid #334155;color:#64748b;font-weight:600;font-size:.75rem;text-transform:uppercase}
  td{padding:6px 10px;border-bottom:1px solid #0f172a}
  tr:hover td{background:#334155}
  .chart-full{background:#1e293b;border-radius:8px;padding:12px;margin-bottom:12px}
  .chart-full h3{font-size:.85rem;margin-bottom:6px;color:#94a3b8}
  .chart-full canvas{width:100%;height:200px}
  .chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
  .chart-box{background:#1e293b;border-radius:8px;padding:12px}
  .chart-box h3{font-size:.85rem;margin-bottom:6px;color:#94a3b8}
  .chart-box canvas{width:100%;height:200px}
  .latency-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
  .lat-box{background:#1e293b;border-radius:8px;padding:14px}
  .lat-box h3{font-size:.85rem;margin-bottom:8px}
  .lat-row{display:flex;justify-content:space-between;padding:3px 0;font-size:.8rem}
  .lat-val{font-weight:700}
  .verdict-pass{color:#4ade80;font-weight:700} .verdict-fail{color:#f87171;font-weight:700} .verdict-warn{color:#facc15;font-weight:700}
  .no-data{color:#475569;font-size:.85rem;padding:20px;text-align:center}
  .dl-btn{position:absolute;top:8px;right:8px;background:#334155;color:#94a3b8;border:none;padding:3px 8px;border-radius:4px;cursor:pointer;font-size:.7rem;opacity:.5;transition:opacity .2s}
  .dl-btn:hover{opacity:1;color:#e2e8f0}
  .chart-full,.chart-box{position:relative}
  .dl-bar{display:flex;gap:8px;align-items:center;margin-bottom:12px}
  .dl-bar button{background:#334155;color:#e2e8f0;border:none;padding:6px 16px;border-radius:4px;cursor:pointer;font-size:.8rem}
  .dl-bar button:hover{background:#475569}
  .live-bar{background:linear-gradient(135deg,#1a1a2e,#16213e);border:1px solid #0f3460;border-radius:8px;padding:12px 16px;margin-bottom:12px;position:relative;overflow:hidden}
  .live-bar::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,#f87171,#fb923c,#facc15,#4ade80);animation:livePulse 2s ease-in-out infinite}
  @keyframes livePulse{0%,100%{opacity:1}50%{opacity:.4}}
  .live-bar .lb-header{display:flex;align-items:center;gap:8px;margin-bottom:8px}
  .live-bar .lb-dot{width:8px;height:8px;border-radius:50%;background:#f87171;animation:blink 1s infinite}
  @keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
  .live-bar .lb-title{font-size:.75rem;color:#f87171;font-weight:700;text-transform:uppercase;letter-spacing:1px}
  .live-bar .lb-ts{font-size:.7rem;color:#475569;margin-left:auto}
  .live-bar .lb-metrics{display:flex;gap:16px;flex-wrap:wrap;align-items:center}
  .live-bar .lb-item{display:flex;flex-direction:column;align-items:center;min-width:70px}
  .live-bar .lb-val{font-size:1.1rem;font-weight:700;color:#e2e8f0}
  .live-bar .lb-label{font-size:.65rem;color:#64748b;text-transform:uppercase;letter-spacing:.5px}
  .live-bar .lb-sep{width:1px;height:28px;background:#334155}
  .live-bar .lb-detail{font-size:.72rem;color:#64748b;margin-top:6px;padding-top:6px;border-top:1px solid #1e293b}
  .live-bar .lb-detail b{color:#94a3b8}
  .live-bar .lb-nodata{color:#475569;font-size:.8rem;text-align:center;padding:4px 0}
</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js"></script>
</head>
<body>
<nav class="top-nav">
  <a href="/">Locust UI</a>
  <a href="/cache-dashboard" class="active">캐시 대시보드</a>
  <a href="/request-log-view">요청 로그</a>
</nav>

<div class="live-bar" id="liveBar">
  <div class="lb-header">
    <span class="lb-dot"></span>
    <span class="lb-title">실시간 (10초)</span>
    <span class="lb-ts" id="lbTs">-</span>
  </div>
  <div id="lbContent"><div class="lb-nodata">데이터 수집중...</div></div>
</div>

<div class="header">
  <h1>캐시 대시보드 <span style="font-size:.7rem;color:#475569;font-weight:400">▲ 실시간 (10초) &nbsp; ▼ 누적 (전체)</span></h1>
  <div class="phase-bar">
    <button class="pb" onclick="setPhase('P1')">P1 TM Only</button>
    <button class="pb" onclick="setPhase('P2')">P2 Mixed</button>
    <button class="pb" onclick="setPhase('P3')">P3 Live</button>
    <span class="phase-info" id="phaseInfo">-</span>
  </div>
</div>

<div class="cards" id="topCards"></div>

<div class="tabs">
  <button class="active" onclick="switchTab('charts',this)">차트</button>
  <button onclick="switchTab('latency',this)">응답속도 & 판정</button>
  <button onclick="switchTab('breakdown',this)">상세분류</button>
</div>

<div id="tab-charts" class="tab-content active">
  <div class="chart-full"><button class="dl-btn" onclick="downloadChartPng('c1','전체RPS')">PNG</button><h3>1. 전체 RPS <small style="font-weight:400;color:#94a3b8">— 초당 처리 요청수. 실측 vs 목표(CCU÷사이클) 비교</small> <span id="c1info" style="font-size:.75rem;font-weight:400"></span></h3><div style="height:200px"><canvas id="c1"></canvas></div></div>
  <div class="chart-grid">
    <div class="chart-box"><button class="dl-btn" onclick="downloadChartPng('c2','히트율')">PNG</button><h3>2. 캐시 히트율 / 미스율 <small style="font-weight:400;color:#94a3b8">— CF 캐시 효율. 95%↑ PASS, 80%↓ FAIL</small></h3><div style="height:200px"><canvas id="c2"></canvas></div></div>
    <div class="chart-box"><button class="dl-btn" onclick="downloadChartPng('c3','호출수')">PNG</button><h3>3. 구간별 호출수 <small style="font-weight:400;color:#94a3b8">— CF MISS + 오리진 도달 호출 건수. MISS와 오리진이 비례하면 캐시 미스가 곧 오리진 부하</small></h3><div style="height:200px"><canvas id="c3"></canvas></div></div>
    <div class="chart-box"><button class="dl-btn" onclick="downloadChartPng('c4','응답속도')">PNG</button><h3>4. HIT / MISS 응답속도 <small style="font-weight:400;color:#94a3b8">— HIT≤100ms PASS, MISS≤500ms PASS</small></h3><div style="height:200px"><canvas id="c4"></canvas></div></div>
    <div class="chart-box"><button class="dl-btn" onclick="downloadChartPng('c5','에러율')">PNG</button><h3>5. 에러율 <small style="font-weight:400;color:#94a3b8">— 0.1%↓ PASS, 1%↑ FAIL. 4xx/5xx 모니터링</small></h3><div style="height:200px"><canvas id="c5"></canvas></div></div>
  </div>
  <div class="chart-full"><button class="dl-btn" onclick="downloadChartPng('c8m','Master응답분포1초')">PNG</button><h3>6. Master m3u8 응답시간 분포 (1초 버킷) <small style="font-weight:400;color:#94a3b8">— master playlist 응답속도별 요청 건수</small></h3><div style="height:260px"><canvas id="c8m"></canvas></div></div>
  <div class="chart-full"><button class="dl-btn" onclick="downloadChartPng('c8c','Child응답분포1초')">PNG</button><h3>7. Child m3u8 응답시간 분포 (1초 버킷) <small style="font-weight:400;color:#94a3b8">— rendition playlist 응답속도별 요청 건수</small></h3><div style="height:260px"><canvas id="c8c"></canvas></div></div>
</div>
<div id="tab-latency" class="tab-content"></div>
<div id="tab-breakdown" class="tab-content"></div>

<div style="display:flex;gap:8px;align-items:center;margin-top:12px">
  <button type="button" onclick="downloadFullScreenshot()" style="background:#2563eb;color:#fff;border:none;padding:6px 16px;border-radius:4px;cursor:pointer;font-size:.8rem;font-weight:600">전체 스크린샷 PNG</button>

  <button type="button" onclick="downloadSnapshot()" style="background:#334155;color:#e2e8f0;border:none;padding:6px 16px;border-radius:4px;cursor:pointer;font-size:.8rem">데이터 JSON</button>
  <select id="prevResults" style="background:#1e293b;color:#e2e8f0;border:1px solid #334155;padding:5px 10px;border-radius:4px;font-size:.8rem"><option value="">-- 이전 결과 --</option></select>
  <button type="button" onclick="downloadPrev()" style="background:#334155;color:#e2e8f0;border:none;padding:6px 12px;border-radius:4px;cursor:pointer;font-size:.8rem">다운로드</button>
  <span style="color:#334155;font-size:.7rem">자동 새로고침 3초</span>
</div>

<script>
function downloadChartPng(canvasId,filename){
  var chart=chartInstances[canvasId];if(!chart)return;
  var a=document.createElement('a');a.href=chart.toBase64Image('image/png',1.0);a.download=(filename||canvasId)+'_'+new Date().toISOString().slice(0,19).replace(/[:.]/g,'-')+'.png';a.click();
}
function downloadFullScreenshot(){
  var btn=event.target;btn.textContent='캡처중...';btn.disabled=true;
  html2canvas(document.body,{backgroundColor:'#0f172a',scale:2,useCORS:true,logging:false,windowWidth:1400}).then(function(canvas){
    var a=document.createElement('a');a.href=canvas.toDataURL('image/png');a.download='cache_dashboard_'+new Date().toISOString().slice(0,19).replace(/[:.]/g,'-')+'.png';a.click();
  }).finally(function(){btn.textContent='전체 스크린샷 PNG';btn.disabled=false;});
}

function switchTab(id,btn){
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tabs button').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+id).classList.add('active');
  btn.classList.add('active');
}
function setPhase(p){
  var pr={P1:{live:0,tm:10},P2:{live:8,tm:2},P3:{live:10,tm:0}}[p];if(!pr)return;
  fetch('/weight-config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({live_weight:pr.live,timemachine_weight:pr.tm,phase:p})
  }).then(r=>r.json()).then(()=>refresh()).catch(()=>{});
}
function rc(r){return r>=95?'high':r>=80?'mid':'low';}
function vc(v){return v==='PASS'?'verdict-pass':v==='FAIL'?'verdict-fail':'verdict-warn';}
var chartInstances={};
var chartDarkTheme={grid:{color:'#1e293b'},ticks:{color:'#64748b',font:{size:10}}};
function syncCrosshair(chartId){
  return {id:'syncCrosshair',afterEvent:function(chart,args){
    if(args.event.type==='mousemove'||args.event.type==='mouseout'){
      Object.keys(chartInstances).forEach(function(k){
        if(k===chartId)return;var c=chartInstances[k];if(!c)return;
        if(args.event.type==='mouseout'){c.setActiveElements([]);c.tooltip.setActiveElements([],{x:0,y:0});c.update('none');return;}
        var elements=[];c.data.datasets.forEach(function(ds,di){
          var meta=c.getDatasetMeta(di);if(!meta.hidden){var idx=chart.tooltip&&chart.tooltip.dataPoints?chart.tooltip.dataPoints[0].dataIndex:0;
            if(meta.data[idx])elements.push({datasetIndex:di,index:idx});}});
        if(elements.length){c.setActiveElements(elements);c.tooltip.setActiveElements(elements,{x:c.getDatasetMeta(0).data[elements[0].index].x,y:0});c.update('none');}
      });
    }
  }};
}
function makeChart(canvasId,cfg){
  var el=document.getElementById(canvasId);if(!el)return;
  if(chartInstances[canvasId]){
    // Update existing chart data in-place (no flicker)
    var chart=chartInstances[canvasId];
    chart.data.labels=cfg.data.labels;
    cfg.data.datasets.forEach(function(ds,i){
      if(chart.data.datasets[i]){chart.data.datasets[i].data=ds.data;}
    });
    chart.update('none');
    return;
  }
  cfg.options=cfg.options||{};cfg.options.responsive=true;cfg.options.maintainAspectRatio=false;
  cfg.options.interaction={mode:'index',intersect:false};
  cfg.options.plugins=cfg.options.plugins||{};
  cfg.options.plugins.tooltip={mode:'index',intersect:false,backgroundColor:'#1e293b',titleColor:'#e2e8f0',bodyColor:'#e2e8f0',borderColor:'#334155',borderWidth:1,padding:8,bodySpacing:4};
  cfg.options.plugins.legend={display:true,labels:{color:'#94a3b8',boxWidth:10,padding:8,font:{size:11}}};
  cfg.plugins=[syncCrosshair(canvasId)];
  cfg.options.animation={duration:0};
  chartInstances[canvasId]=new Chart(el,cfg);
}
function renderLiveBar(rt){
  var el=document.getElementById('lbContent');
  if(!rt||!rt.ts){el.innerHTML='<div class="lb-nodata">데이터 수집중...</div>';return;}
  document.getElementById('lbTs').textContent=rt.ts+' 기준';
  var h='<div class="lb-metrics">';
  var rpsColor=rt.target_rps>0?(rt.rps>=rt.target_rps*0.9?'#4ade80':rt.rps>=rt.target_rps*0.7?'#facc15':'#f87171'):'#38bdf8';
  var rpsPct=rt.target_rps>0?Math.round(rt.rps/rt.target_rps*100):'-';
  h+='<div class="lb-item"><span class="lb-val" style="color:'+rpsColor+'">'+rt.rps+'</span><span class="lb-label">RPS (목표 '+rt.target_rps+')</span></div>';
  h+='<div class="lb-item"><span class="lb-val" style="color:'+rpsColor+'">'+rpsPct+'%</span><span class="lb-label">RPS 달성률</span></div>';
  h+='<span class="lb-sep"></span>';
  h+='<div class="lb-item"><span class="lb-val" style="color:#4ade80">'+rt.hit_rps+'</span><span class="lb-label">CF HIT (캐시응답)/초</span></div>';
  h+='<div class="lb-item"><span class="lb-val" style="color:#f87171">'+rt.miss_rps+'</span><span class="lb-label">CF MISS (오리진행)/초</span></div>';
  h+='<span class="lb-sep"></span>';
  var hrc=rt.hit_rate>=80?'#4ade80':rt.hit_rate>=50?'#facc15':'#f87171';
  h+='<div class="lb-item"><span class="lb-val" style="color:'+hrc+'">'+rt.hit_rate+'%</span><span class="lb-label">히트율</span></div>';
  h+='<span class="lb-sep"></span>';
  var cfa=rt.cf_actual||{};var cfHit=cfa.hit_ms||0;var cfMiss=cfa.miss_ms||0;
  var hitDelay=rt.hit_avg_ms>0&&cfHit>0?Math.round(rt.hit_avg_ms-cfHit):0;
  var missDelay=rt.miss_avg_ms>0&&cfMiss>0?Math.round(rt.miss_avg_ms-cfMiss):0;
  h+='<div class="lb-item"><span class="lb-val" style="color:#4ade80">'+cfHit+'ms</span><span class="lb-sub" style="color:#64748b;font-size:.7rem"> +'+Math.max(0,hitDelay)+'ms</span><span class="lb-label">HIT CF응답 <small>+지연</small></span></div>';
  h+='<div class="lb-item"><span class="lb-val" style="color:#f87171">'+cfMiss+'ms</span><span class="lb-sub" style="color:#64748b;font-size:.7rem"> +'+Math.max(0,missDelay)+'ms</span><span class="lb-label">MISS CF응답 <small>+지연</small></span></div>';
  h+='<span class="lb-sep"></span>';
  var erc=rt.error_rate<=0.1?'#4ade80':rt.error_rate<=1.0?'#facc15':'#f87171';
  h+='<div class="lb-item"><span class="lb-val" style="color:'+erc+'">'+rt.error_rate+'%</span><span class="lb-label">에러율</span></div>';
  h+='<span class="lb-sep"></span>';
  // Mbps removed — playlist-only traffic is misleading
  h+='</div>';
  h+='<div class="lb-detail">RPS 상세: <b>'+(rt.total_requests||0).toLocaleString()+'</b>건 / <b>'+rt.interval_sec+'</b>초 = <b>'+(rt.rps||0).toLocaleString()+'/s</b> &nbsp;( HIT <b>'+(rt.hits||0).toLocaleString()+'</b>건 = <b>'+(rt.hit_rps||0).toLocaleString()+'/s</b> + MISS <b>'+(rt.misses||0).toLocaleString()+'</b>건 = <b>'+(rt.miss_rps||0).toLocaleString()+'/s</b>';
  if(rt.errors>0)h+=' + 에러 <b>'+rt.errors+'</b>건';
  h+=' ) &nbsp;│&nbsp; 목표 RPS 계산: <b>'+(rt.user_count||0)+'</b>명 ÷ <b>'+(rt.avg_cycle_sec||'?')+'</b>초(사이클) = <b>'+rt.target_rps+'/s</b></div>';
  el.innerHTML=h;
}
function refresh(){
  fetch('/dashboard-bundle?n=1800').then(r=>{if(!r.ok)throw new Error(r.status);return r.json();}).then(d=>{
    var cache=d.cache,lat=d.latency,pf=d.pass_fail,wc=d.weight,origin=d.origin,mts=d.metrics_ts,rt=d.realtime;
    renderLiveBar(rt);
    var o=cache.overall;
    // Phase
    document.getElementById('phaseInfo').textContent=(wc.phase||'-')+' L:'+wc.live_weight+' T:'+wc.timemachine_weight;
    document.querySelectorAll('.phase-bar .pb').forEach(b=>b.classList.toggle('active',b.textContent.includes(wc.phase||'---')));
    // Top cards
    var cards='';
    cards+='<div class="card"><div class="label">히트율</div><div class="value rate '+rc(o.hit_rate)+'">'+o.hit_rate+'%</div><div class="sub">'+(o.hits||0).toLocaleString()+' / '+(o.total||0).toLocaleString()+'</div></div>';
    var cfA=rt&&rt.cf_actual?rt.cf_actual:{};var cfH=cfA.hit_ms||'-';var cfM=cfA.miss_ms||'-';
    cards+='<div class="card"><div class="label">응답속도</div><div class="value" style="font-size:1.1rem"><span class="hit">'+cfH+'</span> / <span class="miss">'+cfM+'</span></div><div class="sub">CF실제 HIT / MISS ms</div></div>';
    cards+='<div class="card"><div class="label">총 요청수</div><div class="value" style="font-size:1.2rem">'+(o.total||0).toLocaleString()+'</div><div class="sub">평균 수명: '+o.avg_age+'초 | POP: '+o.top_pop+'</div></div>';
    cards+='<div class="card"><div class="label">오리진 / 에러</div><div class="value" style="font-size:1.1rem"><span style="color:'+(origin.current_origin_rps<5?'#4ade80':'#f87171')+'">'+origin.current_origin_rps+'/s</span> <span style="color:'+(lat.error_rate<0.1?'#4ade80':'#f87171')+'">'+lat.error_rate+'%</span></div><div class="sub">오리진 요청/초 | 에러율</div></div>';
    document.getElementById('topCards').innerHTML=cards;

    // Tab: Charts — update data in-place (no flicker)
    if(!mts||mts.length===0){
      // No data — destroy existing charts
      ['c1','c2','c3','c4','c5','c8m','c8c'].forEach(function(id){if(chartInstances[id]){chartInstances[id].destroy();delete chartInstances[id];}});
      var c1info=document.getElementById('c1info');if(c1info)c1info.innerHTML='<span style="color:#475569">데이터 수집중...</span>';
    }
    if(mts&&mts.length>0){
      var lastPt=mts[mts.length-1];
      var c1info=document.getElementById('c1info');
      if(c1info&&lastPt){var pct=lastPt.target_rps>0?Math.round(lastPt.total_rps/lastPt.target_rps*100):'-';var pctColor=pct>=90?'#4ade80':pct>=70?'#facc15':'#f87171';
        var cycSec=lastPt.user_count>0&&lastPt.target_rps>0?(lastPt.user_count/lastPt.target_rps).toFixed(1):'?';
        c1info.innerHTML='<span style="color:#64748b">실측 '+lastPt.total_rps+'/s</span> <span style="color:#64748b">│ 목표 '+lastPt.target_rps+'/s ('+lastPt.user_count+'명 ÷ '+cycSec+'초)</span> <span style="color:'+pctColor+';font-weight:700">│ 달성률 '+pct+'%</span>';}
      var tsL=mts.map(p=>p.ts);
      var missRps=mts.map(p=>Math.max(0,p.total_rps-p.total_rps*(p.hit_rate/100)));
      makeChart('c1',{type:'line',data:{labels:tsL,datasets:[
        {label:'전체 RPS',data:mts.map(p=>p.total_rps),borderColor:'#38bdf8',backgroundColor:'rgba(56,189,248,0.08)',fill:true,borderWidth:2,pointRadius:0,tension:0.3},
        {label:'목표 RPS',data:mts.map(p=>p.target_rps||0),borderColor:'#facc15',borderDash:[6,3],borderWidth:1.5,pointRadius:0,tension:0.3}
      ]},options:{scales:{x:chartDarkTheme,y:{...chartDarkTheme,beginAtZero:true,title:{display:true,text:'RPS',color:'#64748b'}}}}});
      makeChart('c2',{type:'line',data:{labels:tsL,datasets:[
        {label:'히트율 %',data:mts.map(p=>p.hit_rate),borderColor:'#4ade80',backgroundColor:'rgba(74,222,128,0.08)',fill:true,borderWidth:2,pointRadius:0,tension:0.3,yAxisID:'y'},
        {label:'미스율 %',data:mts.map(p=>Math.max(0,100-p.hit_rate)),borderColor:'#f87171',borderDash:[4,4],borderWidth:1.5,pointRadius:0,tension:0.3,yAxisID:'y1'},
        {label:'목표 95%',data:mts.map(()=>95),borderColor:'#facc15',borderDash:[6,3],borderWidth:1,pointRadius:0,yAxisID:'y'}
      ]},options:{scales:{x:chartDarkTheme,y:{...chartDarkTheme,position:'left',min:0,max:100,title:{display:true,text:'히트율 %',color:'#4ade80'}},y1:{...chartDarkTheme,position:'right',min:0,title:{display:true,text:'미스율 %',color:'#f87171'},grid:{drawOnChartArea:false}}}}});
      var fmtNum=function(v){return v.toLocaleString();};
      makeChart('c3',{type:'line',data:{labels:tsL,datasets:[
        {label:'CF MISS 호출수 (구간)',data:mts.map(p=>p.miss_count||0),borderColor:'#fb923c',backgroundColor:'rgba(251,146,60,0.08)',fill:true,borderWidth:2,pointRadius:0,tension:0.3,yAxisID:'y'},
        {label:'오리진 도달 RPS (/s)',data:mts.map(p=>p.origin_rps||0),borderColor:'#f87171',borderDash:[4,4],borderWidth:1.5,pointRadius:0,tension:0.3,yAxisID:'y1'}
      ]},options:{plugins:{tooltip:{mode:'index',intersect:false,callbacks:{label:function(ctx){var v=ctx.parsed.y;var unit=ctx.datasetIndex===0?'건':'/s';return ctx.dataset.label+': '+v.toLocaleString()+unit;}}},legend:{labels:{color:'#94a3b8',boxWidth:10,padding:8}}},scales:{x:chartDarkTheme,y:{...chartDarkTheme,position:'left',beginAtZero:true,title:{display:true,text:'CF MISS 호출수 (건/구간)',color:'#fb923c'},ticks:{callback:fmtNum}},y1:{...chartDarkTheme,position:'right',beginAtZero:true,title:{display:true,text:'오리진 도달 RPS (/s)',color:'#f87171'},ticks:{callback:fmtNum},grid:{drawOnChartArea:false}}}}});
      makeChart('c4',{type:'line',data:{labels:tsL,datasets:[
        {label:'CF HIT',data:mts.map(p=>p.cf_hit_ms||0),borderColor:'#4ade80',borderWidth:2,pointRadius:0,tension:0.3,yAxisID:'y'},
        {label:'CF MISS',data:mts.map(p=>p.cf_miss_ms||0),borderColor:'#f87171',borderWidth:2,pointRadius:0,tension:0.3,yAxisID:'y1'},
        {label:'Locust HIT',data:mts.map(p=>p.hit_avg),borderColor:'#4ade80',borderDash:[4,4],borderWidth:1,pointRadius:0,tension:0.3,yAxisID:'y'},
        {label:'Locust MISS',data:mts.map(p=>p.miss_avg),borderColor:'#f87171',borderDash:[4,4],borderWidth:1,pointRadius:0,tension:0.3,yAxisID:'y1'}
      ]},options:{scales:{x:chartDarkTheme,y:{...chartDarkTheme,position:'left',beginAtZero:true,title:{display:true,text:'HIT ms',color:'#4ade80'}},y1:{...chartDarkTheme,position:'right',beginAtZero:true,title:{display:true,text:'MISS ms',color:'#f87171'},grid:{drawOnChartArea:false}}}}});
      makeChart('c5',{type:'line',data:{labels:tsL,datasets:[
        {label:'에러율 %',data:mts.map(p=>p.error_rate||0),borderColor:'#facc15',backgroundColor:'rgba(250,204,21,0.08)',fill:true,borderWidth:2,pointRadius:0,tension:0.3},
        {label:'경고선 0.1%',data:mts.map(()=>0.1),borderColor:'#fb923c',borderDash:[6,3],borderWidth:1,pointRadius:0},
        {label:'위험선 1.0%',data:mts.map(()=>1.0),borderColor:'#f87171',borderDash:[6,3],borderWidth:1,pointRadius:0}
      ]},options:{scales:{x:chartDarkTheme,y:{...chartDarkTheme,beginAtZero:true,title:{display:true,text:'에러율 %',color:'#facc15'}}}}});
    }
    // master/child m3u8 1초버킷 막대차트 (ok/error 스택)
    var renderSecHist=function(canvasId,hist,barColor){
      if(!hist||!hist.labels||!hist.labels.length){if(chartInstances[canvasId]){chartInstances[canvasId].destroy();delete chartInstances[canvasId];}return;}
      makeChart(canvasId,{type:'bar',data:{labels:hist.labels,datasets:[
        {label:'성공',data:hist.ok||[],backgroundColor:barColor||'rgba(56,189,248,0.80)',borderColor:'#38bdf8',borderWidth:1,stack:'s'},
        {label:'에러',data:hist.error||[],backgroundColor:'rgba(248,113,113,0.75)',borderColor:'#f87171',borderWidth:1,stack:'s'}
      ]},options:{
        plugins:{
          tooltip:{mode:'index',intersect:false,callbacks:{label:function(ctx){return ctx.dataset.label+': '+(ctx.parsed.y||0).toLocaleString()+'건';},footer:function(items){var t=0;items.forEach(function(it){t+=(it.parsed.y||0);});return '합계: '+t.toLocaleString()+'건';}}},
          legend:{display:true,labels:{color:'#94a3b8',boxWidth:10,padding:8,font:{size:11}}}
        },
        scales:{
          x:{...chartDarkTheme,title:{display:true,text:'응답시간 버킷',color:'#94a3b8'}},
          y:{...chartDarkTheme,beginAtZero:true,stacked:true,title:{display:true,text:'요청 건수',color:'#94a3b8'},ticks:{callback:function(v){return v.toLocaleString();}}}
        }
      }});
    };
    renderSecHist('c8m', d.master_sec_hist, 'rgba(250,204,21,0.80)');
    renderSecHist('c8c', d.child_sec_hist,  'rgba(56,189,248,0.80)');

    // Tab: Latency & Judgment
    var lt='';
    // Master/Child m3u8 percentile summary (SSAI 핵심 KPI)
    var ml=d.master_latency||{},cl=d.child_latency||{};
    var ppc=function(v,p,f){if(v<=0)return '';return v<=p?'color:#4ade80':(v>f?'color:#f87171':'color:#facc15');};
    lt+='<div class="section"><h2>M3U8 응답속도 백분위 (SSAI 핵심 KPI)</h2><table><tr><th>레이어</th><th>샘플수</th><th>p50</th><th>p95</th><th>p99</th><th>avg</th><th>max</th><th>에러율</th></tr>';
    [['Master',ml],['Child',cl]].forEach(function(pair){
      var nm=pair[0],s=pair[1]||{};
      lt+='<tr><td><b>'+nm+' m3u8</b></td><td>'+(s.count||0).toLocaleString()+'</td>'
        +'<td style="'+ppc(s.p50||0,150,500)+'">'+(s.p50||0)+' ms</td>'
        +'<td style="'+ppc(s.p95||0,400,1000)+'">'+(s.p95||0)+' ms</td>'
        +'<td style="'+ppc(s.p99||0,800,2000)+'">'+(s.p99||0)+' ms</td>'
        +'<td>'+(s.avg||0)+' ms</td><td>'+(s.max||0)+' ms</td>'
        +'<td>'+(s.error_rate||0)+'%</td></tr>';
    });
    lt+='</table></div>';
    lt+='<div class="latency-grid">';
    var cfA2=rt&&rt.cf_actual?rt.cf_actual:{};
    lt+='<div class="lat-box"><h3 class="hit">HIT 응답속도</h3>';
    lt+='<div class="lat-row"><span>CF 실제</span><span class="lat-val" style="color:#4ade80;font-size:1.1rem">'+(cfA2.hit_ms||'-')+' ms</span></div>';
    lt+='<div class="lat-row"><span>Locust 평균 <small style="color:#64748b">(+지연)</small></span><span class="lat-val" style="color:#64748b">'+lat.hit.avg+' ms</span></div>';
    lt+='<div class="lat-row"><span>건수</span><span class="lat-val">'+lat.hit.count+'</span></div></div>';
    lt+='<div class="lat-box"><h3 class="miss">MISS 응답속도</h3>';
    lt+='<div class="lat-row"><span>CF 실제</span><span class="lat-val" style="color:#f87171;font-size:1.1rem">'+(cfA2.miss_ms||'-')+' ms</span></div>';
    lt+='<div class="lat-row"><span>Locust 평균 <small style="color:#64748b">(+지연)</small></span><span class="lat-val" style="color:#64748b">'+lat.miss.avg+' ms</span></div>';
    lt+='<div class="lat-row"><span>건수</span><span class="lat-val">'+lat.miss.count+'</span></div></div>';
    lt+='</div>';
    lt+='<div class="section"><h2>합격 / 불합격 판정</h2><table><tr><th>항목</th><th>측정값</th><th>합격기준</th><th>불합격기준</th><th>결과</th></tr>';
    pf.forEach(r=>{lt+='<tr><td>'+r.label+'</td><td><b>'+r.value+'</b></td><td>'+r.pass+'</td><td>'+r.fail+'</td><td class="'+vc(r.verdict)+'">'+r.verdict+'</td></tr>';});
    lt+='</table></div>';
    document.getElementById('tab-latency').innerHTML=lt;

    // Tab: Breakdown
    var bk='';
    if(lat.by_codec&&Object.keys(lat.by_codec).length){
      bk+='<div class="section"><h2>코덱별</h2><table><tr><th>코덱</th><th>총건수</th><th>히트율</th><th>HIT 평균</th><th>MISS 평균</th></tr>';
      for(var c in lat.by_codec){var d=lat.by_codec[c];bk+='<tr><td><b>'+c+'</b></td><td>'+d.total.toLocaleString()+'</td><td class="rate '+rc(d.hit_rate)+'">'+d.hit_rate+'%</td><td>'+d.hit.avg+' ms</td><td>'+d.miss.avg+' ms</td></tr>';}
      bk+='</table></div>';
    }
    if(lat.by_rendition&&Object.keys(lat.by_rendition).length){
      bk+='<div class="section"><h2>렌디션별</h2><table><tr><th>렌디션</th><th>총건수</th><th>히트율</th><th>HIT 평균</th><th>MISS 평균</th></tr>';
      for(var r in lat.by_rendition){var d=lat.by_rendition[r];bk+='<tr><td><b>'+r+'</b></td><td>'+d.total.toLocaleString()+'</td><td class="rate '+rc(d.hit_rate)+'">'+d.hit_rate+'%</td><td>'+d.hit.avg+' ms</td><td>'+d.miss.avg+' ms</td></tr>';}
      bk+='</table></div>';
    }
    if(cache.by_user_type&&cache.by_user_type.length){
      bk+='<div class="section"><h2>사용자유형별</h2><table><tr><th>유형</th><th>총건수</th><th>히트</th><th>미스</th><th>히트율</th><th>평균 수명</th></tr>';
      cache.by_user_type.forEach(r=>{bk+='<tr><td><b>'+r.user_type+'</b></td><td>'+r.total.toLocaleString()+'</td><td class="hit">'+r.hits.toLocaleString()+'</td><td class="miss">'+r.misses.toLocaleString()+'</td><td class="rate '+rc(r.hit_rate)+'">'+r.hit_rate+'%</td><td>'+r.avg_age+'s</td></tr>';});
      bk+='</table></div>';
    }
    if(!bk)bk='<div class="no-data">데이터 없음</div>';
    document.getElementById('tab-breakdown').innerHTML=bk;
  }).catch(e=>{
    console.warn('[dashboard] bundle failed, trying individual fetches:',e);
    Promise.all([
      fetch('/cache-stats').then(r=>r.json()),
      fetch('/latency-stats').then(r=>r.json()),
      fetch('/pass-fail').then(r=>r.json()),
      fetch('/weight-config').then(r=>r.json()),
      fetch('/origin-rps').then(r=>r.json()),
      fetch('/metrics-ts?n=1800').then(r=>r.json()),
      fetch('/realtime-stats').then(r=>r.json()),
    ]).then(function([cache,lat,pf,wc,origin,mts,rt]){
      renderLiveBar(rt);
      var o=cache.overall;
      document.getElementById('phaseInfo').textContent=(wc.phase||'-')+' L:'+wc.live_weight+' T:'+wc.timemachine_weight;
      document.querySelectorAll('.phase-bar .pb').forEach(b=>b.classList.toggle('active',b.textContent.includes(wc.phase||'---')));
      var cards='';
      cards+='<div class="card"><div class="label">히트율</div><div class="value rate '+rc(o.hit_rate)+'">'+o.hit_rate+'%</div><div class="sub">'+(o.hits||0).toLocaleString()+' / '+(o.total||0).toLocaleString()+'</div></div>';
      var cfA=rt&&rt.cf_actual?rt.cf_actual:{};var cfH=cfA.hit_ms||'-';var cfM=cfA.miss_ms||'-';
      cards+='<div class="card"><div class="label">응답속도</div><div class="value" style="font-size:1.1rem"><span class="hit">'+cfH+'</span> / <span class="miss">'+cfM+'</span></div><div class="sub">CF실제 HIT / MISS ms</div></div>';
      cards+='<div class="card"><div class="label">총 요청수</div><div class="value" style="font-size:1.2rem">'+(o.total||0).toLocaleString()+'</div><div class="sub">평균 수명: '+o.avg_age+'초 | POP: '+o.top_pop+'</div></div>';
      cards+='<div class="card"><div class="label">오리진 / 에러</div><div class="value" style="font-size:1.1rem"><span style="color:'+(origin.current_origin_rps<5?'#4ade80':'#f87171')+'">'+origin.current_origin_rps+'/s</span> <span style="color:'+(lat.error_rate<0.1?'#4ade80':'#f87171')+'">'+lat.error_rate+'%</span></div><div class="sub">오리진 요청/초 | 에러율</div></div>';
      document.getElementById('topCards').innerHTML=cards;
    }).catch(e2=>{console.error('[dashboard] fallback also failed:',e2);});
  });
}
function downloadSnapshot(){
  fetch('/dashboard-bundle?n=1800').then(r=>r.json()).then(d=>{
    var cache=d.cache,lat=d.latency,pf=d.pass_fail,origin=d.origin,wc=d.weight,mts=d.metrics_ts;
    var snap={meta:{phase:wc.phase,live:wc.live_weight,tm:wc.timemachine_weight,time:new Date().toISOString()},cache:cache,latency:lat,pass_fail:pf,origin:origin,timeseries:mts};
    var blob=new Blob([JSON.stringify(snap,null,2)],{type:'application/json'});
    var a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='snapshot_'+new Date().toISOString().replace(/[:.]/g,'-')+'.json';a.click();
  });
}
function downloadPrev(){var s=document.getElementById('prevResults');if(s.value)window.open('/results/'+s.value);}
fetch('/results').then(r=>r.json()).then(files=>{
  var sel=document.getElementById('prevResults');
  files.forEach(f=>{var o=document.createElement('option');o.value=f;o.textContent=f;sel.appendChild(o);});
});
refresh();setInterval(refresh,3000);
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# Event listeners: init (web UI), test_start
# ---------------------------------------------------------------------------
@events.init.add_listener
def on_init(environment, **kwargs):
    global _global_environment, _BASE_PATH
    _global_environment = environment

    # Locust --host가 주어지면 BASE_URL 자동 동기화
    # 사용법: locust --host https://new-cf.cloudfront.net
    #   + STREAM_PATH=/out/v1/new-channel/vc12 (환경변수)
    #   또는 BASE_URL 전체 지정
    if environment.host:
        host = environment.host.rstrip("/")
        # --host가 config.BASE_URL의 호스트와 다르면 갱신
        from urllib.parse import urlparse as _up
        current_host = f"{_up(config.BASE_URL).scheme}://{_up(config.BASE_URL).netloc}"
        if host != current_host:
            config.BASE_URL = f"{host}{config.STREAM_PATH}"
            config.BASE_PLAYLIST_URL = f"{config.BASE_URL}/playlist.m3u8"
            _BASE_PATH = config.STREAM_PATH
            print(f"  [config] BASE_URL updated: {config.BASE_URL}")
            print(f"  [config] STREAM_PATH: {config.STREAM_PATH}")

    if not environment.web_ui:
        return

    environment.web_ui.template_args["extended_tabs"] = [
        {"title": "Cache Statistics", "key": "cache-statistics"},
        {"title": "Request URLs", "key": "request-urls"},
    ]
    environment.web_ui.template_args["extended_tables"] = [
        {"key": "cache-statistics", "structure": [
            {"key": "name", "title": "이름"}, {"key": "hit", "title": "캐시 히트"},
            {"key": "miss", "title": "캐시 미스"}, {"key": "noinfo", "title": "정보없음"},
            {"key": "hit_rate", "title": "히트율"}, {"key": "avg_age", "title": "평균 수명"},
            {"key": "top_pop", "title": "CF POP"},
        ]},
        {"key": "request-urls", "structure": [
            {"key": "ts", "title": "시간"}, {"key": "name", "title": "이름"},
            {"key": "url", "title": "URL"}, {"key": "status", "title": "상태"},
            {"key": "latency_ms", "title": "응답속도(ms)"}, {"key": "size", "title": "크기"},
            {"key": "cf_cache", "title": "CF 캐시"}, {"key": "cf_pop", "title": "CF POP"},
            {"key": "cache_control", "title": "캐시정책"},
        ]},
    ]
    environment.web_ui.template_args["extended_csv_files"] = [
        {"href": "/cache/csv", "title": "캐시 통계 CSV 다운로드"},
    ]

    @environment.web_ui.app.after_request
    def extend_response(response):
        if flask_request.path == "/stats/requests":
            try:
                data = response.get_json()
                log_data = _get_request_log_data()
                with request_log_lock:
                    # M7: take last 100 directly (avoid full deque→list→reverse)
                    n = len(log_data)
                    start = max(0, n - 100)
                    url_data = [log_data[i] for i in range(n - 1, start - 1, -1)]
                data["extended_stats"] = [
                    {"key": "cache-statistics", "data": _get_cache_table_data()},
                    {"key": "request-urls", "data": url_data},
                ]
                response.set_data(_dumps(data))
            except Exception:
                pass
            return response

        skip_paths = ("/stats/report", "/cache-dashboard", "/request-log-view")
        if response.content_type and "text/html" in response.content_type and not flask_request.path.startswith(skip_paths):
            try:
                html = response.get_data(as_text=True)
                if "</body>" in html and "rp-panel" not in html:
                    html = html.replace("</body>", RATIO_PANEL_INJECT + "</body>")
                    response.set_data(html)
            except Exception:
                pass
        return response

    # --- Routes ---
    @environment.web_ui.app.route("/cache/csv")
    def cache_csv_download():
        headers = ["Name", "Hit", "Miss", "No Info", "Hit Rate", "Avg Age", "CF POP"]
        rows = [",".join(f'"{h}"' for h in headers)]
        for row in _get_cache_table_data():
            rows.append(",".join([f'"{row.get("name","")}"', str(row.get("hit",0)), str(row.get("miss",0)), str(row.get("noinfo",0)),
                f'"{row.get("hit_rate","")}"', f'"{row.get("avg_age","")}"', f'"{row.get("top_pop","")}"']))
        resp = make_response("\n".join(rows))
        resp.headers["Content-Type"] = "text/csv"
        resp.headers["Content-Disposition"] = f"attachment;filename=cache-{timestamp()}.csv"
        return resp

    @environment.web_ui.app.route("/cache-stats")
    def cache_stats_api():
        return Response(_dumps(_get_collector_snapshot()), mimetype="application/json")

    @environment.web_ui.app.route("/cache-dashboard")
    def cache_dashboard():
        return Response(DASHBOARD_HTML, mimetype="text/html")

    @environment.web_ui.app.route("/throughput")
    def throughput_api():
        mbps = throughput.get_throughput_mbps()
        total_bytes = throughput.get_total_bytes()
        return Response(_dumps({"mbps": mbps, "total_bytes": total_bytes, "total_formatted": _format_bytes(total_bytes)}), mimetype="application/json")

    @environment.web_ui.app.route("/latency-stats")
    def latency_stats_api():
        return Response(_dumps(_get_resp_snapshot()), mimetype="application/json")

    @environment.web_ui.app.route("/pass-fail")
    def pass_fail_api():
        return Response(_dumps(_evaluate_pass_fail()), mimetype="application/json")

    @environment.web_ui.app.route("/origin-rps")
    def origin_rps_api():
        return Response(_dumps(_get_origin_snapshot()), mimetype="application/json")

    @environment.web_ui.app.route("/metrics-ts")
    def metrics_ts_api():
        # H4: cap at 1800 points (30min @ 10s) to prevent 1-2MB JSON transfers
        last_n = min(int(flask_request.args.get("n", 120)), 1800)
        return Response(_dumps(metrics_ts.get(last_n)), mimetype="application/json")

    @environment.web_ui.app.route("/realtime-stats")
    def realtime_stats_api():
        data = _get_period_snapshot()
        # Add target RPS based on current user count
        user_count = environment.runner.user_count if environment.runner else 0
        avg_cycle = config.AVG_CYCLE_SEC
        if isinstance(data, dict):
            data["user_count"] = user_count
            data["target_rps"] = round(user_count / avg_cycle, 1) if avg_cycle > 0 else 0
            data["avg_cycle_sec"] = round(avg_cycle, 1)
            # CF 응답속도: resp_tracker (segment HIT/MISS) 우선, CF Sampler 폴백
            snap = _get_resp_snapshot()
            cf_data = cf_sampler.get()
            cf_data["hit_ms"] = snap["hit"]["avg"] if snap["hit"]["count"] > 0 else cf_data.get("hit_ms", 0)
            cf_data["miss_ms"] = snap["miss"]["avg"] if snap["miss"]["count"] > 0 else cf_data.get("miss_ms", 0)
            data["cf_actual"] = cf_data
        return Response(_dumps(data), mimetype="application/json")

    # H3: Consolidated dashboard API — single fetch replaces 7 parallel fetches
    @environment.web_ui.app.route("/dashboard-bundle")
    def dashboard_bundle():
        user_count = environment.runner.user_count if environment.runner else 0
        avg_cycle = config.AVG_CYCLE_SEC
        rt = _get_period_snapshot()
        if isinstance(rt, dict):
            rt["user_count"] = user_count
            rt["target_rps"] = round(user_count / avg_cycle, 1) if avg_cycle > 0 else 0
            rt["avg_cycle_sec"] = round(avg_cycle, 1)
            snap = _get_resp_snapshot()
            cf_data = cf_sampler.get()
            cf_data["hit_ms"] = snap["hit"]["avg"] if snap["hit"]["count"] > 0 else cf_data.get("hit_ms", 0)
            cf_data["miss_ms"] = snap["miss"]["avg"] if snap["miss"]["count"] > 0 else cf_data.get("miss_ms", 0)
            rt["cf_actual"] = cf_data
        bundle = {
            "cache": _get_collector_snapshot(),
            "latency": _get_resp_snapshot(),
            "pass_fail": _evaluate_pass_fail(),
            "weight": {
                "live_weight": LiveUser.weight,
                "timemachine_weight": TimeMachineUser.weight,
                "phase": current_phase["name"],
                "time_delay_min": config.TIME_DELAY_MIN,
                "time_delay_max": config.TIME_DELAY_MAX,
            },
            "origin": _get_origin_snapshot(),
            "metrics_ts": metrics_ts.get(min(int(flask_request.args.get("n", 1800)), 1800)),
            "realtime": rt,
            "histogram": _get_histogram(),
            "seg_histogram": _get_seg_histogram(),
            "master_latency": _get_master_latency_snapshot(),
            "child_latency": _get_child_latency_snapshot(),
            "master_sec_hist": _get_master_sec_hist_snapshot(),
            "child_sec_hist": _get_child_sec_hist_snapshot(),
        }
        return Response(_dumps(bundle), mimetype="application/json")

    @environment.web_ui.app.route("/request-log")
    def request_log_api():
        page = int(flask_request.args.get("page", 1))
        per_page = int(flask_request.args.get("per_page", 50))
        name_filter = flask_request.args.get("filter", "")
        cache_filter = flask_request.args.get("cache_filter", "")
        log_data = _get_request_log_data()
        # M7: snapshot under lock, then process without lock
        with request_log_lock:
            entries = list(log_data)
        if not name_filter and not cache_filter:
            # Fast path: direct slice from end (no reversed() copy)
            total = len(entries)
            total_pages = max(1, (total + per_page - 1) // per_page)
            page = max(1, min(page, total_pages))
            start_from_end = total - (page - 1) * per_page
            end_from_end = max(0, start_from_end - per_page)
            page_entries = entries[end_from_end:start_from_end]
            page_entries.reverse()
        else:
            # Filtered path — iterate once with combined filter
            nf = name_filter.lower() if name_filter else ""
            cf = cache_filter.lower() if cache_filter else ""
            filtered = [e for e in reversed(entries)
                        if (not nf or nf in (e["name"] + " " + e.get("url", "")).lower())
                        and (not cf or cf in e.get("cf_cache", "").lower())]
            total = len(filtered)
            total_pages = max(1, (total + per_page - 1) // per_page)
            page = max(1, min(page, total_pages))
            start = (page - 1) * per_page
            page_entries = filtered[start:start + per_page]
        return Response(_dumps({"entries": page_entries, "page": page, "per_page": per_page, "total": total, "total_pages": total_pages}), mimetype="application/json")

    @environment.web_ui.app.route("/request-log-export")
    def request_log_export():
        import csv, io
        name_filter = flask_request.args.get("filter", "")
        cache_filter = flask_request.args.get("cache_filter", "")
        log_data = _get_request_log_data()
        with request_log_lock:
            all_entries = list(reversed(log_data))
        if name_filter:
            all_entries = [e for e in all_entries if name_filter.lower() in (e["name"] + " " + e.get("url", "")).lower()]
        if cache_filter:
            all_entries = [e for e in all_entries if cache_filter.lower() in (e.get("cf_cache", "")).lower()]
        cols = ["ts", "name", "url", "status", "actual_ms", "overhead_ms", "size", "cf_cache", "cf_pop", "pdt", "pdt_diff", "tm_accuracy", "live_latency", "cache_control"]
        headers = ["시간", "이름", "URL", "상태", "CF응답(ms)", "측정지연(ms)", "크기", "CF캐시", "CF POP", "PDT(KST)", "PDT차이(초)", "TM오차(초)", "라이브지연(초)", "캐시정책"]
        buf = io.StringIO()
        buf.write('\ufeff')  # BOM for Excel Korean
        writer = csv.writer(buf)
        writer.writerow(headers)
        for e in all_entries:
            writer.writerow([e.get(c, "") for c in cols])
        ts = time.strftime("%Y%m%d_%H%M%S")
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=request_log_{ts}.csv"})

    @environment.web_ui.app.route("/request-log-view")
    def request_log_view():
        return Response(REQUEST_LOG_HTML, mimetype="text/html")

    @environment.web_ui.app.route("/weight-config", methods=["GET", "POST"])
    def weight_config():
        global _BASE_PATH
        if flask_request.method == "GET":
            return Response(_dumps({
                "live_weight": LiveUser.weight, "timemachine_weight": TimeMachineUser.weight,
                "phase": current_phase["name"],
                "time_delay_min": config.TIME_DELAY_MIN,
                "time_delay_max": config.TIME_DELAY_MAX,
                "base_url": config.BASE_URL,
                "stream_path": config.STREAM_PATH,
                "auth_token": config.AUTH_TOKEN,
                "tm_param_mode": config.TM_PARAM_MODE,
                "host_header": getattr(config, "HOST_HEADER", "") or "",
            }), mimetype="application/json")

        data = flask_request.get_json(force=True)
        live_w = max(0, min(10, int(data.get("live_weight", LiveUser.weight))))
        tm_w = max(0, min(10, int(data.get("timemachine_weight", TimeMachineUser.weight))))

        LiveUser.weight = live_w
        TimeMachineUser.weight = tm_w

        # Update time_delay range
        if "time_delay_min" in data:
            config.TIME_DELAY_MIN = max(1, int(data["time_delay_min"]))
        if "time_delay_max" in data:
            config.TIME_DELAY_MAX = max(config.TIME_DELAY_MIN, int(data["time_delay_max"]))

        # Update auth token & TM param mode
        if "auth_token" in data:
            config.AUTH_TOKEN = data["auth_token"].strip()
        if "tm_param_mode" in data and data["tm_param_mode"] in ("minus", "aws"):
            config.TM_PARAM_MODE = data["tm_param_mode"]

        # Update Host header override (CF → ALB 우회용)
        if "host_header" in data:
            new_hh = (data.get("host_header") or "").strip()
            config.HOST_HEADER = new_hh
            # 전역 헤더 dict 갱신 — 다음 요청부터 즉시 반영
            global _DEFAULT_HEADERS
            _DEFAULT_HEADERS = {"Host": new_hh} if new_hh else {}
            print(f"  [config] Host header → {new_hh or '(none)'}")

        # Update stream URL (host + path)
        url_changed = False
        new_host = data.get("host", "").strip().rstrip("/")
        new_path = data.get("stream_path", "").strip()
        if new_path and not new_path.startswith("/"):
            new_path = "/" + new_path
        if new_host or new_path:
            if new_path:
                config.STREAM_PATH = new_path
            if new_host:
                config.BASE_URL = f"{new_host}{config.STREAM_PATH}"
            elif new_path:
                # host 안 바꾸고 path만 바꾼 경우
                from urllib.parse import urlparse as _up
                parsed = _up(config.BASE_URL)
                config.BASE_URL = f"{parsed.scheme}://{parsed.netloc}{config.STREAM_PATH}"
            config.BASE_PLAYLIST_URL = f"{config.BASE_URL}/playlist.m3u8"
            _BASE_PATH = config.STREAM_PATH
            url_changed = True
            print(f"  [config] URL changed → {config.BASE_URL}")

        # Update phase
        phase = data.get("phase", "")
        if phase:
            current_phase["name"] = phase
            current_phase["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        else:
            if live_w == 0 and tm_w > 0:
                current_phase["name"] = "P1"
            elif live_w > 0 and tm_w > 0:
                current_phase["name"] = "P2"
            elif live_w > 0 and tm_w == 0:
                current_phase["name"] = "P3"
            else:
                current_phase["name"] = "-"

        return Response(_dumps({
            "live_weight": live_w, "timemachine_weight": tm_w,
            "phase": current_phase["name"],
            "time_delay_min": config.TIME_DELAY_MIN,
            "time_delay_max": config.TIME_DELAY_MAX,
            "base_url": config.BASE_URL,
            "stream_path": config.STREAM_PATH,
            "auth_token": config.AUTH_TOKEN,
            "tm_param_mode": config.TM_PARAM_MODE,
            "host_header": getattr(config, "HOST_HEADER", "") or "",
            "url_changed": url_changed,
        }), mimetype="application/json")

    @environment.web_ui.app.route("/results")
    def results_list():
        import os
        results_dir = os.path.join(os.path.dirname(__file__) or ".", "results")
        if not os.path.isdir(results_dir):
            return Response(_dumps([]), mimetype="application/json")
        files = sorted([f for f in os.listdir(results_dir) if f.endswith(".json")], reverse=True)
        return Response(_dumps(files), mimetype="application/json")

    @environment.web_ui.app.route("/results/<filename>")
    def results_download(filename):
        import os
        if ".." in filename or "/" in filename or "\\" in filename:
            return Response("Invalid", status=400)
        results_dir = os.path.join(os.path.dirname(__file__) or ".", "results")
        filepath = os.path.realpath(os.path.join(results_dir, filename))
        # Ensure resolved path stays within results_dir
        if not filepath.startswith(os.path.realpath(results_dir)):
            return Response("Invalid", status=400)
        if not os.path.isfile(filepath):
            return Response("Not found", status=404)
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        return Response(content, mimetype="application/json",
                        headers={"Content-Disposition": f"attachment; filename={filename}"})

    @environment.web_ui.app.route("/load-shape")
    def load_shape_status():
        runner = environment.runner
        run_time = runner.stats.last_request_timestamp - runner.stats.start_time if runner and runner.stats.start_time else 0
        user_count = runner.user_count if runner else 0
        # Find current stage
        elapsed = 0
        current_stage = None
        for i, stage in enumerate(config.LOAD_STAGES):
            elapsed += stage["duration"]
            if run_time < elapsed:
                current_stage = {"index": i + 1, **stage, "elapsed": round(run_time), "stage_remaining": round(elapsed - run_time)}
                break
        return Response(_dumps({
            "enabled": config.USE_LOAD_SHAPE,
            "user_count": user_count,
            "run_time": round(run_time),
            "current_stage": current_stage,
            "stages": config.LOAD_STAGES,
        }), mimetype="application/json")


# TimeMachine base time — set on test_start, used to calculate time_delay range
_tm_base_time = 0.0


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    global _master_reset_ts, _worker_reset_ts, _tm_base_time
    # Sync reset_ts on test start so worker reports are not rejected as stale
    now = time.time()
    _master_reset_ts = now
    _worker_reset_ts = now
    _tm_base_time = now
    current_phase["test_start_time"] = time.strftime("%Y%m%d_%H%M%S")
    cf_sampler.reset()
    # Go sampler는 master에서만 시작 (worker에서 중복 실행 방지)
    if _is_master() or not _global_environment:
        cf_sampler.start(config.BASE_URL)
    gevent.spawn(cache_reporter, environment)


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    """Auto-save test results as JSON snapshot on test stop."""
    cf_sampler.stop()
    import os
    ts = current_phase.get("test_start_time", time.strftime("%Y%m%d_%H%M%S"))
    phase = current_phase.get("name", "-")
    user_count = environment.runner.user_count if environment.runner else 0

    snapshot = {
        "meta": {
            "phase": phase,
            "user_count": user_count,
            "start_time": ts,
            "end_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "time_delay_range": f"{config.TIME_DELAY_MIN}~{config.TIME_DELAY_MAX}",
            "fetch_segments": config.FETCH_SEGMENTS,
        },
        "cache": _get_collector_snapshot(),
        "latency": _get_resp_snapshot(),
        "pass_fail": _evaluate_pass_fail(),
        "origin": _get_origin_snapshot(),
        "master_latency": _get_master_latency_snapshot(),
        "child_latency": _get_child_latency_snapshot(),
        "master_sec_hist": _get_master_sec_hist_snapshot(),
        "child_sec_hist": _get_child_sec_hist_snapshot(),
        "throughput": {
            "total_bytes": throughput.get_total_bytes(),
            "total_formatted": _format_bytes(throughput.get_total_bytes()),
        },
        "timeseries": metrics_ts.get(9999),
    }

    results_dir = os.path.join(os.path.dirname(__file__) or ".", "results")
    os.makedirs(results_dir, exist_ok=True)
    filename = f"test_{ts}_{phase}_u{user_count}.json"
    filepath = os.path.join(results_dir, filename)

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n{'='*60}")
        print(f"  Test results saved: {filepath}")
    except Exception as e:
        print(f"\n  [WARN] Failed to save results: {e}")
    print(f"{'='*60}\n")
