"""Benchmark: per-command SSH round-trip latency.
Measures the overhead of vm.run() vs native subprocess.
"""

from __future__ import annotations

import subprocess
import sys

try:
    from .helpers import (
        format_stats,
        is_sandbox_exec_available,
        overhead_str,
        print_header,
        print_result,
        print_subheader,
        run_sandboxed,
        stats_summary,
        time_call_n,
    )
except ImportError:
    from helpers import (  # type: ignore[no-redef]
        format_stats,
        is_sandbox_exec_available,
        overhead_str,
        print_header,
        print_result,
        print_subheader,
        run_sandboxed,
        stats_summary,
        time_call_n,
    )

ITERATIONS = 100
WARMUP = 2


def _host_true() -> None:
    subprocess.run(["true"], capture_output=True)


def _host_echo() -> None:
    result = subprocess.run(["echo", "hello"], capture_output=True, text=True)
    assert "hello" in result.stdout


def _sandboxed_true() -> None:
    run_sandboxed("true")


def _sandboxed_echo() -> None:
    result = run_sandboxed("echo hello")
    assert "hello" in result.stdout


def run_benchmark() -> dict:
    """Run all command latency benchmarks and return results."""
    from smolvm import SmolVM

    results = {}

    print_header("Command Latency Benchmark")
    print(f"  Iterations: {ITERATIONS}, Warmup: {WARMUP}")

    # Host baseline
    print_subheader("Host: subprocess.run(['true'])")
    host_true_times = time_call_n(_host_true, ITERATIONS, warmup=WARMUP)
    host_true_stats = stats_summary(host_true_times)
    print_result("Stats", format_stats(host_true_stats))
    results["host_true"] = host_true_stats

    print_subheader("Host: subprocess.run(['echo', 'hello'])")
    host_echo_times = time_call_n(_host_echo, ITERATIONS, warmup=WARMUP)
    host_echo_stats = stats_summary(host_echo_times)
    print_result("Stats", format_stats(host_echo_stats))
    results["host_echo"] = host_echo_stats

    # Sandbox baseline
    if is_sandbox_exec_available():
        print_subheader("Sandbox: sandbox-exec 'true'")
        sandbox_true_times = time_call_n(_sandboxed_true, ITERATIONS, warmup=WARMUP)
        sandbox_true_stats = stats_summary(sandbox_true_times)
        print_result("Stats", format_stats(sandbox_true_stats))
        results["sandbox_true"] = sandbox_true_stats

        print_subheader("Sandbox: sandbox-exec 'echo hello'")
        sandbox_echo_times = time_call_n(_sandboxed_echo, ITERATIONS, warmup=WARMUP)
        sandbox_echo_stats = stats_summary(sandbox_echo_times)
        print_result("Stats", format_stats(sandbox_echo_stats))
        results["sandbox_echo"] = sandbox_echo_stats
    else:
        print("\n  sandbox-exec not available (non-macOS), skipping sandbox baseline")
        results["sandbox_true"] = None
        results["sandbox_echo"] = None

    # SmolVM
    print_subheader("SmolVM: vm.run('true')")
    print("  Starting VM...")
    with SmolVM() as vm:
        # Sanity check
        sanity = vm.run("echo smolvm_sanity")
        assert "smolvm_sanity" in sanity.stdout, f"Sanity failed: {sanity.stdout}"

        def _vm_true():
            vm.run("true")

        def _vm_echo():
            result = vm.run("echo hello")
            assert "hello" in result.stdout

        vm_true_times = time_call_n(_vm_true, ITERATIONS, warmup=WARMUP)
        vm_true_stats = stats_summary(vm_true_times)
        print_result("Stats", format_stats(vm_true_stats))
        results["vm_true"] = vm_true_stats

        print_subheader("SmolVM: vm.run('echo hello')")
        vm_echo_times = time_call_n(_vm_echo, ITERATIONS, warmup=WARMUP)
        vm_echo_stats = stats_summary(vm_echo_times)
        print_result("Stats", format_stats(vm_echo_stats))
        results["vm_echo"] = vm_echo_stats

    # domparison
    print_subheader("Comparison (p50)")
    print_result(
        "vm.run('true') vs host",
        overhead_str(host_true_stats["p50"], vm_true_stats["p50"]),
    )
    print_result(
        "vm.run('echo') vs host",
        overhead_str(host_echo_stats["p50"], vm_echo_stats["p50"]),
    )
    if results.get("sandbox_true"):
        print_result(
            "vm.run('true') vs sandbox",
            overhead_str(results["sandbox_true"]["p50"], vm_true_stats["p50"]),
        )

    return results


if __name__ == "__main__":
    run_benchmark()
