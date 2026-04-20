import threading
from collections import defaultdict


class CacheMetricsCollector:
    """Thread-safe singleton for collecting CloudFront cache metrics."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._data_lock = threading.Lock()
        # Cumulative stats (never reset — for dashboard)
        self._hits = defaultdict(int)
        self._misses = defaultdict(int)
        self._age_sum = defaultdict(float)
        self._age_count = defaultdict(int)
        self._pops = defaultdict(lambda: defaultdict(int))
        # Periodic stats (reset each interval — for console)
        self._period_hits = defaultdict(int)
        self._period_misses = defaultdict(int)
        self._period_age_sum = defaultdict(float)
        self._period_age_count = defaultdict(int)
        self._period_pops = defaultdict(lambda: defaultdict(int))
        self._initialized = True

    def record(self, user_type: str, request_type: str,
               x_cache_raw: str = "", age_raw: str = "", pop_raw: str = ""):
        """Record cache metrics from response header values (passed directly)."""
        key = (user_type, request_type)
        x_cache = x_cache_raw.lower()
        age = age_raw or None
        pop = pop_raw

        is_hit = "hit" in x_cache

        with self._data_lock:
            if is_hit:
                self._hits[key] += 1
                self._period_hits[key] += 1
            else:
                self._misses[key] += 1
                self._period_misses[key] += 1

            if age is not None:
                try:
                    age_f = float(age)
                    self._age_sum[key] += age_f
                    self._age_count[key] += 1
                    self._period_age_sum[key] += age_f
                    self._period_age_count[key] += 1
                except (ValueError, TypeError):
                    pass

            if pop:
                self._pops[key][pop] += 1
                self._period_pops[key][pop] += 1

    def _build_rows(self, hits, misses, age_sum, age_count, pops) -> list[dict]:
        all_keys = set(hits) | set(misses)
        results = []
        for key in sorted(all_keys):
            h = hits.get(key, 0)
            m = misses.get(key, 0)
            total = h + m
            hit_rate = (h / total * 100) if total > 0 else 0.0

            ac = age_count.get(key, 0)
            avg_age = (age_sum.get(key, 0.0) / ac) if ac > 0 else 0.0

            pop_counts = pops.get(key, {})
            top_pop = max(pop_counts, key=pop_counts.get) if pop_counts else "-"

            results.append({
                "user_type": key[0],
                "request_type": key[1],
                "total": total,
                "hits": h,
                "misses": m,
                "hit_rate": round(hit_rate, 1),
                "avg_age": round(avg_age, 1),
                "top_pop": top_pop,
            })
        return results

    def reset(self):
        """Clear all cumulative and periodic stats."""
        with self._data_lock:
            self._hits.clear()
            self._misses.clear()
            self._age_sum.clear()
            self._age_count.clear()
            self._pops.clear()
            self._period_hits.clear()
            self._period_misses.clear()
            self._period_age_sum.clear()
            self._period_age_count.clear()
            self._period_pops.clear()

    def reset_cumulative(self):
        """Clear cumulative stats only, preserving periodic counters."""
        with self._data_lock:
            self._hits.clear()
            self._misses.clear()
            self._age_sum.clear()
            self._age_count.clear()
            self._pops.clear()

    def snapshot_periodic(self) -> list[dict]:
        """Return periodic stats and reset period counters."""
        with self._data_lock:
            hits = dict(self._period_hits)
            misses = dict(self._period_misses)
            age_sum = dict(self._period_age_sum)
            age_count = dict(self._period_age_count)
            pops = {k: dict(v) for k, v in self._period_pops.items()}
            self._period_hits.clear()
            self._period_misses.clear()
            self._period_age_sum.clear()
            self._period_age_count.clear()
            self._period_pops.clear()
        return self._build_rows(hits, misses, age_sum, age_count, pops)

    def snapshot_cumulative(self) -> dict:
        """Return cumulative stats grouped by: per-key, per-user_type, and overall."""
        with self._data_lock:
            hits = dict(self._hits)
            misses = dict(self._misses)
            age_sum = dict(self._age_sum)
            age_count = dict(self._age_count)
            pops = {k: dict(v) for k, v in self._pops.items()}

        detail_rows = self._build_rows(hits, misses, age_sum, age_count, pops)

        # Aggregate by user_type
        by_user = defaultdict(lambda: {"hits": 0, "misses": 0, "age_sum": 0.0, "age_count": 0, "pops": defaultdict(int)})
        for key in set(hits) | set(misses):
            ut = key[0]
            by_user[ut]["hits"] += hits.get(key, 0)
            by_user[ut]["misses"] += misses.get(key, 0)
            by_user[ut]["age_sum"] += age_sum.get(key, 0.0)
            by_user[ut]["age_count"] += age_count.get(key, 0)
            for p, c in pops.get(key, {}).items():
                by_user[ut]["pops"][p] += c

        user_rows = []
        for ut in sorted(by_user):
            d = by_user[ut]
            total = d["hits"] + d["misses"]
            user_rows.append({
                "user_type": ut,
                "total": total,
                "hits": d["hits"],
                "misses": d["misses"],
                "hit_rate": round(d["hits"] / total * 100, 1) if total else 0.0,
                "avg_age": round(d["age_sum"] / d["age_count"], 1) if d["age_count"] else 0.0,
                "top_pop": max(d["pops"], key=d["pops"].get) if d["pops"] else "-",
            })

        # Overall aggregate
        total_hits = sum(hits.values())
        total_misses = sum(misses.values())
        total_all = total_hits + total_misses
        total_age_sum = sum(age_sum.values())
        total_age_count = sum(age_count.values())
        all_pops = defaultdict(int)
        for pc in pops.values():
            for p, c in pc.items():
                all_pops[p] += c

        overall = {
            "total": total_all,
            "hits": total_hits,
            "misses": total_misses,
            "hit_rate": round(total_hits / total_all * 100, 1) if total_all else 0.0,
            "avg_age": round(total_age_sum / total_age_count, 1) if total_age_count else 0.0,
            "top_pop": max(all_pops, key=all_pops.get) if all_pops else "-",
        }

        return {
            "detail": detail_rows,
            "by_user_type": user_rows,
            "overall": overall,
        }
