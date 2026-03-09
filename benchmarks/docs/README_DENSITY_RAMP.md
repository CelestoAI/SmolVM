# Density Ramp Benchmark

## Overview

`density_ramp.py` is a stress test that measures SmolVM's maximum capacity by progressively creating and booting VMs until system resource exhaustion. It collects detailed metrics on host performance and individual VM boot times to characterize the limits of the Firecracker-based runtime.

## Purpose

This benchmark helps answer key questions about SmolVM:

- **Maximum concurrent VMs**: How many VMs can run simultaneously on a given system?
- **Performance degradation**: How do boot times and system metrics change as density increases?
- **Resource utilization**: What are the per-VM resource costs (CPU, memory)?
- **Failure modes**: What resource limits cause deployments to fail?

## Running on EC2 (Recommended)

The easiest way to run this benchmark is with the provided `run_density_ramp_ec2.sh` script, which handles provisioning, setup, execution, and cleanup automatically.

### Prerequisites

- AWS CLI configured (`aws configure` or IAM role)
- An EC2 key pair with the `.pem` file accessible locally
- The key pair name set in the script or via `KEY_NAME` environment variable

### Usage

```bash
cd benchmarks/
./run_density_ramp_ec2.sh [--tier tiny|small|med] [--max-attempts N] \
                           [--sustain-sec N] [--parallel N] [--shared-disk] \
                           [--output FILE]
```

The script will:

1. Launch a `c5d.metal` instance (96 vCPUs, 192 GB RAM, 2x 900 GB NVMe)
2. Install all dependencies (Python 3.11, Docker, nftables)
3. Mount the NVMe drive and redirect all SmolVM data to it
4. Upload and run the benchmark
5. Download results to `./density_YYYYMMDD_HHMMSS.json`
6. Print a quick summary
7. Terminate the instance and clean up the security group

### Environment Variable Overrides

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `KEY_NAME` | `smolvm-benchmark` | EC2 key pair name |
| `KEY_PATH` | `~/.ssh/${KEY_NAME}.pem` | Path to `.pem` file |
| `INSTANCE_TYPE` | `c5d.metal` | EC2 instance type |
| `REGION` | `us-east-2` | AWS region |
| `SECURITY_GROUP` | _(auto-created)_ | Existing security group ID |
| `SUBNET_ID` | _(default VPC)_ | Subnet ID |
| `AMI_ID` | _(latest AL2023)_ | AMI to use |

### Instance Type Notes

The default `c5d.metal` provides:

- 96 vCPUs, 192 GB RAM
- **2x 900 GB NVMe instance store** — the NVMe is required for disk-intensive runs (isolated mode uses up to 126 GB for 253 tiny VMs)

To override to a different instance type: `INSTANCE_TYPE=c5.metal ./run_density_ramp_ec2.sh` — but you will need to add a large EBS volume separately or use `--shared-disk`.

---

## Running Locally

### Basic Invocation

```bash
python density_ramp.py --tier tiny --max-attempts 500
```

### Arguments

| Argument | Type | Default | Description |
| -------- | ---- | ------- | ----------- |
| `--tier` | `{tiny, small, med}` | `tiny` | VM size configuration tier |
| `--max-attempts` | int | `500` | Maximum number of VMs to launch |
| `--sustain-sec` | int | `60` | Idle duration (seconds) after peak density before health-checking all VMs |
| `--parallel` | int | `1` | Number of VMs to boot concurrently per batch |
| `--shared-disk` | flag | off | Use `disk_mode=shared` (no per-VM rootfs clone); saves disk, less isolation |
| `--backend` | string | `auto` | Backend override: `firecracker`, `qemu`, or `auto` |
| `--socket-dir` | path | `/tmp` | Firecracker socket directory |
| `--output` | string | `density.json` | Output file path for results (JSON format) |

### VM Tiers

Each tier defines VM memory and disk size:

| Tier | Memory | Disk | Max disk (isolated, 253 VMs) |
| ---- | ------ | ---- | ---------------------------- |
| `tiny` | 128 MiB | 512 MiB | ~126 GB |
| `small` | 512 MiB | 1 GiB | ~253 GB |
| `med` | 2048 MiB | 4 GiB | ~1 TB |

> **Tip**: Use `--shared-disk` to eliminate per-VM disk clones entirely when disk space is limited.

## How It Works

1. **Boot Loop**: For each iteration (up to `--max-attempts`):
   - Create a new VM with the specified tier configuration
   - Boot it and wait for SSH to become ready (up to 30 s)
   - Record boot time (includes SSH readiness — this is the true "ready to serve" latency)
   - Keep it running with the SSH connection pre-established (append to active VM list)
   - Capture host metrics (CPU, memory, disk, Firecracker RSS, TAP count, socket count)

2. **Sustain Check**: After the ramp completes (or hits `--max-attempts`), wait `--sustain-sec` seconds then run a liveness check (`uptime`) on all surviving VMs. Because SSH connections are pre-established during boot, the sustain check reuses existing paramiko transports rather than opening 64+ new connections concurrently.

3. **Failure Handling**: If an **entire batch** fails to boot, the ramp stops. A single failure within a larger batch does not halt the run.

4. **Cleanup**: On exit (success, failure, or interrupt), all VMs are deleted gracefully.

## Output Format

Results are written to a JSON file (default: `density.json`). Each entry represents an event:

```json
[
  {
    "event": "boot_ok",
    "vm_seq": 1,
    "boot_time_s": 0.45,
    "density_at_boot": 1,
    "timestamp": 1699564800.123,
    "vms_running": 1,
    "host_cpu_pct": 12.5,
    "host_mem_used_gb": 2.3,
    "host_disk_used_gb": 2.1,
    "tap_count": 1,
    "firecracker_rss_mb": 46.9,
    "fc_socket_count": 1,
    "sqlite_db_kb": 52.0
  },
  {
    "event": "boot_fail",
    "vm_seq": 245,
    "error": "Out of memory",
    "timestamp": 1699564900.456
  },
  {
    "event": "sustain_check",
    "sustain_sec": 60,
    "vms_checked": 244,
    "vms_alive": 244,
    "vms_dead": [],
    "timestamp": 1699564960.789
  },
  {
    "event": "teardown",
    "teardown_time_s": 45.2,
    "timestamp": 1699565010.0
  }
]
```

## Prerequisites (Local)

- Linux system with KVM support (`/dev/kvm` accessible)
- Python 3.10+
- SmolVM installed: `pip install smolvm[benchmarks]`
- Docker (for building the Alpine SSH image on first run)
- `nftables` (`nft` command) for VM networking
- Firecracker binary (downloaded automatically via `smolvm.host.HostManager().install_firecracker()`)
- Sufficient disk space (see tier table above; use `--shared-disk` to reduce requirements)

## Example Runs

### Tiny VMs (Max Capacity Test)

```bash
python density_ramp.py --tier tiny --max-attempts 253 --sustain-sec 30 --output tiny_density.json
```

Good for finding absolute maximum concurrent VM count with minimal overhead.

### Tiny VMs, Parallel Boot

```bash
python density_ramp.py --tier tiny --max-attempts 253 --parallel 10 --sustain-sec 30
```

Boot 10 VMs at a time to reduce ramp-up time.

### Small VMs (Realistic Mixed Workload)

```bash
python density_ramp.py --tier small --max-attempts 100 --sustain-sec 60 --output small_density.json
```

Realistic for typical application workloads.

### Medium VMs (Headroom Test)

```bash
python density_ramp.py --tier med --max-attempts 50 --sustain-sec 60 --output med_density.json
```

Tests system behavior under heavier individual VM loads.

### Shared Disk (Disk-Constrained Systems)

```bash
python density_ramp.py --tier tiny --max-attempts 253 --shared-disk
```

All VMs boot from the same rootfs — no per-VM clone needed.

## Analysis & Interpretation

After running the benchmark:

1. **Look at the final record** to identify the failure point and surrounding metrics
2. **Boot time trend**: Is boot time degrading as VMs accumulate?
3. **Memory usage**: How does host memory consumption scale with VM count?
4. **Firecracker RSS**: Individual Firecracker process overhead
5. **CPU contention**: Does CPU utilization increase as density rises?

Example analysis (Python):

```python
import json

with open('density.json') as f:
    data = json.load(f)

boots = [d for d in data if d.get('event') == 'boot_ok']
fails = [d for d in data if d.get('event') == 'boot_fail']
sustain = next((d for d in data if d.get('event') == 'sustain_check'), None)

print(f"Peak VMs       : {len(boots)}")
print(f"Failed boots   : {len(fails)}")
if boots:
    times = [d['boot_time_s'] for d in boots]
    print(f"Avg boot time  : {sum(times)/len(times):.2f}s")
    print(f"Max boot time  : {max(times):.2f}s")
    print(f"Peak mem used  : {boots[-1]['host_mem_used_gb']:.2f} GB")
if sustain:
    print(f"Sustain alive  : {sustain['vms_alive']}/{sustain['vms_checked']} VMs")
```

## Notes

- This is a "first draft" benchmark maintained in "skepticism mode" — results should be interpreted carefully and may vary based on host configuration
- `boot_time_s` includes the time to SSH readiness (not just Firecracker start), making it a true end-to-end "ready to serve" latency
- SSH connections are pre-established during the boot loop; the sustain check reuses these connections via persistent paramiko transports
- The liveness check (`uptime`) ensures VMs remain responsive, not just that they booted
- Firecracker RSS includes all Firecracker processes, not per-VM attribution
- The test does not perform VM cleanup between iterations — all VMs remain running until the end
- On macOS, this test will not run (KVM is Linux-only); use the EC2 script or a Linux system

## Troubleshooting

| Issue | Cause | Solution |
| ----- | ----- | -------- |
| `smolvm` not found | Package not installed | `pip3.11 install smolvm psutil` |
| `nft: command not found` | nftables not installed | `sudo dnf install -y nftables` |
| `Docker is required` | Docker not installed or not running | `sudo dnf install -y docker && sudo systemctl start docker` |
| `Firecracker binary not found` | Firecracker not downloaded | `python3 -c "from smolvm.host import HostManager; HostManager().install_firecracker()"` |
| Quick failure (VM 1) | KVM permissions | `sudo chmod 666 /dev/kvm` and add user to `kvm` group |
| Disk exhaustion warning | Isolated mode fills disk | Use `--shared-disk` or provision more disk (NVMe on `c5d.metal`) |
| All VMs dead in sustain check: `SSH did not become ready` | 64+ concurrent SSH connections opened simultaneously overwhelm paramiko or nftables DNAT under load | Fixed: SSH is now pre-established during the boot loop |
| Inconsistent results | System load variation | Run with minimal background processes |

## See Also

- [SmolVM Documentation](../README.md)
- [EC2 Benchmark Script](../run_density_ramp_ec2.sh)
- [Other Benchmarks](../)
