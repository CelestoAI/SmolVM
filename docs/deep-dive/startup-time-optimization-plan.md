# SmolVM Startup-Time Optimization Plan

This plan tracks how we make the official published Ubuntu sandbox start faster.
The goal is to improve the time from creating a sandbox to running the first
command, while keeping the measurement honest about which phase became faster.

OpenClaw and other presets are intentionally out of scope for this plan. They
may share some runtime improvements, but they should not drive the benchmark or
acceptance criteria here.

## Primary Benchmark Target

- Image: official published Ubuntu preset.
- Backends: QEMU and Firecracker.
- Transports: SSH and vsock.
- Host state: warm image/runtime cache unless a run is explicitly marked cold.
- Summary statistic: median of repeated runs.
- Main user-facing metrics: cold ready, first command, total first command, and
  warm exec.

Track these fields for every optimization PR:

| Field | Meaning |
|---|---|
| `host_create_ms` | Host-side VM object, disk, network, and runtime setup. |
| `vmm_start_ms` | Hypervisor process/API launch time. |
| `guest_ready_wait_ms` | Time spent waiting for SSH or the guest-agent to answer. |
| `total_fresh_ready_ms` | Host create + VMM launch + readiness wait. |
| `cold_ready_ms` | Ready time from a cold image/runtime cache, when explicitly measured. |
| `first_command_ms` | First command latency after the control channel is ready. |
| `total_first_command_ms` | End-to-end time from create to the first command result. |
| `warm_exec_ms` | Median repeated command latency after the sandbox is ready. |
| `guest_uptime_at_first_command_s` | Guest `/proc/uptime` when the first command ran, when available. |

## Current Baseline

The latest tracked improvement moved the published Ubuntu vsock path from the
older warm-cache medians to the current measured medians:

| Backend | Transport | Before total ready | After total ready | Delta | Improvement |
|---|---:|---:|---:|---:|---:|
| QEMU | vsock | 1551.4 ms | 1073.9 ms | -477.5 ms | 30.8% faster |
| Firecracker | vsock | 2598.8 ms | 1195.3 ms | -1403.5 ms | 54.0% faster |

These are fresh Ubuntu boot numbers, not snapshot restore numbers. A 50-120 ms
VMM launch is already plausible today; the remaining latency is mostly Linux
boot, init, and control-channel readiness.

After publishing `images-2026.06.14.0`, phase telemetry exposed a separate
host-side Firecracker issue: the decompressed raw ext4 cache was fully
allocated, so isolated Firecracker starts copied gigabytes of zeros on
non-reflink filesystems. Preserving sparse holes in the published rootfs cache
and raw disk copy path brought Firecracker-vsock published total ready to
`1057.4 ms` on the local benchmark host.

## Optimization Roadmap

### Phase 1: Measurement Hygiene

Keep improving the Ubuntu benchmark output before changing behavior. Every run
should make it obvious whether time was spent in host setup, VMM launch, guest
boot, control-channel probing, first command, or warm execution.

Acceptance:

- Benchmark output includes all fields listed above.
- Reports label `total_fresh_ready_ms` clearly as fresh guest readiness.
- The speed ledger is updated in the same PR when behavior or methodology
  changes.

### Phase 2: Explicit-vsock Fast Path

For explicit vsock sandboxes, the guest-agent should become available before
networking and SSH work that is not required for command execution.

Keep:

- Guest-agent startup before network and SSH setup in the published Ubuntu init
  path.
- SSH setup available after boot for users who explicitly use SSH.
- Auto-mode SSH fallback behavior unchanged.

Next work:

- Keep network and SSH off the explicit-vsock critical path.
- Defer Firecracker route/NAT/SSH forwarding work when no startup feature needs
  SSH or network.
- Keep QEMU slirp SSH host forwarding only where terminal compatibility requires
  it.

Current status:

- Guest-agent startup already happens before network and SSH in the current
  published Ubuntu init script.
- Firecracker explicit-vsock now creates/configures the TAP needed by
  Firecracker, but defers route/NAT/egress setup until SSH or port forwarding
  needs host TCP/IP connectivity.
- Published zstd rootfs decompression preserves sparse zero regions, and raw
  isolated-disk copies preserve those holes. This removes the accidental
  4 GiB copy from the Firecracker critical path on non-reflink filesystems.

Acceptance:

- QEMU vsock and Firecracker vsock command execution still pass.
- SSH variants still reach SSH and accept the injected key.
- Explicit-vsock benchmarks do not wait for SSH readiness unless the requested
  startup feature needs SSH.
- Firecracker host-create time stays near the measured TAP/setup cost instead
  of scaling with the apparent raw rootfs size.

### Phase 3: QEMU Fast Machine Profile

QEMU currently optimizes for broad compatibility. Add a measured fast profile
for Linux direct-kernel vsock sandboxes before considering a default change.

Approach:

- Add an internal QEMU `microvm` experiment for Ubuntu direct-kernel vsock runs.
- Avoid PCI/ACPI/SeaBIOS work where the fast profile supports the required
  devices.
- Fall back to the compatibility profile for SSH, workspace mounts, unsupported
  host setups, or features that need the existing device model.

Acceptance:

- Unit tests prove the fast profile is selected only for eligible Ubuntu/vsock
  runs.
- QEMU vsock exec and file transfer pass.
- Benchmarks report kernel-to-init and agent-ready deltas.

### Phase 4: Kernel And Rootfs Trim

Only trim kernel/rootfs work after phase telemetry shows the expensive stages.
The target is a fast sandbox profile, not a general-purpose Ubuntu kernel.

Candidates to test:

- Boot args such as `raid=noautodetect`, `quiet`, `tsc=reliable`, and
  `no_timer_check`.
- `acpi=off` only inside a measured fast profile.
- Less serial console output for explicit-vsock profiles.
- Remove or modularize unused built-in paths such as sound, wireless regulatory
  loading, RAID autodetect, and unused buses.

Acceptance:

- QEMU vsock and Firecracker vsock e2e tests pass.
- SSH variants remain supported by the compatibility profile.
- The speed ledger records both the measured win and any compatibility tradeoff.

### Phase 5: Snapshot Or Warm Pool Path

Fresh Ubuntu boot is unlikely to be the right path for sub-200 ms command-ready
UX. Snapshot restore or a warm pool should be tracked separately because it
skips kernel boot and most init work.

Approach:

- Create a base snapshot after the guest-agent is listening.
- Restore and immediately repair mutable identity such as hostname, machine-id,
  SSH keys when SSH is enabled, network identity, and agent session state.
- Benchmark restore-to-first-command separately from fresh boot.

Current status:

- The Ubuntu transport benchmark has an opt-in `--include-snapshot` lane that
  reports snapshot restore-to-ready and restore-to-first-command separately from
  fresh boot.
- `SmolVM.from_snapshot(..., comm_channel=...)` can reattach to restored VMs
  with the same SSH or vsock transport selected by the benchmark variant.
- The benchmark defaults snapshot measurement to requesting diff snapshots; QEMU
  may fall back to a full snapshot when the active disk has no backing file.
- QEMU vsock CID allocation skips CIDs already visible in live QEMU process
  arguments, so stale/out-of-band QEMU processes do not break benchmark runs.
- A CLI-validated QEMU-vsock snapshot probe restored to first command in
  `195.0 ms` (`snapshot_restore_ms=193.5`, ready wait `0.6 ms`, first command
  `0.9 ms`).
- QEMU published Ubuntu restores fast enough to show the snapshot path is
  useful, but it is not using the smallest possible snapshot artifact yet. The
  current run asks for a diff snapshot, which should save only changed disk
  blocks, but QEMU falls back to a full disk artifact because the managed
  `qcow2` disk has no backing file. `qcow2` is QEMU's copy-on-write disk
  format, a backing file is the unchanged base image it can refer back to, and
  a raw-backed overlay would let the per-sandbox disk point at the published
  raw rootfs while storing only sandbox changes. A follow-up should evaluate
  raw-backed `qcow2` overlays for the published raw rootfs path.
- Firecracker full/diff snapshot restore needs follow-up before we can report
  warm numbers: stale vsock UDS cleanup is fixed, but the restored guest still
  panics in `restore_fpregs_from_fpstate` on the local benchmark host.

Acceptance:

- Restore-to-first-command e2e passes.
- Isolation tests prove restored sandboxes do not share mutable identity.
- Published reports separate fresh boot, snapshot restore, and warm-pool
  checkout numbers.

## Update Rules

- Update `docs/benchmarks/startup-speedups.md` in every PR that changes startup
  behavior, published Ubuntu image contents, runtime launch behavior, or
  benchmark methodology.
- Record the git SHA, image tag, benchmark command, host notes, before/after
  medians, and what changed.
- Do not compare fresh boot numbers to snapshot or warm-pool numbers in the
  same headline row.
- Keep OpenClaw and other presets out of the primary acceptance criteria until
  they are explicitly added back.

## Validation Rules

- For docs-only changes, run `git diff --check`.
- For benchmark code changes, run the focused pytest coverage for benchmark
  payload shape and timeout handling, then `uv run ruff check` on changed Python
  files.
- For startup behavior changes, rerun published Ubuntu benchmarks across QEMU
  SSH, QEMU vsock, Firecracker SSH, and Firecracker vsock.
- Every benchmark update must record the command, git SHA, image tag, host
  notes, and medians in `docs/benchmarks/startup-speedups.md`.

## Assumptions

- The official published Ubuntu image is the primary optimization target.
- OpenClaw and other presets are out of scope for this plan.
- Fresh boot, snapshot restore, and warm-pool numbers are tracked separately.
- The speed ledger is updated by every PR that changes startup behavior or
  benchmark methodology.
