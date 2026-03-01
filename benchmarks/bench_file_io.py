"""Benchmark: file read/write throughput inside the VM.

Simulates ralph's JSONL event logging, JSON job files, and session logs.
File IO inside the VM is native ext4 on virtio-blk, but getting results
out requires reading via vm.run("cat ...").
"""

from __future__ import annotations

import os
import sys
import tempfile

from helpers import (
    format_stats,
    overhead_str,
    print_header,
    print_result,
    print_subheader,
    stats_summary,
    time_call,
)

# A 1KB line simulating a JSONL event
_1KB_LINE = '{"ts":1234567890,"event":"prompt","session":"abc123","data":' + '"x' * 470 + '"}\n'
assert len(_1KB_LINE) >= 1000

_10MB_DATA_LINES = 10240  # ~10MB at 1KB per line


def _host_sequential_writes(n: int = 1000) -> float:
    """Append n x 1KB lines to a file on the host."""

    def _do():
        with tempfile.NamedTemporaryFile(mode="w", delete=True, suffix=".jsonl") as f:
            for _ in range(n):
                f.write(_1KB_LINE)
            f.flush()

    return time_call(_do)


def _host_bulk_write() -> float:
    """Write a single ~10MB file on the host."""
    data = _1KB_LINE * _10MB_DATA_LINES

    def _do():
        with tempfile.NamedTemporaryFile(mode="w", delete=True) as f:
            f.write(data)
            f.flush()

    return time_call(_do)


def _host_bulk_read() -> float:
    """Write then read back a ~10MB file on the host."""
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".dat") as f:
        f.write(_1KB_LINE * _10MB_DATA_LINES)
        path = f.name

    def _do():
        with open(path) as f:
            _ = f.read()

    t = time_call(_do)
    os.unlink(path)
    return t


def _host_many_small_files(n: int = 100) -> float:
    """Create n x 1KB files on the host."""

    def _do():
        tmpdir = tempfile.mkdtemp()
        for i in range(n):
            with open(os.path.join(tmpdir, f"job_{i}.json"), "w") as f:
                f.write(_1KB_LINE)
        # Cleanup
        import shutil

        shutil.rmtree(tmpdir)

    return time_call(_do)


def run_benchmark() -> dict:
    """Run all file IO benchmarks and return results."""
    from smolvm import SmolVM

    results = {}

    print_header("File IO Benchmark")

    # baslines on host
    print_subheader("Host: Sequential small writes (1000 x 1KB)")
    host_seq_times = [_host_sequential_writes() for _ in range(5)]
    host_seq_stats = stats_summary(host_seq_times)
    print_result("Stats", format_stats(host_seq_stats))
    results["host_seq_write"] = host_seq_stats

    print_subheader("Host: Bulk write (~10MB)")
    host_bulk_w_times = [_host_bulk_write() for _ in range(5)]
    host_bulk_w_stats = stats_summary(host_bulk_w_times)
    print_result("Stats", format_stats(host_bulk_w_stats))
    results["host_bulk_write"] = host_bulk_w_stats

    print_subheader("Host: Bulk read (~10MB)")
    host_bulk_r_times = [_host_bulk_read() for _ in range(5)]
    host_bulk_r_stats = stats_summary(host_bulk_r_times)
    print_result("Stats", format_stats(host_bulk_r_stats))
    results["host_bulk_read"] = host_bulk_r_stats

    print_subheader("Host: Many small files (100 x 1KB)")
    host_many_times = [_host_many_small_files() for _ in range(5)]
    host_many_stats = stats_summary(host_many_times)
    print_result("Stats", format_stats(host_many_stats))
    results["host_many_files"] = host_many_stats

    # SmolVM
    print_subheader("SmolVM: Starting VM...")
    with SmolVM() as vm:
        # warmup with a sanity check
        sanity = vm.run("echo smolvm_ready")
        assert "smolvm_ready" in sanity.stdout

        # sequential small writes
        print_subheader("SmolVM: Sequential small writes (1000 x 1KB)")
        # We write via a single vm.run command using a shell loop
        write_cmd = (
            "rm -f /tmp/bench.jsonl; "
            "i=0; while [ $i -lt 1000 ]; do "
            "printf '%s\\n' '" + _1KB_LINE.strip().replace("'", "'\\''") + "' >> /tmp/bench.jsonl; "
            "i=$((i+1)); done"
        )
        vm_seq_times = []
        for _ in range(3):
            t = time_call(lambda: vm.run(write_cmd, timeout=120))
            vm_seq_times.append(t)
        vm_seq_stats = stats_summary(vm_seq_times)
        print_result("Stats", format_stats(vm_seq_stats))
        results["vm_seq_write"] = vm_seq_stats

        # Bulk write
        print_subheader("SmolVM: Bulk write (~10MB)")
        # Use dd to write 10MB of data
        bulk_write_cmd = "dd if=/dev/zero of=/tmp/bench_bulk.dat bs=1024 count=10240 2>&1"
        vm_bulk_w_times = []
        for _ in range(3):
            t = time_call(lambda: vm.run(bulk_write_cmd, timeout=60))
            vm_bulk_w_times.append(t)
        vm_bulk_w_stats = stats_summary(vm_bulk_w_times)
        print_result("Stats", format_stats(vm_bulk_w_stats))
        results["vm_bulk_write"] = vm_bulk_w_stats

        # Bulk read reading via vm.run("cat ...")
        print_subheader("SmolVM: Bulk read (~10MB via cat)")
        # First ensure the file exists
        vm.run("dd if=/dev/zero of=/tmp/bench_read.dat bs=1024 count=10240 2>/dev/null", timeout=60)
        vm_bulk_r_times = []
        for _ in range(3):
            t = time_call(lambda: vm.run("cat /tmp/bench_read.dat", timeout=60))
            vm_bulk_r_times.append(t)
        vm_bulk_r_stats = stats_summary(vm_bulk_r_times)
        print_result("Stats", format_stats(vm_bulk_r_stats))
        results["vm_bulk_read"] = vm_bulk_r_stats

        # Many small files
        print_subheader("SmolVM: Many small files (100 x 1KB)")
        escaped_line = _1KB_LINE.strip().replace("'", "'\\''")
        many_files_cmd = (
            "rm -rf /tmp/bench_jobs; mkdir -p /tmp/bench_jobs; "
            "i=0; while [ $i -lt 100 ]; do "
            "printf '%s' '" + escaped_line + "'"
            " > /tmp/bench_jobs/job_$i.json; "
            "i=$((i+1)); done"
        )
        vm_many_times = []
        for _ in range(3):
            t = time_call(lambda: vm.run(many_files_cmd, timeout=120))
            vm_many_times.append(t)
        vm_many_stats = stats_summary(vm_many_times)
        print_result("Stats", format_stats(vm_many_stats))
        results["vm_many_files"] = vm_many_stats

    # compare
    print_subheader("Comparison (p50)")
    print_result(
        "Sequential writes: VM vs Host",
        overhead_str(host_seq_stats["p50"], vm_seq_stats["p50"]),
    )
    print_result(
        "Bulk write: VM vs Host",
        overhead_str(host_bulk_w_stats["p50"], vm_bulk_w_stats["p50"]),
    )
    print_result(
        "Bulk read: VM vs Host",
        overhead_str(host_bulk_r_stats["p50"], vm_bulk_r_stats["p50"]),
    )
    print_result(
        "Many files: VM vs Host",
        overhead_str(host_many_stats["p50"], vm_many_stats["p50"]),
    )

    return results


if __name__ == "__main__":
    if "benchmarks" in __file__:
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    run_benchmark()
