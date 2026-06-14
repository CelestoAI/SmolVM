# SmolVM Startup Speedups

This ledger records startup speed changes for the official published Ubuntu
benchmark target. Each startup-related PR should add a row so we can see which
changes moved user-visible latency.

Primary target:

- Image: official published Ubuntu preset.
- Backends: QEMU and Firecracker.
- Transports: SSH and vsock.
- Statistic: warm-cache median unless noted otherwise.
- Required headline fields: cold ready, first command, total first command, and
  warm exec.
- Fresh boot, snapshot restore, and warm-pool checkout must be reported
  separately.

## Summary Timeline

| Date | PR / change | Backend | Transport | Before total ready | After total ready | Delta | Improvement | Notes |
|---|---|---|---|---:|---:|---:|---:|---|
| 2026-06-14 | #367 startup phase telemetry and early guest-agent path | QEMU | vsock | 1551.4 ms | 1073.9 ms | -477.5 ms | 30.8% faster | Published Ubuntu, Rust guest-agent, warm-cache medians. |
| 2026-06-14 | #367 startup phase telemetry and early guest-agent path | Firecracker | vsock | 2598.8 ms | 1195.3 ms | -1403.5 ms | 54.0% faster | Published Ubuntu, Rust guest-agent, warm-cache medians. |
| 2026-06-14 | #371 Firecracker explicit-vsock lazy network setup | Firecracker | vsock | 1195.3 ms | 1098.3 ms | -97.0 ms | 8.1% faster | Current-init local run, 3 measured iterations; published artifact was still `images-2026.06.12.0`. |

## Current Best Published Ubuntu Medians

| Backend | Transport | Total ready | First command | Warm exec | Source |
|---|---:|---:|---:|---:|---|
| QEMU | SSH | 1455.6 ms | 9.3 ms | 42.9 ms | #371 current-init run |
| QEMU | vsock | 1067.6 ms | 1.0 ms | 0.7 ms | #371 current-init run |
| Firecracker | SSH | 1827.7 ms | 52.7 ms | 42.8 ms | #371 current-init run |
| Firecracker | vsock | 1098.3 ms | 1.1 ms | 1.3 ms | #371 current-init run |

The current best table uses `--rootfs-source current-init` because the latest
published artifact observed locally was still `images-2026.06.12.0`.

## Required Entry Format

Add a short section for each PR that changes startup behavior or benchmark
methodology.

```markdown
## YYYY-MM-DD - PR #NNN: short title

- Commit: `SHA`
- Image tag: `images-YYYY.MM.DD.N`
- Command: `...`
- Host: CPU, kernel, KVM/vsock notes
- Method: warm-up count, measured iterations, median/mean policy
- Behavior changed: one sentence

| Backend | Transport | Before total ready | After total ready | Delta | Improvement |
|---|---:|---:|---:|---:|---:|
| QEMU | vsock | ... | ... | ... | ... |
| Firecracker | vsock | ... | ... | ... | ... |
```

## 2026-06-14 - PR #367: Startup Phase Telemetry And Early Guest-Agent Path

- Commit: `85a7e0a`
- Image tag: current published Ubuntu image used by the local benchmark run.
- Method: warm-cache medians from repeated published Ubuntu benchmark runs.
- Behavior changed: the published Ubuntu path moved the Rust guest-agent earlier
  in boot and benchmark output now separates startup phases more clearly.

| Backend | Transport | Before total ready | After total ready | Delta | Improvement |
|---|---:|---:|---:|---:|---:|
| QEMU | vsock | 1551.4 ms | 1073.9 ms | -477.5 ms | 30.8% faster |
| Firecracker | vsock | 2598.8 ms | 1195.3 ms | -1403.5 ms | 54.0% faster |

Notes:

- These numbers are fresh Ubuntu boot readiness, not snapshot restore.
- QEMU VMM launch is already much lower than the total ready number; the
  remaining time is dominated by guest boot and readiness detection.
- The next benchmark update should fill in first-command and warm-exec columns
  for the current best table using the full phase payload.

## 2026-06-14 - PR #371: Firecracker Explicit-vsock Lazy Network Setup

- Commit: `9923297`
- Image tag: `images-2026.06.12.0`
- Command: `uv run python scripts/benchmarks/ubuntu_transport.py --iterations 3 --warm-exec-runs 5 --rootfs-source current-init --output /tmp/smolvm-ubuntu-transport-final-current-init.json`
- Host: Linux with KVM; Firecracker networking used the unprivileged fallback path.
- Method: one warm-up VM per variant, then three measured warm-cache iterations per variant.
- Behavior changed: explicit Firecracker-vsock creates/configures the TAP for Firecracker, but defers route/NAT/egress setup until a network-backed operation needs it.

| Backend | Transport | Before total ready | After total ready | Delta | Improvement |
|---|---:|---:|---:|---:|---:|
| QEMU | vsock | 1073.9 ms | 1067.6 ms | -6.3 ms | 0.6% faster |
| Firecracker | vsock | 1195.3 ms | 1098.3 ms | -97.0 ms | 8.1% faster |

Full current-init medians:

| Backend | Transport | Host create | VMM start | Ready wait | Total ready | First command | Total first command | Warm exec |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| QEMU | SSH | 69.7 ms | 52.7 ms | 1325.5 ms | 1455.6 ms | 9.3 ms | 1464.8 ms | 42.9 ms |
| QEMU | vsock | 76.6 ms | 52.4 ms | 935.1 ms | 1067.6 ms | 1.0 ms | 1068.5 ms | 0.7 ms |
| Firecracker | SSH | 340.1 ms | 121.4 ms | 1365.6 ms | 1827.7 ms | 52.7 ms | 1880.4 ms | 42.8 ms |
| Firecracker | vsock | 224.6 ms | 121.5 ms | 752.6 ms | 1098.3 ms | 1.1 ms | 1099.4 ms | 1.3 ms |

Published-artifact check:

- Command: `uv run python scripts/benchmarks/ubuntu_transport.py --iterations 2 --warm-exec-runs 3 --rootfs-source published --output /tmp/smolvm-ubuntu-transport-lazy-network.json -v`
- Result: Firecracker-vsock used the deferred route/NAT path, but total ready was `2437.2 ms` because the locally published artifact was still `images-2026.06.12.0`.
- Use the current-init numbers above for this PR's implementation signal until the published image is republished with the current init script.
