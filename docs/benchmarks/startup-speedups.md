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
| 2026-06-14 | #373 sparse published rootfs cache and raw disk copy | Firecracker | SSH | 2500.2 ms | 1797.9 ms | -702.3 ms | 28.1% faster | Published `images-2026.06.14.0`; avoids copying fully allocated zero regions during Firecracker disk materialization. |
| 2026-06-14 | #373 sparse published rootfs cache and raw disk copy | Firecracker | vsock | 1979.1 ms | 1057.4 ms | -921.7 ms | 46.6% faster | Published `images-2026.06.14.0`; host create dropped from 1123.6 ms to 200.5 ms. |
| 2026-06-14 | #374 Ubuntu transport telemetry and Ed25519 SSH host key | QEMU | SSH | 1455.6 ms | 1233.9 ms | -221.7 ms | 15.2% faster | Current-init local run; replaces `ssh-keygen -A` with one Ed25519 host key. |
| 2026-06-14 | #374 Ubuntu transport telemetry and Ed25519 SSH host key | Firecracker | SSH | 1827.7 ms | 1576.5 ms | -251.2 ms | 13.7% faster | Current-init local run; SSH host-key phase median is now 10.0 ms. |
| 2026-06-17 | Current PR QEMU microvm default | QEMU | vsock | 982.6 ms | 345.1 ms | -637.5 ms | 64.9% faster | Published Ubuntu preset; `qemu_machine="q35"` remains the compatibility escape hatch. |

## Current Published Ubuntu Medians

| Backend | Transport | Total ready | First command | Warm exec | Source |
|---|---:|---:|---:|---:|---|
| QEMU | SSH | 1751.6 ms | 9.9 ms | 43.0 ms | #373 published run; microvm SSH not remeasured in the transport harness |
| QEMU | vsock | 345.1 ms | 1.2 ms | 1.0 ms | Current PR microvm sweep, published Ubuntu |
| Firecracker | SSH | 1797.9 ms | 53.0 ms | 43.0 ms | #373 published run |
| Firecracker | vsock | 1057.4 ms | 1.1 ms | 1.0 ms | #373 published run |

The current best table uses the public `images-2026.06.14.0` release with #373
sparse-cache behavior plus the current PR's QEMU microvm default for supported
Linux direct-kernel guests.

Snapshot restore metrics are now instrumented separately by
`scripts/benchmarks/ubuntu_transport.py --include-snapshot`. Add snapshot
restore rows only after running that lane; do not mix them with the fresh-boot
summary timeline above.

## 2026-06-23 - Rust Migration Follow-up Benchmarks

- Commit: `cbd0d52`
- Image tag: current published Ubuntu image used by the local benchmark run.
- Commands:
  - `uv run python scripts/benchmarks/ubuntu_transport.py --variants qemu-ssh,qemu-vsock --iterations 3 --warm-exec-runs 5 --rootfs-source published --output /tmp/smolvm-post-release-benchmarks/ubuntu-transport-qemu-20260623T175406Z.json -v`
  - `uv run python scripts/benchmarks/ubuntu_transport.py --variants firecracker-ssh,firecracker-vsock --iterations 3 --warm-exec-runs 5 --rootfs-source published --output /tmp/smolvm-post-release-benchmarks/ubuntu-transport-firecracker-20260623T175406Z.json -v`
  - `uv run python scripts/benchmarks/disk_io.py --iterations 3 --json --output /tmp/smolvm-post-release-benchmarks/disk-io-20260623T175406Z.json`
  - `uv run python scripts/benchmarks/networking.py --json --output /tmp/smolvm-post-release-benchmarks/networking-20260623T175406Z.json`
- Host: Linux x86_64, kernel `7.0.0-15-generic`, KVM available.
- Method: one warm-up VM per transport variant, then three measured warm-cache iterations per variant.
- Behavior changed: follow-up measurement after protocol-v2 control paths, native disk helpers, native Firecracker API, and `smolvm-core` `2026.6.23`.

Published Ubuntu medians from this run:

| Backend | Transport | Total ready | First command | Warm exec | Notes |
|---|---:|---:|---:|---:|---|
| QEMU | SSH | 1152.2 ms | 12.0 ms | 42.7 ms | Published Ubuntu, local warm-cache run. |
| QEMU | vsock | 413.1 ms | 1.2 ms | 1.0 ms | 64.1% faster ready than SSH in this run. |
| Firecracker | SSH | 3506.4 ms | 53.9 ms | 42.9 ms | Host used unprivileged sudo-command networking fallback. |
| Firecracker | vsock | 2939.8 ms | 1.1 ms | 0.9 ms | 16.2% faster ready than SSH in this run; not a direct native-TAP result. |

Disk helper findings from this run:

| Operation | Size | Native Rust | Forced-off | Result |
|---|---:|---:|---:|---|
| zstd decompress | 16 MiB | 13.0 ms | 39.3 ms | 66.9% faster |
| zstd decompress | 128 MiB | 95.4 ms | 350.1 ms | 72.8% faster |
| sparse copy | 16 MiB | 12.9 ms | 10.3 ms | 25.2% slower |
| sparse copy | 128 MiB | 95.7 ms | 64.3 ms | 48.8% slower |

Networking note:

- True native TAP mode was skipped because the benchmark process did not have
  root or `CAP_NET_ADMIN`. Forced-off and unprivileged-fallback measured the
  existing sudo-command path, with stage sums of `242.1 ms` and `236.4 ms`.
  These are fallback-path numbers, not native TAP speedup numbers.

## 2026-06-23 - Current PR: Alpha Cleanup And Transfer Validation

This benchmark confirms the disk-image speedup and shows that current
published Ubuntu images must be republished before the new fast file-transfer
path can be measured.

- Commit: current PR branch, based on `0818286`.
- Image tag: current published Ubuntu image used by the local benchmark run.
- Commands:
  - `UV_CACHE_DIR=/tmp/uv-cache uv run python scripts/benchmarks/disk_io.py --iterations 2 --sizes 16M,128M --json --output /tmp/smolvm-alpha-cleanup-disk-io.json`
  - `UV_CACHE_DIR=/tmp/uv-cache uv run python scripts/benchmarks/file_transfer.py --backend qemu --comm-channel vsock --os ubuntu --sizes 1K --skip-directory --boot-timeout 120 --ready-timeout 120 --json --output /tmp/smolvm-alpha-cleanup-file-transfer-current-published.json`
- Host: Linux x86_64, kernel `7.0.0-15-generic`, KVM available.
- Method: two local disk iterations; one QEMU/vsock published-Ubuntu file
  transfer smoke with small payloads.
- Behavior changed: the new Rust guest-agent build drops the old JSON/base64
  file endpoints, and the host no longer falls back to those older endpoints.
  The active published Ubuntu image still lacks protocol-v2 transfer
  capabilities, meaning the newer fast file-transfer protocol is not present
  until the image is republished from this branch.

Disk helper validation:

| Operation | Size | Native path | Forced-off path | Result |
|---|---:|---:|---:|---|
| sparse copy | 16 MiB | 10.5 ms (`cp`) | 10.2 ms (`cp`) | unchanged; `cp` remains first |
| sparse copy | 128 MiB | 64.6 ms (`cp`) | 64.8 ms (`cp`) | unchanged; `cp` remains first |
| zstd decompress | 16 MiB | 13.5 ms | 40.6 ms | 66.8% faster |
| zstd decompress | 128 MiB | 96.4 ms | 376.1 ms | 74.4% faster |

File-transfer validation:

| Backend | Transport | Result | Feature flags |
|---|---|---|---|
| QEMU | vsock | failed as expected after 52.7 ms start and 282.9 ms ready | current published image lacks `GET /capabilities`, `files.stream`, and `files.directory_tar` |

Finding: the current published Ubuntu image can boot over vsock but cannot use
the new fast file-transfer protocol. Rerun this file-transfer benchmark after
the next published-image release before claiming streaming transfer speedups.

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

## 2026-06-17 - Current PR: QEMU Microvm Default

- Commit: current PR branch.
- Image tag: `images-2026.06.14.0`.
- Commands:
  - `uv run python scripts/benchmarks/ubuntu_transport.py --variants qemu-vsock --iterations 5 --warm-exec-runs 5 --rootfs-source published --output /tmp/smolvm-qemu-q35-vsock.json`
  - `SMOLVM_QEMU_MACHINE=microvm uv run python scripts/benchmarks/ubuntu_transport.py --variants qemu-vsock --iterations 5 --warm-exec-runs 5 --rootfs-source published --output /tmp/smolvm-qemu-microvm-vsock.json`
- Host: AMD Ryzen 7 7800X3D, Linux x86_64, kernel `7.0.0-15-generic`,
  KVM and `/dev/vhost-vsock` available.
- Method: one warm-up VM per variant, then five measured Ubuntu iterations for
  the initial experiment and three measured iterations per published QEMU preset
  for the broader sweep.
- Behavior changed: QEMU now uses the `microvm` machine by default for Linux
  x86_64 direct-kernel Linux guests; `qemu_machine="q35"`,
  `--qemu-machine q35`, and `SMOLVM_QEMU_MACHINE=q35` keep the compatibility
  path available.

| Backend | Transport | Before total ready | After total ready | Delta | Improvement | First command | Warm exec |
|---|---|---:|---:|---:|---:|---:|---:|
| QEMU | vsock | 982.6 ms | 345.1 ms | -637.5 ms | 64.9% faster | 1.2 ms | 1.0 ms |

Published QEMU preset sweep, vsock-ready p50:

| Preset | q35 p50 | microvm p50 | Delta | Improvement |
|---|---:|---:|---:|---:|
| ubuntu | 982.6 ms | 345.1 ms | -637.5 ms | 64.9% |
| codex | 1500.1 ms | 607.8 ms | -892.3 ms | 59.5% |
| claude-code | 1520.9 ms | 624.4 ms | -896.5 ms | 58.9% |
| hermes | 1525.8 ms | 587.5 ms | -938.3 ms | 61.5% |
| openclaw | 1490.6 ms | 608.5 ms | -882.1 ms | 59.2% |
| pi | 1510.3 ms | 613.1 ms | -897.2 ms | 59.4% |

## 2026-06-14 - PR #374: Ubuntu Transport Telemetry And Ed25519 Host Key

- Commit: current PR branch.
- Image tag: `images-2026.06.14.0`.
- Commands:
  - `uv run python scripts/benchmarks/ubuntu_transport.py --iterations 1 --warm-exec-runs 1 --rootfs-source published --variants qemu-vsock --output /tmp/smolvm-ubuntu-boot-telemetry-probe.json -v`
  - `uv run python scripts/benchmarks/ubuntu_transport.py --iterations 1 --warm-exec-runs 1 --rootfs-source published --variants qemu-ssh --output /tmp/smolvm-ubuntu-boot-telemetry-qemu-ssh.json -v`
  - `uv run python scripts/benchmarks/ubuntu_transport.py --iterations 3 --warm-exec-runs 1 --rootfs-source current-init --variants all --output /tmp/smolvm-ubuntu-bundle-current-init.json -v`
- Host: Linux x86_64, kernel `7.0.0-15-generic`, KVM available.
- Method: one warm-up VM per variant, then three measured current-init
  iterations per variant for the bundled result.
- Behavior changed: Ubuntu transport raw records now include parsed guest boot
  telemetry from `SMOLVM_TS` runtime-log markers, each variant summary includes
  phase stats under `boot_telemetry_stats`, summary stats include p90/p95, the
  CLI prints a compact Markdown table, and `/init` generates only an Ed25519 SSH
  host key instead of running `ssh-keygen -A`. The plain `smolvm sandbox create` command
  now waits for the resolved control channel by default, so QEMU Ubuntu can use
  the same vsock readiness path measured by this benchmark.

Telemetry smoke results:

| Backend | Transport | Total ready | Notable guest phase |
|---|---|---:|---|
| QEMU | vsock | 1057.4 ms | Guest-agent marker present; VM tears down before later SSH markers. |
| QEMU | SSH | 1452.3 ms | `ssh_hostkey_check_ms=290.0 ms`. |

Full current-init medians after Ed25519 host-key generation:

| Backend | Transport | Host create | VMM start | Ready wait | Total ready | Total p95 | First command | Total first command | Warm exec | SSH host-key phase |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| QEMU | SSH | 67.7 ms | 52.7 ms | 1113.9 ms | 1233.9 ms | 1235.2 ms | 9.9 ms | 1243.8 ms | 43.2 ms | 10.0 ms |
| QEMU | vsock | 74.5 ms | 53.2 ms | 924.7 ms | 1052.1 ms | 1071.5 ms | 1.2 ms | 1053.6 ms | 1.6 ms | 0.0 ms |
| Firecracker | SSH | 305.4 ms | 120.6 ms | 1146.0 ms | 1576.5 ms | 1582.8 ms | 51.8 ms | 1628.3 ms | 42.4 ms | 10.0 ms |
| Firecracker | vsock | 207.4 ms | 122.4 ms | 736.8 ms | 1066.2 ms | 1282.5 ms | 1.1 ms | 1067.3 ms | 0.9 ms | 210.0 ms |

Notes:

- The Firecracker-vsock SSH host-key phase is visible in the runtime log after
  the guest agent is ready, but it is not on the vsock readiness critical path.
- The published image still contains the old init until the next image release;
  use the current-init numbers above as the implementation signal for this PR.

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
- Follow-up: route/NAT deferral was later removed because guests need internet
  immediately after create; vsock still avoids SSH port forwarding.

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

## 2026-06-14 - PR #372: Published Image Release `images-2026.06.14.0`

- Commit: `269c1a5`
- Image tag: `images-2026.06.14.0`
- Command: `uv run python scripts/benchmarks/ubuntu_transport.py --iterations 3 --warm-exec-runs 5 --rootfs-source published --output /tmp/smolvm-benchmarks/ubuntu-transport-published-2026-06-14.json -v`
- Host: Linux x86_64, kernel `7.0.0-15-generic`, KVM available.
- Method: one warm-up VM per variant, then three measured warm-cache iterations per variant.
- Behavior changed: the official published Ubuntu image now contains the current init path and Rust guest-agent startup order from #367/#371.

Published medians before the sparse-cache fix:

| Backend | Transport | Host create | VMM start | Ready wait | Total ready | First command | Total first command | Warm exec |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| QEMU | SSH | 82.6 ms | 54.6 ms | 1527.4 ms | 1663.8 ms | 9.7 ms | 1673.5 ms | 42.1 ms |
| QEMU | vsock | 80.3 ms | 52.7 ms | 921.2 ms | 1055.1 ms | 1.0 ms | 1056.0 ms | 0.9 ms |
| Firecracker | SSH | 1207.0 ms | 118.6 ms | 1150.6 ms | 2500.2 ms | 52.4 ms | 2552.0 ms | 42.4 ms |
| Firecracker | vsock | 1123.6 ms | 121.8 ms | 734.1 ms | 1979.1 ms | 1.2 ms | 1980.4 ms | 1.0 ms |

Finding:

- QEMU matched the current-init expectation, but Firecracker regressed because
  the zstd decompression cache stored `rootfs.ext4` as a fully allocated 4 GiB
  file. On this ext4 host, Firecracker isolated-disk materialization then copied
  gigabytes of zeros before boot.

## 2026-06-14 - PR #373: Sparse Published Rootfs Cache

- Commit: `2ef22c3`
- Image tag: `images-2026.06.14.0`
- Command: `uv run python scripts/benchmarks/ubuntu_transport.py --iterations 3 --warm-exec-runs 5 --rootfs-source published --output /tmp/smolvm-benchmarks/ubuntu-transport-published-sparse-2026-06-14.json -v`
- Host: Linux x86_64, kernel `7.0.0-15-generic`, KVM available.
- Method: one warm-up VM per variant, then three measured warm-cache iterations per variant.
- Behavior changed: published zstd rootfs decompression now preserves sparse zero regions and raw isolated-disk copies pass `cp --sparse=always`.

| Backend | Transport | Before total ready | After total ready | Delta | Improvement |
|---|---:|---:|---:|---:|---:|
| Firecracker | SSH | 2500.2 ms | 1797.9 ms | -702.3 ms | 28.1% faster |
| Firecracker | vsock | 1979.1 ms | 1057.4 ms | -921.7 ms | 46.6% faster |

Full published medians after sparse-cache fix:

| Backend | Transport | Host create | VMM start | Ready wait | Total ready | First command | Total first command | Warm exec |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| QEMU | SSH | 73.7 ms | 52.7 ms | 1623.1 ms | 1751.6 ms | 9.9 ms | 1761.0 ms | 43.0 ms |
| QEMU | vsock | 80.6 ms | 54.6 ms | 924.0 ms | 1059.7 ms | 1.1 ms | 1061.4 ms | 0.8 ms |
| Firecracker | SSH | 310.0 ms | 121.7 ms | 1362.3 ms | 1797.9 ms | 53.0 ms | 1850.7 ms | 43.0 ms |
| Firecracker | vsock | 200.5 ms | 120.9 ms | 736.6 ms | 1057.4 ms | 1.1 ms | 1058.5 ms | 1.0 ms |

Cache size check:

- Before sparse cache refresh: decompressed Ubuntu rootfs used about `4.1G` on disk.
- After sparse cache refresh: decompressed Ubuntu rootfs used about `423M` on disk.

## 2026-06-14 - Snapshot Restore Probe: QEMU Vsock Snapshot

- Commit: current working tree on top of `ad560f1`.
- Image tag: `images-2026.06.14.0`.
- Command: `uv run python scripts/benchmarks/ubuntu_transport.py --iterations 1 --warm-exec-runs 1 --rootfs-source published --variants qemu-vsock --include-snapshot --output /tmp/smolvm-ubuntu-qemu-vsock-snapshot-probe.json -v`
- Host: Linux x86_64, kernel `7.0.0-15-generic`, KVM available.
- Method: one warm-up plus one measured QEMU-vsock source VM, snapshot after guest-agent readiness, restore with `comm_channel="vsock"`, then first `true` command.
- Behavior changed: the Ubuntu transport benchmark can now measure snapshot restore separately from fresh boot and skips live QEMU CIDs that are not present in local state.

Fresh-boot result from the same filtered run:

| Backend | Transport | Host create | VMM start | Ready wait | Total ready | First command | Warm exec |
|---|---|---:|---:|---:|---:|---:|---:|
| QEMU | vsock | 83.4 ms | 54.1 ms | 922.9 ms | 1060.4 ms | 1.0 ms | 0.8 ms |

Snapshot result:

| Backend | Transport | Snapshot request | Effective snapshot | Source fresh ready | Snapshot create | Restore | Restore ready wait | Restore to first command | Warm exec |
|---|---|---|---|---:|---:|---:|---:|---:|---:|
| QEMU | vsock | diff | full fallback | 1105.4 ms | 1294.2 ms | 193.5 ms | 0.6 ms | 195.0 ms | 0.8 ms |

Firecracker note:

- Firecracker-vsock full/diff snapshot restore is not reported yet. Stale vsock
  UDS cleanup is fixed, but the restored guest still panics in
  `restore_fpregs_from_fpstate` on this host before the guest-agent can answer.
