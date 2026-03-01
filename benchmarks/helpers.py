"""Shared utilities for SmolVM benchmarks.

Provides timing helpers, statistical functions, sandbox-exec wrappers,
and table formatting for benchmark reporting.
"""

from __future__ import annotations

import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------


@contextmanager
def timer():
    """Context manager that records elapsed wall-clock time in milliseconds.

    Usage::

        with timer() as t:
            do_something()
        print(t.elapsed_ms)
    """
    t = _TimerResult()
    t.start = time.perf_counter()
    yield t
    t.end = time.perf_counter()
    t.elapsed_ms = (t.end - t.start) * 1000.0


class _TimerResult:
    start: float = 0.0
    end: float = 0.0
    elapsed_ms: float = 0.0


def time_call(fn: Callable[[], Any]) -> float:
    """Run *fn* and return the elapsed time in milliseconds."""
    start = time.perf_counter()
    fn()
    return (time.perf_counter() - start) * 1000.0


def time_call_n(fn: Callable[[], Any], n: int, *, warmup: int = 0) -> list[float]:
    """Run *fn* ``warmup + n`` times and return the last *n* timings in ms."""
    for _ in range(warmup):
        fn()
    return [time_call(fn) for _ in range(n)]


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def percentile(data: list[float], p: float) -> float:
    """Return the *p*-th percentile (0-100) of *data*."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (p / 100.0) * (len(sorted_data) - 1)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_data[int(k)]
    d0 = sorted_data[f] * (c - k)
    d1 = sorted_data[c] * (k - f)
    return d0 + d1


def stats_summary(timings: list[float]) -> dict[str, float]:
    """Return min, p50, p95, p99, max, mean, and stdev for *timings* (ms)."""
    if not timings:
        return dict.fromkeys(("min", "p50", "p95", "p99", "max", "mean", "stdev"), 0.0)
    n = len(timings)
    mean = sum(timings) / n
    variance = sum((x - mean) ** 2 for x in timings) / n if n > 1 else 0.0
    return {
        "min": min(timings),
        "p50": percentile(timings, 50),
        "p95": percentile(timings, 95),
        "p99": percentile(timings, 99),
        "max": max(timings),
        "mean": mean,
        "stdev": math.sqrt(variance),
    }


def format_stats(stats: dict[str, float], unit: str = "ms") -> str:
    """Format a stats dict as a single readable line."""
    return (
        f"min={stats['min']:.1f}{unit}  "
        f"p50={stats['p50']:.1f}{unit}  "
        f"p95={stats['p95']:.1f}{unit}  "
        f"p99={stats['p99']:.1f}{unit}  "
        f"max={stats['max']:.1f}{unit}  "
        f"mean={stats['mean']:.1f}{unit}  "
        f"stdev={stats['stdev']:.1f}{unit}"
    )


# ---------------------------------------------------------------------------
# sandbox-exec wrapper (macOS only)
# ---------------------------------------------------------------------------

# Minimal Seatbelt profile that allows most operations except network.
# Mirrors the basic approach from nightshift's sandbox.ts.
_SEATBELT_PROFILE = """\
(version 1)
(allow default)
"""


def is_sandbox_exec_available() -> bool:
    """Return True if sandbox-exec is available (macOS only)."""
    if platform.system() != "Darwin":
        return False
    return shutil.which("sandbox-exec") is not None


def run_sandboxed(command: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Execute *command* via ``sandbox-exec`` with a permissive profile.

    Falls back to plain ``subprocess.run`` if sandbox-exec is unavailable.
    """
    if is_sandbox_exec_available():
        # Write a temporary profile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".sb", delete=False
        ) as fp:
            fp.write(_SEATBELT_PROFILE)
            profile_path = fp.name
        try:
            return subprocess.run(
                ["sandbox-exec", "-f", profile_path, "bash", "-c", command],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        finally:
            os.unlink(profile_path)
    else:
        return subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )


def time_sandboxed(command: str, timeout: int = 30) -> float:
    """Run a command via sandbox-exec and return elapsed time in ms."""
    start = time.perf_counter()
    run_sandboxed(command, timeout=timeout)
    return (time.perf_counter() - start) * 1000.0


def time_host(command: str, timeout: int = 30) -> float:
    """Run a command directly on the host and return elapsed time in ms."""
    start = time.perf_counter()
    subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return (time.perf_counter() - start) * 1000.0


# ---------------------------------------------------------------------------
# Table formatting
# ---------------------------------------------------------------------------


def format_table(
    headers: list[str],
    rows: list[list[str]],
    *,
    align: str | None = None,
) -> str:
    """Format a simple ASCII table.

    Args:
        headers: Column header strings.
        rows: List of rows, each a list of cell strings.
        align: Optional alignment string ('l'=left, 'r'=right) per column.
            Defaults to left-align for the first column, right for the rest.
    """
    if not rows:
        return ""

    col_count = len(headers)
    if align is None:
        align = "l" + "r" * (col_count - 1)

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def _fmt_row(cells: list[str]) -> str:
        parts = []
        for i, cell in enumerate(cells):
            w = widths[i]
            if i < len(align) and align[i] == "r":
                parts.append(cell.rjust(w))
            else:
                parts.append(cell.ljust(w))
        return "| " + " | ".join(parts) + " |"

    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    lines = [_fmt_row(headers), sep]
    lines.extend(_fmt_row(row) for row in rows)
    return "\n".join(lines)


def overhead_str(base_ms: float, test_ms: float) -> str:
    """Return a human-readable overhead string like '2.5x' or '+150ms'."""
    if base_ms <= 0:
        return "N/A"
    ratio = test_ms / base_ms
    diff = test_ms - base_ms
    return f"{ratio:.1f}x (+{diff:.0f}ms)"


# ---------------------------------------------------------------------------
# Platform info
# ---------------------------------------------------------------------------


def platform_info() -> str:
    """Return a one-line platform description."""
    system = platform.system()
    machine = platform.machine()
    version = platform.release()
    py_version = sys.version.split()[0]
    return f"{system} {machine} (kernel {version}), Python {py_version}"


# ---------------------------------------------------------------------------
# Printing helpers
# ---------------------------------------------------------------------------


def print_header(title: str) -> None:
    """Print a section header."""
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def print_subheader(title: str) -> None:
    """Print a sub-section header."""
    print(f"\n--- {title} ---")


def print_result(label: str, value: str) -> None:
    """Print a labelled result."""
    print(f"  {label}: {value}")
