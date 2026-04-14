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

"""Benchmark SmolVM performance across the full VM lifecycle.

Profiles subprocess overhead AND end-to-end VM operations:
- Network setup (TAP, NAT, port forwards)
- VM start (boot + socket ready)
- SSH readiness
- Command execution
- VM stop + delete (including network teardown)

Usage:
    # Static analysis — counts subprocess calls (works on any platform)
    python3 scripts/bench_subprocess.py --dry-run [--vms N]

    # Full lifecycle benchmark (requires Linux with sudo)
    sudo python3 scripts/bench_subprocess.py [--vms N]

    # JSON output for ingestion
    sudo python3 scripts/bench_subprocess.py --json [--vms N]
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from collections import defaultdict
from contextlib import suppress
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure src/ is on the path for editable installs
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

logger = logging.getLogger("bench")


# ── Helpers ──────────────────────────────────────────────────────────


def _percentile(data: list[float], p: float) -> float:
    """Return the p-th percentile of a sorted list (0 <= p <= 1)."""
    if not data:
        return 0.0
    k = int(len(data) * p)
    k = min(k, len(data) - 1)
    return sorted(data)[k]


def _stats(data: list[float]) -> dict[str, float]:
    """Return p50 / p95 / mean / min / max for a list of floats."""
    if not data:
        return {"p50": 0, "p95": 0, "mean": 0, "min": 0, "max": 0}
    return {
        "p50": round(_percentile(data, 0.50), 1),
        "p95": round(_percentile(data, 0.95), 1),
        "mean": round(statistics.mean(data), 1),
        "min": round(min(data), 1),
        "max": round(max(data), 1),
    }


def _timed(label: str, fn, *args, **kwargs):
    """Call fn(*args, **kwargs) and return (result, elapsed_ms)."""
    start = time.monotonic()
    result = fn(*args, **kwargs)
    elapsed_ms = (time.monotonic() - start) * 1000
    logger.info("  %-25s %7.1fms", label, elapsed_ms)
    return result, elapsed_ms


# ── Dry-Run Benchmark ────────────────────────────────────────────────


def _dry_run_benchmark(num_vms: int) -> dict:
    """Count subprocess calls per VM operation without executing them.

    Works on any platform (macOS, Linux without root).
    Traces which run_command() invocations each NetworkManager
    method would make during VM create + delete.
    """
    from smolvm.host.network import NetworkManager

    nm = NetworkManager()
    nm._outbound_interface = "eth0"  # Fake for counting

    # Track calls per phase
    phases: dict[str, dict[str, int]] = {
        "create_tap": defaultdict(int),
        "configure_tap": defaultdict(int),
        "add_route": defaultdict(int),
        "setup_nat": defaultdict(int),
        "setup_ssh_port_forward": defaultdict(int),
        "cleanup_ssh_port_forward": defaultdict(int),
        "cleanup_tap": defaultdict(int),
    }
    current_phase = ""
    all_calls: list[dict] = []

    def _counting_run_command(cmd, **kwargs):
        base = cmd[0] if cmd else "unknown"
        all_calls.append({"phase": current_phase, "cmd": base, "args": list(cmd)})
        if current_phase in phases:
            phases[current_phase][base] += 1
        result = MagicMock()
        result.stdout = ""
        result.returncode = 0
        return result

    with patch("smolvm.host.network.run_command", side_effect=_counting_run_command):
        for i in range(num_vms):
            vm_id = f"bench-vm-{i}"
            guest_ip = f"172.16.0.{i + 2}"
            tap_name = f"tap{i + 2}"

            # ── VM Create path ──
            current_phase = "create_tap"
            with suppress(Exception):
                nm.create_tap(tap_name, "root")

            current_phase = "configure_tap"
            with suppress(Exception):
                nm.configure_tap(tap_name, netmask="32")

            current_phase = "add_route"
            with suppress(Exception):
                nm.add_route(guest_ip, tap_name)

            current_phase = "setup_nat"
            with suppress(Exception):
                nm.setup_nat(tap_name)

            current_phase = "setup_ssh_port_forward"
            with suppress(Exception):
                nm.setup_ssh_port_forward(
                    vm_id=vm_id,
                    guest_ip=guest_ip,
                    host_port=2200 + i,
                )

            # ── VM Delete path ──
            current_phase = "cleanup_ssh_port_forward"
            with suppress(Exception):
                nm.cleanup_ssh_port_forward(vm_id, guest_ip, 2200 + i)

            current_phase = "cleanup_tap"
            with suppress(Exception):
                nm.cleanup_tap(tap_name)

    # Aggregate per-VM
    create_calls = sum(
        sum(v.values()) for k, v in phases.items() if not k.startswith("cleanup")
    ) // max(num_vms, 1)
    delete_calls = sum(
        sum(v.values()) for k, v in phases.items() if k.startswith("cleanup")
    ) // max(num_vms, 1)

    # Flatten per-phase summary
    phase_summary = {}
    for phase, counts in phases.items():
        total = sum(counts.values()) // max(num_vms, 1)
        phase_summary[phase] = {"total": total, "by_cmd": dict(counts)}

    # Count by command type across all calls
    cmd_totals: dict[str, int] = defaultdict(int)
    for call in all_calls:
        cmd_totals[call["cmd"]] += 1
    # Per-VM
    cmd_per_vm = {k: v // max(num_vms, 1) for k, v in cmd_totals.items()}

    return {
        "mode": "dry_run",
        "num_vms": num_vms,
        "per_vm": {
            "create_subprocess_calls": create_calls,
            "delete_subprocess_calls": delete_calls,
            "total_subprocess_calls": create_calls + delete_calls,
            "by_command": cmd_per_vm,
            "by_phase": phase_summary,
        },
        "estimated_overhead_ms": {
            "best_5ms": {
                "create": create_calls * 5,
                "delete": delete_calls * 5,
                "total": (create_calls + delete_calls) * 5,
            },
            "typical_10ms": {
                "create": create_calls * 10,
                "delete": delete_calls * 10,
                "total": (create_calls + delete_calls) * 10,
            },
            "worst_20ms": {
                "create": create_calls * 20,
                "delete": delete_calls * 20,
                "total": (create_calls + delete_calls) * 20,
            },
        },
        "notes": {
            "vm_start": "Not measured in dry-run (requires Firecracker/QEMU)",
            "ssh_ready": "Not measured in dry-run (requires running VM)",
            "cmd_exec": "Not measured in dry-run (requires SSH)",
        },
    }


# ── Live Benchmark ───────────────────────────────────────────────────


def _live_benchmark(num_vms: int) -> dict:
    """Run full VM lifecycle and measure each phase.

    Requires Linux with sudo privileges and SmolVM installed.
    Measures: create, start, ssh_ready, cmd_exec, stop, delete.
    """
    import os
    import platform

    if platform.system() != "Linux":
        print("ERROR: Live benchmark requires Linux. Use --dry-run on macOS.")
        sys.exit(1)

    from smolvm.facade import SmolVM

    vm_results: list[dict] = []

    for i in range(num_vms):
        logger.info("── VM %d/%d ──", i + 1, num_vms)
        timings: dict[str, float] = {}
        vm_id = f"bench-{i}"

        vm = None
        try:
            # Phase: Create + Start (auto-config builds image, creates VM, etc.)
            # SmolVM() with no args = auto-configure a fresh SSH-ready VM.
            vm, timings["create_ms"] = _timed(
                "create (auto-config)",
                SmolVM,
            )

            # Phase: Start (boot + Firecracker/QEMU process ready)
            _, timings["start_ms"] = _timed(
                "start",
                vm.start,
            )

            # Phase: SSH ready
            _, timings["ssh_ready_ms"] = _timed(
                "wait_for_ssh",
                vm.wait_for_ssh,
                timeout=30.0,
            )

            vm_id = vm._vm_id
            logger.info("  VM ID: %s", vm_id)

            # Phase: Command execution (simple echo — first call, cold SSH)
            result, timings["cmd_exec_ms"] = _timed(
                "run(echo hello)",
                vm.run,
                "echo hello",
            )

            # Phase: Second command (warm SSH, connection reuse)
            _, timings["cmd_exec_2_ms"] = _timed(
                "run(uname -a)",
                vm.run,
                "uname -a",
            )

            # Phase: Stop
            _, timings["stop_ms"] = _timed(
                "stop",
                vm.stop,
            )

            # Phase: Delete (includes network teardown)
            _, timings["delete_ms"] = _timed(
                "delete",
                vm.delete,
            )
            vm = None  # Already deleted

        except Exception as e:
            timings["error"] = str(e)
            logger.warning("VM %s failed: %s", vm_id, e)
        finally:
            if vm is not None:
                with suppress(Exception):
                    vm.stop()
                with suppress(Exception):
                    vm.delete()

        # Derived
        timings["total_ms"] = sum(
            v for k, v in timings.items() if k.endswith("_ms") and k != "total_ms"
        )
        timings["network_overhead_ms"] = timings.get("create_ms", 0) + timings.get("delete_ms", 0)

        vm_results.append({"vm_id": vm_id, **timings})

    # Aggregate
    def _collect(key: str) -> list[float]:
        return [r[key] for r in vm_results if key in r and not isinstance(r[key], str)]

    phases = [
        "create_ms",
        "start_ms",
        "ssh_ready_ms",
        "cmd_exec_ms",
        "cmd_exec_2_ms",
        "stop_ms",
        "delete_ms",
        "total_ms",
        "network_overhead_ms",
    ]

    summary = {}
    for phase in phases:
        values = _collect(phase)
        if values:
            summary[phase] = _stats(values)

    return {
        "mode": "live",
        "num_vms": num_vms,
        "summary": summary,
        "per_vm": vm_results,
    }


# ── Reporting ────────────────────────────────────────────────────────


def print_report(results: dict) -> None:
    """Print a human-readable report."""
    print("\n" + "=" * 65)
    print("SmolVM Benchmark Report")
    print("=" * 65)

    mode = results["mode"]
    num_vms = results["num_vms"]

    print(f"\nMode: {mode}")
    print(f"VMs: {num_vms}")

    if mode == "dry_run":
        pv = results["per_vm"]
        print(f"\n── Subprocess Calls Per VM ──")
        print(f"  Create:  {pv['create_subprocess_calls']:3d} calls")
        print(f"  Delete:  {pv['delete_subprocess_calls']:3d} calls")
        print(f"  Total:   {pv['total_subprocess_calls']:3d} calls")

        print(f"\n── By Command Type (per VM) ──")
        for cmd, count in sorted(pv["by_command"].items(), key=lambda x: -x[1]):
            print(f"  {cmd:15s} {count:3d} calls")

        print(f"\n── By Phase (per VM) ──")
        for phase, info in pv["by_phase"].items():
            if info["total"] > 0:
                print(f"  {phase:30s} {info['total']:3d} calls")

        print(f"\n── Estimated Overhead (per VM) ──")
        est = results["estimated_overhead_ms"]
        print(f"  {'Scenario':20s} {'Create':>8s} {'Delete':>8s} {'Total':>8s}")
        print(f"  {'-' * 20} {'-' * 8} {'-' * 8} {'-' * 8}")
        for label in ["best_5ms", "typical_10ms", "worst_20ms"]:
            e = est[label]
            print(f"  {label:20s} {e['create']:6d}ms {e['delete']:6d}ms {e['total']:6d}ms")

        print(f"\n── Notes ──")
        for k, v in results["notes"].items():
            print(f"  {k}: {v}")

    elif mode == "live":
        summary = results["summary"]
        print(f"\n── Lifecycle Timing (ms) ──")
        print(f"  {'Phase':25s} {'p50':>8s} {'p95':>8s} {'mean':>8s} {'min':>8s} {'max':>8s}")
        print(f"  {'-' * 25} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 8}")

        display_order = [
            ("create_ms", "Create (net setup)"),
            ("start_ms", "Start (boot)"),
            ("ssh_ready_ms", "SSH ready"),
            ("cmd_exec_ms", "Run cmd (1st)"),
            ("cmd_exec_2_ms", "Run cmd (2nd)"),
            ("stop_ms", "Stop"),
            ("delete_ms", "Delete (net teardown)"),
            ("network_overhead_ms", "Network overhead"),
            ("total_ms", "TOTAL"),
        ]

        for key, label in display_order:
            if key in summary:
                s = summary[key]
                sep = "─" if key != "total_ms" else "═"
                if key in ("network_overhead_ms", "total_ms"):
                    print(f"  {sep * 25} {sep * 8} {sep * 8} {sep * 8} {sep * 8} {sep * 8}")
                print(
                    f"  {label:25s} {s['p50']:7.1f}  {s['p95']:7.1f}  "
                    f"{s['mean']:7.1f}  {s['min']:7.1f}  {s['max']:7.1f}"
                )

    print("\n" + "=" * 65)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark SmolVM performance across the full VM lifecycle"
    )
    parser.add_argument(
        "--vms",
        type=int,
        default=5,
        help="Number of VMs to benchmark (default: 5)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count subprocess calls without executing (any platform)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(message)s",
    )

    if args.dry_run:
        results = _dry_run_benchmark(args.vms)
    else:
        results = _live_benchmark(args.vms)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_report(results)


if __name__ == "__main__":
    main()
