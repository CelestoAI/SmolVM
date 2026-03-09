# SmolVM Density Ramp — Test Harness Report

## Overview

`density_ramp.py` is a stress test that answers one core question: **how many SmolVM
microVMs can a given host run simultaneously before something breaks?** It ramps up VMs
one-by-one (or in parallel batches), captures host resource state at each step, sustains
the peak density for a configurable idle window, then tears down cleanly and verifies
resource release.

---

## What It Tests

### 1. Boot Density Ramp

The test progressively launches microVMs with a fixed configuration tier until one of
three terminal conditions is reached:

- **An entire boot batch fails** — the host can no longer start new VMs.
- **`--max-attempts` is reached** — the configured ceiling (capped at 253, the hard
  limit of SmolVM's IP pool) is hit before failure.
- **The user interrupts** — `finally` ensures cleanup regardless.

Each boot is timed end-to-end: from `SmolVM(config)` construction through `vm.start()`
and `vm.wait_for_ssh(timeout=30.0)`. `boot_time_s` therefore represents the true
"ready to serve" latency — the time until SSH is responsive, not just until the
Firecracker process starts. The sequence number at which each VM was booted
(`density_at_boot`) is recorded alongside the boot time, so a plot of `boot_time_s` vs
`density_at_boot` shows whether boot latency degrades under load.

The three tiers define the per-VM footprint:

| Tier  | Guest RAM | Rootfs Size | Realistic use case              |
|-------|-----------|-------------|----------------------------------|
| tiny  | 128 MiB   | 512 MiB     | Idle sandbox, trivial scripts    |
| small | 512 MiB   | 1 GiB       | Python tooling, pip installs     |
| med   | 2 GiB     | 4 GiB       | Light inference, larger installs |

### 2. Sustain Phase

After the ramp exhausts, the full set of running VMs is held for `--sustain-sec`
(default 60 s). Then `vm.run("uptime", timeout=10)` is dispatched concurrently across
all VMs (up to 64 threads). A VM is alive if and only if `exit_code == 0` is returned
within the timeout. Silently crashed or OOM-killed VMs will surface here.

### 3. Teardown and Resource Release

`cleanup()` calls `vm.stop()` on every running VM. The post-cleanup `Metrics.snapshot()`
is written to the output, so the disk usage delta (pre- vs post-cleanup) can be compared
against the expected per-VM clone size. A mismatch indicates leaked rootfs clones.

---

## Metrics Collected

At every boot event and at the sustain/teardown checkpoints, the following are recorded:

| Field                | Source                                | What it tells you                                                      |
|----------------------|---------------------------------------|------------------------------------------------------------------------|
| `boot_time_s`        | Wall clock around `vm.start()`        | Per-VM boot latency; should be flat; degradation indicates I/O or scheduler saturation |
| `density_at_boot`    | `len(running_vms)` at success         | X-axis for the boot latency curve                                      |
| `host_mem_used_gb`   | `psutil.virtual_memory().used`        | Actual host RAM consumed; slope = overhead per VM                      |
| `host_disk_used_gb`  | `psutil.disk_usage(home).used`        | Tracks rootfs clone growth in isolated mode; should recover after teardown |
| `host_cpu_pct`       | `psutil.cpu_percent(interval=0.1)`    | Transient boot-time CPU; sustained high values indicate host saturation |
| `tap_count`          | `ip -o link show \| grep tap`         | Each Firecracker VM owns one TAP device; should equal `vms_running`    |
| `fc_socket_count`    | `glob("<socket-dir>/fc-*.sock")`      | Live FC API sockets; cross-checks `vms_running`. Dir: `--socket-dir`   |
| `firecracker_rss_mb` | Sum of RSS for all `firecracker` PIDs | Hypervisor overhead per VM independent of guest memory allocation      |
| `sqlite_db_kb`       | `stat(~/.local/state/smolvm/smolvm.db)` | State DB grows with each VM record; large size risks lock contention  |

The output JSON groups events by type: `boot_ok`, `boot_fail`, `sustain_check`,
`teardown`.

---

## Why These Tests Are Relevant

SmolVM's target workload is **on-demand AI agent sandboxes**: an agent task arrives,
a fresh microVM boots, the task executes, the VM is discarded. The density ramp
directly exercises the two properties that determine whether this model is economical
at scale.

### Capacity per dollar

The primary output — peak VM count — directly determines cost efficiency. If a
`c5.2xlarge` ($0.34/hr) sustains 60 simultaneous tiny-tier VMs, and each agent task
takes 2 minutes on average, the instance can process 1,800 tasks/hr at ~$0.0002/task.
Without the density number, any cost estimate is guesswork.

### What the metrics reveal about SmolVM's architecture

**`host_mem_used_gb` slope** measures true per-VM overhead. The naive expectation is
`128 MiB (guest) + ~20 MiB (Firecracker process)` = ~150 MiB per tiny-tier VM, yielding
~50 VMs on 8 GiB. If the measured slope is steeper — say 200 MiB/VM — it indicates
additional overhead from TAP buffers, kernel per-process structures, or smolvm state.
This is actionable: it means the guest memory setting is not the binding constraint and
simply reducing `mem_size_mib` further will not improve density as much as expected.

**`boot_time_s` vs `density_at_boot` curve** reveals I/O and scheduler behaviour.
Firecracker VMs boot by cloning a rootfs image (in isolated mode) and writing it to
disk. If boot time at VM #80 is 3× that at VM #1, the host NVMe or the kernel's
per-process file copy is saturating — even though there is still free RAM. This matters
for burst workloads: a sharp latency cliff means you cannot spin up 50 VMs quickly,
only sequentially.

**`tap_count` and `fc_socket_count`** cross-check `vms_running`. If `fc_socket_count <
vms_running`, Firecracker processes have exited silently — the VM count reported by
SmolVM is stale. This would be a serious reliability bug. Conversely, if `tap_count >
vms_running` after teardown, TAP devices are leaking and will accumulate across test
runs until a reboot or manual cleanup.

**`host_disk_used_gb` delta at teardown** validates that `disk_mode=isolated` cleans up
properly. In isolated mode SmolVM clones the rootfs image per VM to
`~/.local/state/smolvm/disks/{vm_id}.ext4`. The post-teardown disk reading should
return to approximately the pre-test baseline. A persistent increase indicates leaked
clone files, which will silently exhaust disk on long-running hosts.

**`sustain_check` alive ratio** distinguishes between VMs that boot successfully but
later crash (OOM-killed by the host kernel, or KVM slot exhausted) versus VMs that were
never viable. A 100% boot success rate followed by a 70% sustain rate is a qualitatively
different failure mode than a ramp that stops at 70%.

**`sqlite_db_kb`** is a canary for state management overhead. SmolVM tracks all VM
state in a single SQLite file with exclusive transactions. In serial operation this is
fine; under `--parallel` load the DB is accessed from multiple threads simultaneously.
A DB size that grows without bound (i.e., records are not cleaned up on stop/delete)
would eventually degrade all write operations.

---

## How to Administer the Tests

### Prerequisites

- Linux host with KVM enabled: `ls /dev/kvm` must succeed.
- Running as a user with permission to create TAP devices and iptables rules, or as root.
- `pip install smolvm psutil`
- Enough disk for the chosen tier (see the pre-run warning printed by the script).

### Recommended test sequence

**Step 1 — Baseline, serial, shared-disk**

Establishes the memory-bound ceiling without disk being a confounding variable.

```bash
python density_ramp.py \
    --tier tiny \
    --shared-disk \
    --sustain-sec 60 \
    --output results_tiny_shared.json
```

**Step 2 — Isolated mode to measure disk behaviour**

Run the same tier with `--max-attempts` set conservatively (e.g. 50) to avoid filling
the disk, then inspect the teardown delta.

```bash
python density_ramp.py \
    --tier tiny \
    --max-attempts 50 \
    --sustain-sec 60 \
    --output results_tiny_isolated.json
```

**Step 3 — Parallel boot to stress concurrent allocation**

This exercises the TAP device setup, iptables rule insertion, and SQLite state writes
under concurrency. Use a moderate parallelism — start at 4, not 32.

```bash
python density_ramp.py \
    --tier tiny \
    --shared-disk \
    --parallel 4 \
    --sustain-sec 30 \
    --output results_tiny_parallel4.json
```

**Step 4 — Realistic tier**

Repeat steps 1–3 with `--tier small` to model real agent workloads (Python installs,
API calls). The tiny tier is primarily useful for measuring hypervisor overhead in
isolation.

### Reading the output

The JSON output is a flat array of event records. Quick analysis:

```python
import json, statistics

records = json.load(open("results_tiny_shared.json"))
boots   = [r for r in records if r["event"] == "boot_ok"]

print("Peak density:", max(r["density_at_boot"] for r in boots))
print("Boot time p50:", statistics.median(r["boot_time_s"] for r in boots))
print("Boot time p95:", statistics.quantiles([r["boot_time_s"] for r in boots], n=20)[18])

mem_per_vm = (boots[-1]["host_mem_used_gb"] - boots[0]["host_mem_used_gb"]) / len(boots)
print(f"Measured mem overhead per VM: {mem_per_vm*1024:.0f} MiB")

sustain = next(r for r in records if r["event"] == "sustain_check")
print(f"Sustain: {sustain['vms_alive']}/{sustain['vms_checked']} alive")

teardown = next(r for r in records if r["event"] == "teardown")
print(f"Teardown time: {teardown['teardown_time_s']}s")
```

---

## Pitfalls and Things to Watch For

### Disk exhaustion will abort the test before memory is exhausted

In `isolated` mode (the default), SmolVM clones the rootfs image for every VM. A
standard cloud instance root volume of 8 GiB is exhausted by ~16 tiny-tier VMs or ~8
small-tier VMs. The script prints a warning and estimated disk requirement before
starting. Use `--shared-disk` when you want to measure memory-bound density, not
disk-bound density. Use isolated mode when you want to validate that the cleanup path
actually releases clones.

### The 253-VM ceiling is architectural, not a performance limit

SmolVM allocates guest IPs from a fixed `/24` subnet (`172.16.0.2–172.16.0.254`), TAP
devices named `tap2–tap254`, and SSH host ports from `2200–2999`. The script clamps
`--max-attempts` to 253. Hitting this ceiling before running out of RAM is a valid and
informative result — it means the host could support more VMs if the networking layer
were extended, not that the hardware is exhausted.

### Nested KVM on cloud instances inflates all timings

EC2, GCE, and Azure VM instances run Firecracker under a second layer of KVM
virtualisation. This imposes a 20–50% overhead on boot times and CPU-bound operations
compared to bare metal. Results from nested environments are internally consistent and
useful for relative comparisons (tier vs tier, isolated vs shared) but should not be
treated as representative of what SmolVM can do on a bare-metal host. For absolute
density numbers, use a bare-metal instance or a local machine with direct KVM access.

### ARM instances require matching kernel and rootfs images

`t4g` (Graviton) and other ARM instances use `aarch64`. The Alpine SSH image
pre-built by `ImageBuilder` is compiled for the host architecture. If you copy results
files between x86 and ARM hosts, the images are not interchangeable. The script will
fail at image build time with a meaningful error if there is a mismatch, but be aware
that `--tier` timings are not comparable across architectures.

### `--parallel` with high concurrency exposes SQLite contention

SmolVM serialises all VM state writes through a single SQLite database with exclusive
transactions. With `--parallel 1` this is invisible. At `--parallel 8` or higher, boot
threads queue behind the DB lock and measured boot times will include wait time that is
not present in production single-VM usage. Watch `sqlite_db_kb` growing suspiciously
large or boot times clustering around a suspiciously regular interval as signs of
contention. If you see this, the finding is real and worth reporting: it means concurrent
agent spawning is slower than sequential spawning even when RAM is not the constraint.

### Leaked resources from interrupted runs

If the script is killed with `SIGKILL` (e.g. `kill -9`, OOM killer), the `finally`
block does not run. Firecracker processes continue running, TAP devices remain, and
rootfs clone files remain on disk. Before re-running after an interrupted test:

```bash
smolvm cleanup --all          # stops all tracked VMs and removes their state
ip link show | grep tap       # verify no stale TAP devices
ls /tmp/fc-*.sock             # verify no stale Firecracker sockets
```

### `--sustain-sec 0` skips the health check

Setting `--sustain-sec 0` disables the sustain phase entirely. This is useful for
a pure throughput measurement but means you get no signal on whether VMs remain alive
under density pressure. Do not use `--sustain-sec 0` when trying to characterise
reliability — only when characterising raw boot throughput.

### Single-run results have significant variance

Boot times and peak density can vary noticeably between runs due to kernel page cache
state, TAP device setup timing, and SSH handshake jitter. Run each configuration at
least three times and report the median peak and p95 boot time rather than a single
result.
