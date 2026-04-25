# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Timing helpers for the SmolVM benchmark suite."""

from __future__ import annotations

import statistics
import time


def percentile(values: list[float], p: float) -> float:
    """Return the p-th percentile of values via linear interpolation (0 <= p <= 1)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * p
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def stats(values: list[float]) -> dict[str, float | int]:
    """Return summary statistics over a list of timings (in ms)."""
    if not values:
        return {"p50": 0.0, "p95": 0.0, "mean": 0.0, "min": 0.0, "max": 0.0, "count": 0}
    return {
        "p50": round(percentile(values, 0.50), 1),
        "p95": round(percentile(values, 0.95), 1),
        "mean": round(statistics.mean(values), 1),
        "min": round(min(values), 1),
        "max": round(max(values), 1),
        "count": len(values),
    }


class Phase:
    """Context manager that measures wall-clock time for a block of code.

    Example:
        with Phase("boot") as p:
            vm.start()
        record(p.elapsed_ms)
    """

    def __init__(self, label: str = "") -> None:
        self.label = label
        self.elapsed_ms: float = 0.0
        self._start: float = 0.0

    def __enter__(self) -> Phase:
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.elapsed_ms = (time.monotonic() - self._start) * 1000.0
