#!/usr/bin/env python3
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

"""Benchmark disk materialization strategies for QEMU on macOS.

Compares the old full-copy approach (qemu-img convert) against the new
thin qcow2 overlay (qemu-img create -b) to quantify the speedup.

Usage:
    uv run python scripts/benchmarks/bench_disk_materialization.py
    uv run python scripts/benchmarks/bench_disk_materialization.py --sizes 256,512,1024,2048
    uv run python scripts/benchmarks/bench_disk_materialization.py --iterations 10 --json
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))


def _find_qemu_img() -> Path | None:
    result = shutil.which("qemu-img")
    return Path(result) if result else None


def _create_test_rootfs(path: Path, size_mb: int) -> None:
    """Create a realistic ext4 image with actual data on disk.

    Writes random data to ~30% of the image to simulate a real rootfs
    (OS files, packages, etc). This gives realistic I/O numbers — sparse
    all-zeros files would make full-copy appear artificially fast.
    """
    filled_mb = max(size_mb // 3, 1)
    subprocess.run(
        ["dd", "if=/dev/urandom", f"of={path}", "bs=1M",
         f"count={filled_mb}", f"seek={size_mb - filled_mb}"],
        capture_output=True, check=True,
    )
    # Truncate to exact target size (dd with seek may leave it short)
    with open(path, "r+b") as f:
        f.truncate(size_mb * 1024 * 1024)


def _time_ms(fn) -> float:
    """Return wall-clock time in ms for calling fn()."""
    start = time.monotonic()
    fn()
    return (time.monotonic() - start) * 1000


def bench_full_convert(qemu_img: Path, source: Path, target: Path) -> float:
    """Old approach: qemu-img convert (full copy)."""
    target.unlink(missing_ok=True)

    def run():
        subprocess.run(
            [str(qemu_img), "convert", "-f", "raw", "-O", "qcow2",
             str(source), str(target)],
            capture_output=True, check=True,
        )

    return _time_ms(run)


def bench_overlay_create(qemu_img: Path, source: Path, target: Path) -> float:
    """New approach: qemu-img create with backing file (thin overlay)."""
    target.unlink(missing_ok=True)

    def run():
        subprocess.run(
            [str(qemu_img), "create", "-f", "qcow2",
             "-b", str(source.resolve()), "-F", "raw",
             str(target)],
            capture_output=True, check=True,
        )

    return _time_ms(run)


def bench_shutil_copy(source: Path, target: Path) -> float:
    """Baseline: Python shutil.copy2 (what Firecracker used before)."""
    target.unlink(missing_ok=True)
    return _time_ms(lambda: shutil.copy2(source, target))


def bench_reflink_copy(source: Path, target: Path) -> float:
    """New Firecracker approach: cp --reflink=auto."""
    target.unlink(missing_ok=True)

    def run():
        result = subprocess.run(
            ["cp", "--reflink=auto", str(source), str(target)],
            capture_output=True, check=False,
        )
        if result.returncode != 0:
            # macOS cp doesn't support --reflink, fall back
            shutil.copy2(source, target)

    return _time_ms(run)


def _stats(times: list[float]) -> dict[str, float]:
    return {
        "mean_ms": round(statistics.mean(times), 2),
        "median_ms": round(statistics.median(times), 2),
        "min_ms": round(min(times), 2),
        "max_ms": round(max(times), 2),
        "stdev_ms": round(statistics.stdev(times), 2) if len(times) > 1 else 0,
    }


def run_benchmark(sizes_mb: list[int], iterations: int) -> dict:
    qemu_img = _find_qemu_img()
    if qemu_img is None:
        print("ERROR: qemu-img not found. Install with: brew install qemu")
        sys.exit(1)

    version = subprocess.run(
        [str(qemu_img), "--version"], capture_output=True, text=True,
    ).stdout.strip().split("\n")[0]

    results: dict = {
        "qemu_img": str(qemu_img),
        "qemu_img_version": version,
        "iterations": iterations,
        "benchmarks": [],
    }

    with tempfile.TemporaryDirectory(prefix="smolvm-bench-") as tmp:
        tmp_path = Path(tmp)

        for size_mb in sizes_mb:
            print(f"\n{'='*60}")
            print(f"  Rootfs size: {size_mb} MB  |  Iterations: {iterations}")
            print(f"{'='*60}")

            source = tmp_path / f"rootfs-{size_mb}.ext4"
            _create_test_rootfs(source, size_mb)

            methods = {
                "qemu-img convert (old)": lambda: bench_full_convert(
                    qemu_img, source, tmp_path / "out.qcow2",
                ),
                "qemu-img overlay (new)": lambda: bench_overlay_create(
                    qemu_img, source, tmp_path / "out.qcow2",
                ),
                "shutil.copy2 (old FC)": lambda: bench_shutil_copy(
                    source, tmp_path / "out.ext4",
                ),
                "cp --reflink (new FC)": lambda: bench_reflink_copy(
                    source, tmp_path / "out.ext4",
                ),
            }

            size_results = {"size_mb": size_mb, "methods": {}}

            for name, bench_fn in methods.items():
                times = []
                # Warmup
                bench_fn()
                for _ in range(iterations):
                    times.append(bench_fn())

                st = _stats(times)
                size_results["methods"][name] = {**st, "raw_ms": times}
                print(f"  {name:30s}  {st['median_ms']:8.2f}ms median  "
                      f"({st['min_ms']:.2f} - {st['max_ms']:.2f})")

            # Speedup
            old_qemu = size_results["methods"]["qemu-img convert (old)"]["median_ms"]
            new_qemu = size_results["methods"]["qemu-img overlay (new)"]["median_ms"]
            if new_qemu > 0:
                speedup = old_qemu / new_qemu
                print(f"\n  QEMU speedup: {speedup:.1f}x faster")
                size_results["qemu_speedup"] = round(speedup, 1)

            old_fc = size_results["methods"]["shutil.copy2 (old FC)"]["median_ms"]
            new_fc = size_results["methods"]["cp --reflink (new FC)"]["median_ms"]
            if new_fc > 0:
                speedup_fc = old_fc / new_fc
                print(f"  Firecracker speedup: {speedup_fc:.1f}x faster")
                size_results["firecracker_speedup"] = round(speedup_fc, 1)

            # Disk usage
            overlay = tmp_path / "out.qcow2"
            if overlay.exists():
                overlay_kb = overlay.stat().st_size / 1024
                print(f"\n  Overlay disk usage: {overlay_kb:.0f} KB "
                      f"(vs {size_mb * 1024} KB base)")
                size_results["overlay_size_kb"] = round(overlay_kb, 1)

            results["benchmarks"].append(size_results)

    return results


def print_summary(results: dict) -> None:
    print(f"\n{'='*60}")
    print("  Summary")
    print(f"{'='*60}")
    print(f"  {results['qemu_img_version']}")
    print()
    print(f"  {'Size':>8s}  {'Old (convert)':>14s}  {'New (overlay)':>14s}  {'Speedup':>8s}")
    print(f"  {'─'*8}  {'─'*14}  {'─'*14}  {'─'*8}")
    for b in results["benchmarks"]:
        old = b["methods"]["qemu-img convert (old)"]["median_ms"]
        new = b["methods"]["qemu-img overlay (new)"]["median_ms"]
        print(f"  {b['size_mb']:>6d}MB  {old:>12.1f}ms  {new:>12.1f}ms  "
              f"{b.get('qemu_speedup', 0):>6.1f}x")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark disk materialization: full copy vs thin overlay"
    )
    parser.add_argument(
        "--sizes",
        default="256,512,1024",
        help="Comma-separated rootfs sizes in MB (default: 256,512,1024)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=5,
        help="Iterations per method per size (default: 5)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    args = parser.parse_args()

    sizes = [int(s.strip()) for s in args.sizes.split(",")]
    results = run_benchmark(sizes, args.iterations)

    if args.json:
        # Strip raw timings for cleaner JSON
        for b in results["benchmarks"]:
            for m in b["methods"].values():
                del m["raw_ms"]
        print(json.dumps(results, indent=2))
    else:
        print_summary(results)


if __name__ == "__main__":
    main()
