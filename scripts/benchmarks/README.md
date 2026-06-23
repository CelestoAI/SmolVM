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
   `smolvm sandbox create --name probe` once and `smolvm sandbox delete probe`).
3. Linux only: `smolvm setup` configured the host networking and your user can
   talk to Firecracker.

The lifecycle benchmark never escalates with `sudo`. Set things up first.

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

## Foundation Scripts

Use these scripts to see which setup step is slowing down a sandbox start, then
decide where an optimization will matter most. Each script is a small probe,
meaning it measures one part of startup or cleanup on its own. Every probe
supports `--help`, `--dry-run`, `--json`, and optional `--output` so you can
check the command plan before starting any sandbox:

```bash
uv run python scripts/benchmarks/artifacts.py --dry-run --json
uv run python scripts/benchmarks/preset_start.py --preset codex --dry-run --json
uv run python scripts/benchmarks/browser_ready.py --dry-run --json
uv run python scripts/benchmarks/runtime_control.py --operations info,stop,start --dry-run --json
```

`artifacts.py` records metadata for local image and browser files.
`preset_start.py` times `smolvm <preset> start` and cleans up the sandbox unless
`--keep` is set. `browser_ready.py` starts a browser sandbox, then polls the CDP
endpoint, which is the local browser debugging URL, when it is available.
`runtime_control.py` measures public lifecycle commands such as
`smolvm sandbox pause`, `resume`, `stop`, and `start`.

## Linux Networking Stages

Use `networking.py` to decide whether the Rust networking path makes Linux VM
startup faster on your machine. It records each host networking stage, then
compares the native path with the subprocess fallback.

The measured stages include TAP setup (the virtual network device used by
Firecracker), routes (the host paths for guest IPs), sysctls (kernel networking
settings), and nftables-backed NAT (the firewall rules that give the guest
network access):

```bash
uv run python scripts/benchmarks/networking.py --json
uv run python scripts/benchmarks/networking.py --include-full-start --output /tmp/smolvm-networking.json
```

This benchmark is Linux-only and touches real host networking. It expects the
same privileges as Firecracker TAP networking. The `native` mode uses Rust
helpers when direct TAP privileges are available; rerun the benchmark with
`sudo` or another root/CAP_NET_ADMIN launch path to measure that speedup.
`forced-off` sets `SMOLVM_DISABLE_NATIVE_NETWORKING=1`, and
`unprivileged-fallback` is skipped unless native can be attempted without
direct TAP privileges and the existing sudo fallback is available. Run
`smolvm setup` first if the sudo fallback is missing.

## Ubuntu Transport Comparison

Use `ubuntu_transport.py` when comparing SSH vs vsock on the official Ubuntu
preset across QEMU and Firecracker:

```bash
# Measure the currently published image bytes.
uv run python scripts/benchmarks/ubuntu_transport.py --rootfs-source published -v

# Pre-publish measurement: copy the published rootfs and replace /init with
# scripts/ci/preset-init.sh from this checkout before booting.
uv run python scripts/benchmarks/ubuntu_transport.py --rootfs-source current-init -v
```

`current-init` is for PR validation before a new image release exists. It avoids
mistaking a stale published rootfs for the current branch's init behavior.

Add `--include-snapshot` to measure a separate snapshot lane for the same Ubuntu
variants. The fresh-boot results stay under `variants`; snapshot restore results
are reported under `snapshot_variants` with `snapshot_restore_to_ready_ms` and
`snapshot_restore_to_first_command_ms`. The default `--snapshot-type auto` uses
diff snapshots when the runtime can store them safely; QEMU may fall back to a
full snapshot when the active disk has no backing file.
Use `--variants qemu-vsock` or a comma-separated list such as
`--variants qemu-vsock,firecracker-vsock` when you want a focused run.
Each raw Ubuntu transport record includes `boot_telemetry` when the guest image
emits `SMOLVM_TS` markers. The per-variant `summary` also includes
`boot_telemetry_stats`, so readiness changes can be traced to guest phases such
as guest-agent startup, network setup, SSH host-key checks, and sshd startup.
Snapshot runs report the same data as `snapshot_source_boot_telemetry` and
`snapshot_restore_boot_telemetry`.
The command also prints a compact Markdown table after writing JSON. Use it for
quick inspection, and use the JSON when you need exact medians, p90/p95 tail
latency, or per-iteration raw data.

## What each benchmark means

| Benchmark      | Metrics                                                         | What it measures |
|----------------|-----------------------------------------------------------------|------------------|
| `cold-start`   | `host_create_ms`, `vmm_start_ms`, `guest_ready_wait_ms`, `total_fresh_ready_ms`, `first_command_ms`, `total_first_command_ms`, `boot_telemetry_stats` | First VM boot in this process. The image cache on disk is assumed already populated; "cold" means no warm SmolVM state in memory and no per-VM disk overlay yet. |
| `tti`          | same as `cold-start`                                            | Subsequent boots — the steady-state experience. `tti` runs a warm-up boot first (excluded from stats), then takes `--iterations` measurements. Compare `results["tti"]["stats"]["total_fresh_ready_ms"]["p50"]` to `results["cold-start"]["raw"][0]["total_fresh_ready_ms"]` to see the one-time cost. |
| `pause-resume` | `pause_ms`, `resume_ms`                                         | Freeze and unfreeze a long-lived VM. |
| `snapshot`     | `snapshot_create_ms`, `snapshot_restore_ms`, `snapshot_restore_to_ssh_ms` | Persist VM state and bring it back. Each iteration uses a fresh source VM. |

All timings are wall-clock milliseconds via `time.monotonic()`.
Each benchmark reports `{p50, p95, mean, min, max, count}` plus the raw per-iteration values.

For `cold-start` and `tti`, `vmm_start_ms` is only the host VMM process/API
startup. `total_fresh_ready_ms` is the user-facing fresh guest readiness
metric: host create + VMM start + guest boot until SSH is ready. Use
`total_first_command_ms` when comparing end-to-end "sandbox can run work"
latency.

For `cold-start` and `tti`, each raw record also includes `boot_telemetry`
when the guest image emits `SMOLVM_TS` markers from `/init`. This reports guest
uptime at each init stage, stage offsets from `init-start`, named phase
durations, and the last kernel printk timestamp when the runtime log contains
kernel messages.

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
        "host_create_ms": {"p50": ..., "p95": ..., "mean": ..., "min": ..., "max": ..., "count": 5},
        "vmm_start_ms": { ... },
        "guest_ready_wait_ms": { ... },
        "total_fresh_ready_ms": { ... },
        "first_command_ms": { ... },
        "total_first_command_ms": { ... }
      },
      "boot_telemetry_stats": {
        "guest_init_offsets_ms": {
          "sshd-invoked": {"p50": ..., "p95": ..., "mean": ..., "min": ..., "max": ..., "count": 5}
        },
        "guest_init_phases_ms": {
          "ssh_hostkey_check_ms": {"p50": ..., "p95": ..., "mean": ..., "min": ..., "max": ..., "count": 5}
        },
        "kernel_last_printk_s": {"p50": ..., "p95": ..., "mean": ..., "min": ..., "max": ..., "count": 5}
      },
      "raw": [{
        "iter": 0,
        "host_create_ms": ...,
        "vmm_start_ms": ...,
        "guest_ready_wait_ms": ...,
        "total_fresh_ready_ms": ...,
        "first_command_ms": ...,
        "total_first_command_ms": ...,
        "guest_uptime_at_first_command_s": ...,
        "boot_telemetry": {
          "available": true,
          "kernel_last_printk_s": ...,
          "guest_init_markers_s": {"init-start": ..., "sshd-invoked": ...},
          "guest_init_offsets_ms": {"sshd-invoked": ...},
          "guest_init_phases_ms": {"ssh_hostkey_check_ms": ...}
        }
      }, ...]
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
  leak VMs. If a run is killed mid-flight, run `smolvm sandbox list` to spot leftovers.
- **CI**: not currently wired in. Run locally on each platform.
