# Density Ramp Benchmark

## Overview

`density_ramp.py` is a stress test that measures SmolVM's maximum capacity by progressively creating and booting VMs until system resource exhaustion. It collects detailed metrics on host performance and individual VM boot times to characterize the limits of the Firecracker-based runtime.

## Purpose

This benchmark helps answer key questions about SmolVM:
- **Maximum concurrent VMs**: How many VMs can run simultaneously on a given system?
- **Performance degradation**: How do boot times and system metrics change as density increases?
- **Resource utilization**: What are the per-VM resource costs (CPU, memory)?
- **Failure modes**: What resource limits cause deployments to fail?

## Usage

### Basic Invocation

```bash
python density_ramp.py --tier tiny --max_attempts 500
```

### Arguments

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `--tier` | `{tiny, small, med}` | `tiny` | VM size configuration tier |
| `--max_attempts` | int | `500` | Maximum number of VMs to launch |
| `--sustain_sec` | int | `60` | Idle duration (seconds) to wait between VM boots for stability check |
| `--output` | string | `density.json` | Output file path for results (JSON format) |

### VM Tiers

Each tier defines VM memory and disk size:

| Tier | Memory | Disk |
|------|--------|------|
| `tiny` | 128 MiB | 512 MiB |
| `small` | 512 MiB | 1 GiB |
| `med` | 2048 MiB | 4 GiB |

## How It Works

1. **Boot Loop**: For each iteration (up to `--max_attempts`):
   - Create a new VM with the specified tier configuration
   - Boot it and record boot time
   - Keep it running (append to active VM list)
   - Wait `--sustain_sec` seconds
   - Run a quick liveness check (`sleep 1 && uptime`)
   - Capture host metrics (CPU, memory, Firecracker RSS, KVM VM count)

2. **Failure Handling**: If any VM fails to boot or fails the liveness check, the test stops and records the failure point.

3. **Metrics Collection**: For each successful VM, the benchmark captures:
   - Boot time (seconds)
   - Timestamp
   - Number of VMs running
   - Host CPU utilization (%)
   - Host memory used (GB)
   - Active KVM VM count
   - Total Firecracker RSS memory (MB)

4. **Cleanup**: On exit (success, failure, or interrupt), all VMs are stopped gracefully.

## Output Format

Results are written to a JSON file (default: `density.json`):

```json
[
  {
    "vm_id": 1,
    "boot_time_s": 0.45,
    "timestamp": 1699564800.123,
    "vms_running": 1,
    "host_cpu_pct": 12.5,
    "host_mem_used_gb": 2.3,
    "kvm_vms": 1,
    "firecracker_rss_mb": 156.2
  },
  ...
  {
    "failure_at": 245,
    "error": "Out of memory",
    "timestamp": 1699564900.456,
    "vms_running": 244,
    "host_cpu_pct": 85.0,
    "host_mem_used_gb": 15.8,
    "kvm_vms": 244,
    "firecracker_rss_mb": 15200.0
  }
]
```

## Prerequisites

- Linux system with KVM support
- SmolVM installed: `pip install smolvm psutil`
- Sufficient disk space for the rootfs/kernel images
- Firecracker binary available on the system

## Example Runs

### Tiny VMs (Max Capacity Test)
```bash
python density_ramp.py --tier tiny --max_attempts 1000 --sustain_sec 30 --output tiny_density.json
```
Good for finding absolute maximum concurrent VM count with minimal overhead.

### Small VMs (Realistic Mixed Workload)
```bash
python density_ramp.py --tier small --max_attempts 100 --sustain_sec 60 --output small_density.json
```
Realistic for typical application workloads.

### Medium VMs (Headroom Test)
```bash
python density_ramp.py --tier med --max_attempts 50 --sustain_sec 60 --output med_density.json
```
Tests system behavior under heavier individual VM loads.

## Analysis & Interpretation

After running the benchmark:

1. **Look at the final record** to identify the failure point and surrounding metrics
2. **Bootstrap time trend**: Is boot time degrading as VMs accumulate?
3. **Memory usage**: How does host memory consumption scale with VM count?
4. **Firecracker RSS**: Individual Firecracker process overhead
5. **CPU contention**: Does CPU utilization increase as density rises?

Example analysis (Python):
```python
import json

with open('density.json') as f:
    data = json.load(f)

# Find where it failed
if 'failure_at' in data[-1]:
    print(f"Max VMs: {data[-1]['failure_at']}")
else:
    print(f"Max VMs: {len(data)}")

# Boot time trend
boot_times = [d.get('boot_time_s') for d in data if 'boot_time_s' in d]
print(f"Avg boot: {sum(boot_times) / len(boot_times):.2f}s")
print(f"Max boot: {max(boot_times):.2f}s")
```

## Notes

- This is a "first draft" benchmark and is maintained in "skepticism mode", meaning results should be interpreted carefully and may vary significantly based on host system configuration
- The liveness check (`sleep 1 && uptime`) ensures VMs remain responsive, not just that they booted
- Firecracker RSS includes all Firecracker processes, not per-VM attribution
- The test does not perform VM cleanup between iterations—all VMs remain running until the end
- On macOS, this test will not run (KVM is Linux-only); use a Linux system for density testing

## Troubleshooting

| Issue | Cause | Solution |
|-------|-------|----------|
| "ERROR: pip install smolvm" | SmolVM not installed | `pip install smolvm psutil` |
| Quick failure (VM 1 or 2) | Missing Firecracker binary | Run `smolvm demo list` to trigger setup |
| High failure rate | Insufficient disk space | Free up space or use smaller tier |
| Inconsistent results | System load variation | Run test with minimal background processes |
| KVM device errors | Permissions issue | Check KVM device permissions (`/dev/kvm`) |

## See Also

- [SmolVM Documentation](../README.md)
- [Other Benchmarks](../)
