# SmolVM Benchmarks

Measure the lifecycle timings AI agents actually feel when using SmolVM:
**cold start**, **time-to-interactive**, **pause/resume**, and **snapshot create/restore**.

The suite drives the public Python SDK (`smolvm.facade.SmolVM`) — what it measures
is what users get.

## Backends per platform

| Platform | Backend     | How |
|----------|-------------|-----|
| macOS    | QEMU        | `--backend auto` picks it |
| Linux    | Firecracker | `--backend auto` picks it |

Firecracker on macOS errors out at startup.

## Prerequisites

1. SmolVM installed and `smolvm setup` completed for your platform.
2. The default image is already pulled (`smolvm doctor` will tell you, or run
   `smolvm create --name probe` once and `smolvm delete probe`).
3. Linux only: `smolvm setup` configured the host networking and your user can
   talk to Firecracker.

The bench never escalates with `sudo`. Set things up first.

## Running

```bash
# Everything, default 5 iterations, human-readable table
uv run python scripts/benchmarks/bench.py

# A subset
uv run python scripts/benchmarks/bench.py --only cold-start,tti --iterations 3

# JSON to stdout
uv run python scripts/benchmarks/bench.py --json

# JSON to a file
uv run python scripts/benchmarks/bench.py --output /tmp/smolvm-bench.json

# Force a specific backend (default: auto)
uv run python scripts/benchmarks/bench.py --backend qemu
```

`-v` enables per-iteration progress logging.

## What each benchmark means

| Benchmark      | Metrics                                                         | What it measures |
|----------------|-----------------------------------------------------------------|------------------|
| `cold-start`   | `construct_ms`, `boot_to_ssh_ms`, `total_ms`                    | First VM boot in this process. The image cache on disk is assumed already populated; "cold" means no warm SmolVM state in memory and no per-VM disk overlay yet. |
| `tti`          | same as `cold-start`                                            | Subsequent boots — the steady-state experience. `tti` runs a warm-up boot first (excluded from stats), then takes `--iterations` measurements. Compare `results["tti"]["stats"]["total_ms"]["p50"]` to `results["cold-start"]["raw"][0]["total_ms"]` to see the one-time cost. |
| `pause-resume` | `pause_ms`, `resume_ms`                                         | Freeze and unfreeze a long-lived VM. |
| `snapshot`     | `snapshot_create_ms`, `snapshot_restore_ms`, `snapshot_restore_to_ssh_ms` | Persist VM state and bring it back. Each iteration uses a fresh source VM. |

All timings are wall-clock milliseconds via `time.monotonic()`.
Each benchmark reports `{p50, p95, mean, min, max, count}` plus the raw per-iteration values.

## JSON shape

```json
{
  "smolvm_version": "0.0.10",
  "platform": {"system": "Darwin", "release": "...", "machine": "arm64"},
  "backend": "qemu",
  "iterations": 5,
  "started_at": "2026-04-25T...",
  "duration_s": 213.4,
  "results": {
    "cold-start": {
      "stats": {
        "construct_ms": {"p50": ..., "p95": ..., "mean": ..., "min": ..., "max": ..., "count": 5},
        "boot_to_ssh_ms": { ... },
        "total_ms": { ... }
      },
      "raw": [{"iter": 0, "construct_ms": ..., "boot_to_ssh_ms": ..., "total_ms": ...}, ...]
    },
    "tti": { ... },
    "pause-resume": { "stats": {"pause_ms": ..., "resume_ms": ...}, "raw": [...] },
    "snapshot": {
      "stats": {
        "snapshot_create_ms": ..., "snapshot_restore_ms": ..., "snapshot_restore_to_ssh_ms": ...
      },
      "raw": [...]
    }
  }
}
```

If the backend doesn't support a benchmark (e.g. snapshots on libkrun), the entry
becomes `{"status": "unsupported", "backend": "...", "reason": "..."}`.

## Caveats

- **First-iteration noise**: cold-start iter 0 may include kernel-image fetch from disk
  cache, KVM warm-up, etc. The raw array is preserved so you can spot outliers.
- **Cleanup**: every benchmark wraps work in `try/finally` and stops/deletes its VMs
  even on error. The teardown chain is `vm.delete()` → `SmolVMManager.delete()` →
  direct SIGKILL + DB row removal, so flaky QEMU shutdown paths on macOS don't
  leak VMs. If a run is killed mid-flight, run `smolvm list` to spot leftovers.
- **CI**: not currently wired in. Run locally on each platform.
