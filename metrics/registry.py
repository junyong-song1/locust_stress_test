from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class MetricDef:
    key: str
    label: str
    unit: str
    source: Callable[[], Any]
    pass_val: Optional[float] = None
    fail_val: Optional[float] = None
    render: str = "value"   # "gauge" | "timeseries" | "percentile" | "histogram" | "table"
    group: str = "general"  # built-in: cache/latency/m3u8/realtime/system; custom: anything else
    higher_is_better: bool = True

    def to_schema(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "unit": self.unit,
            "render": self.render,
            "group": self.group,
            "pass_val": self.pass_val,
            "fail_val": self.fail_val,
            "higher_is_better": self.higher_is_better,
        }


class MetricRegistry:
    """Central metric catalog. Declare a metric here once:
    - /metrics-schema returns the metadata
    - /metrics-data returns current values (flat dict)
    - metrics in non-builtin groups auto-render in dashboard "extra metrics"
    - evaluate_pass_fail() auto-computes pass/fail for all registered metrics

    Builtin groups (handled by hardcoded dashboard): cache, latency, m3u8, realtime, system
    Custom groups (auto-rendered): anything else
    """

    def __init__(self):
        self._metrics: list[MetricDef] = []

    def define(self, **kwargs) -> MetricDef:
        m = MetricDef(**kwargs)
        self._metrics.append(m)
        return m

    def schema(self) -> list[dict]:
        return [m.to_schema() for m in self._metrics]

    def snapshot(self) -> dict:
        result = {}
        for m in self._metrics:
            try:
                result[m.key] = m.source()
            except Exception:
                result[m.key] = None
        return result

    def evaluate_pass_fail(self) -> list[dict]:
        data = self.snapshot()
        results = []
        for i, m in enumerate(self._metrics, 1):
            if m.pass_val is None and m.fail_val is None:
                continue
            val = data.get(m.key)
            if val is None:
                verdict = "WARN"
                val_str = "N/A"
            elif m.higher_is_better:
                verdict = "PASS" if val >= m.pass_val else ("FAIL" if val < m.fail_val else "WARN")
                val_str = f"{val}{m.unit}"
            else:
                verdict = "PASS" if val <= m.pass_val else ("FAIL" if val > m.fail_val else "WARN")
                val_str = f"{val}{m.unit}"
            results.append({
                "id": str(i),
                "key": m.key,
                "label": m.label,
                "value": val_str,
                "pass": f">={m.pass_val}" if m.higher_is_better else f"<={m.pass_val}",
                "fail": f"<{m.fail_val}" if m.higher_is_better else f">{m.fail_val}",
                "verdict": verdict,
            })
        return results


registry = MetricRegistry()
