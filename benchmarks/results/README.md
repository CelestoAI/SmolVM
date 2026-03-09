# Density Ramp Results

This directory contains JSON output files from `density_ramp.py` benchmark runs. Files are named `density_YYYYMMDD_HHMMSS.json` and timestamped at the start of the run.

## File Structure

Each file is a JSON array of event objects written sequentially during the run. Records appear in chronological order: one `boot_ok` or `boot_fail` per VM launch attempt, followed by a single `sustain_check`, and finally a `teardown` record.

### Event Types

#### `boot_ok` — successful VM boot

```json
{
  "event": "boot_ok",
  "vm_seq": 42,
  "boot_time_s": 2.961,
  "density_at_boot": 42,
  "timestamp": 1773030151.45,
  "vms_running": 42,
  "host_cpu_pct": 0.1,
  "host_mem_used_gb": 4.82,
  "host_disk_used_gb": 2.05,
  "tap_count": 42,
  "firecracker_rss_mb": 1968.5,
  "fc_socket_count": 42,
  "sqlite_db_kb": 120.0
}
```

| Field | Description |
| ----- | ----------- |
| `vm_seq` | Sequential index of this VM (1-based launch order) |
| `boot_time_s` | Seconds from VM creation to SSH readiness — the true end-to-end "ready to serve" latency |
| `density_at_boot` | Number of concurrently running VMs at the moment this VM became ready (equals `vms_running` for sequential runs) |
| `timestamp` | Unix epoch at the moment boot was confirmed |
| `vms_running` | Total VMs alive on the host when metrics were sampled |
| `host_cpu_pct` | Host CPU utilization (%) at time of sampling |
| `host_mem_used_gb` | Host physical memory in use (GB) — includes OS, SmolVM process, and all Firecracker processes |
| `host_disk_used_gb` | Disk space consumed under the SmolVM data directory (GB) — grows with each per-VM rootfs clone in isolated mode |
| `tap_count` | Number of active tap network interfaces — should equal `vms_running` |
| `firecracker_rss_mb` | Aggregate RSS (resident set size) of all Firecracker processes (MB) |
| `fc_socket_count` | Number of Firecracker API sockets present — should equal `vms_running` |
| `sqlite_db_kb` | Size of the SmolVM SQLite state database (KB) — grows as VM records accumulate |

#### `boot_fail` — VM failed to boot or reach SSH

```json
{
  "event": "boot_fail",
  "vm_seq": 254,
  "error": "SSH did not become ready within 30s",
  "timestamp": 1773031000.0,
  "vms_running": 253,
  "host_cpu_pct": 95.0,
  "host_mem_used_gb": 191.5,
  ...
}
```

Same host-metrics fields as `boot_ok` plus `error` (string describing the failure). The ramp stops after the first failure.

#### `sustain_check` — liveness check after peak density

```json
{
  "event": "sustain_check",
  "sustain_sec": 60,
  "vms_alive": 253,
  "vms_dead": [],
  "vms_checked": 253,
  "timestamp": 1773030998.82,
  "vms_running": 253,
  "host_cpu_pct": 0.2,
  "host_mem_used_gb": 14.54,
  "host_disk_used_gb": 2.07,
  "tap_count": 253,
  "firecracker_rss_mb": 11949.96,
  "fc_socket_count": 253,
  "sqlite_db_kb": 308.0
}
```

| Field | Description |
| ----- | ----------- |
| `sustain_sec` | Seconds the benchmark idled at peak density before running this check |
| `vms_alive` | VMs that responded to the liveness command (`uptime`) |
| `vms_dead` | List of VM identifiers that failed the liveness check |
| `vms_checked` | Total VMs checked |

Host metrics fields are the same as `boot_ok` and reflect system state at the time of the check — useful for spotting memory growth or CPU drift after a long soak.

#### `teardown` — post-cleanup snapshot

```json
{
  "event": "teardown",
  "teardown_time_s": 127.57,
  "timestamp": 1773031126.57,
  "vms_running": 0,
  "host_cpu_pct": 0.0,
  "host_mem_used_gb": 3.08,
  "host_disk_used_gb": 2.07,
  "tap_count": 253,
  "firecracker_rss_mb": 0.0,
  "fc_socket_count": 0,
  "sqlite_db_kb": 308.0
}
```

| Field | Description |
| ----- | ----------- |
| `teardown_time_s` | Seconds to stop all VMs gracefully |
| `host_disk_used_gb` | Disk after cleanup — delta vs. peak verifies per-VM rootfs clones were released |
| `tap_count` | Note: tap interfaces may linger after teardown until the OS reclaims them |
| `firecracker_rss_mb` | Should be 0.0 — confirms all Firecracker processes exited |

---

## How to Interpret Results

### Did the benchmark succeed?

A successful run reaches `--max-attempts` VMs without any `boot_fail`, and the `sustain_check` shows `vms_alive == vms_checked`. A failed run ends with a `boot_fail` record; the `error` field and surrounding host metrics indicate the limiting resource.

### What was the peak capacity?

Count `boot_ok` records, or read `vms_running` from the `sustain_check`.

### What was the per-VM memory cost?

Compare `host_mem_used_gb` between early and late `boot_ok` records:

```python
per_vm_mem_mb = (boots[-1]["host_mem_used_gb"] - boots[0]["host_mem_used_gb"]) \
                / (len(boots) - 1) * 1024
```

For tiny VMs (128 MiB configured), expect roughly 48–55 MB actual RSS — Firecracker's overhead is well below the guest's configured memory.

### What was the Firecracker process overhead per VM?

```python
rss_per_vm_mb = boots[-1]["firecracker_rss_mb"] / len(boots)
```

This is the average RSS of a single Firecracker process. It should be roughly constant across density levels on a healthy run.

### Was boot time stable under load?

Plot `boot_time_s` against `vm_seq`. A flat line means boot latency doesn't degrade with density. An upward trend indicates CPU or I/O contention affecting startup.

### What did teardown cost?

`teardown_time_s` divided by `vms_checked` gives average stop-time per VM. Check that `host_disk_used_gb` in the teardown record is close to the value in the first `boot_ok` — a large residual indicates rootfs clones were not fully cleaned up.

---

## Quick Analysis Script

```python
import json

with open("density_20260309_001743.json") as f:
    data = json.load(f)

boots   = [d for d in data if d["event"] == "boot_ok"]
fails   = [d for d in data if d["event"] == "boot_fail"]
sustain = next((d for d in data if d["event"] == "sustain_check"), None)
teardown = next((d for d in data if d["event"] == "teardown"), None)

times = [d["boot_time_s"] for d in boots]
print(f"Peak VMs        : {len(boots)}")
print(f"Failed boots    : {len(fails)}")
print(f"Avg boot time   : {sum(times)/len(times):.2f}s")
print(f"Max boot time   : {max(times):.2f}s")
print(f"Peak mem used   : {boots[-1]['host_mem_used_gb']:.2f} GB")
print(f"RSS per VM      : {boots[-1]['firecracker_rss_mb'] / len(boots):.1f} MB")
if sustain:
    print(f"Sustain result  : {sustain['vms_alive']}/{sustain['vms_checked']} alive")
if teardown:
    print(f"Teardown time   : {teardown['teardown_time_s']:.1f}s")
    print(f"Disk after      : {teardown['host_disk_used_gb']:.3f} GB")
```

---

## File Naming

Files generated by `run_density_ramp_ec2.sh` are named `density_YYYYMMDD_HHMMSS.json` where the timestamp is the local time on the machine that launched the EC2 script (not the EC2 instance). Files generated by running `density_ramp.py` directly use whatever `--output` argument was passed (default: `density.json`).
