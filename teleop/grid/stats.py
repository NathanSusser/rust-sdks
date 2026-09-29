"""The one Summary implementation. Every distribution in metrics.json comes from here.

Standard library only: this module is imported on Host B as well.
"""
from __future__ import annotations

import math
from typing import Iterable

FIELDS = ("mean", "p50", "p95", "p99", "max", "min", "n")


def percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank on a sorted list, p in [0, 1]; matches the ad-hoc scripts this replaces."""
    n = len(sorted_values)
    if n == 0:
        raise ValueError("empty")
    idx = int(round(p * (n - 1)))
    return sorted_values[min(n - 1, max(0, idx))]


def summ(values: Iterable[float]) -> dict:
    """mean, p50, p95, p99, max, min, n. Non-finite inputs are dropped. Empty -> nulls, n=0."""
    v = sorted(float(x) for x in values if x is not None and math.isfinite(float(x)))
    if not v:
        return {k: None for k in FIELDS} | {"n": 0}
    return {
        "mean": sum(v) / len(v),
        "p50": percentile(v, 0.50),
        "p95": percentile(v, 0.95),
        "p99": percentile(v, 0.99),
        "max": v[-1],
        "min": v[0],
        "n": len(v),
    }


def sd(values: Iterable[float]) -> float | None:
    v = [float(x) for x in values if x is not None and math.isfinite(float(x))]
    if len(v) < 2:
        return None
    m = sum(v) / len(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / len(v))
