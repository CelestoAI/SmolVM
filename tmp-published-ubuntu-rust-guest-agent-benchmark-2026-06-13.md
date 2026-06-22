# Published Ubuntu Rust Guest Agent Benchmark - 2026-06-13

This compares the latest SmolVM Rust guest agent across QEMU and Firecracker with SSH and vsock control channels, using the official published bare Ubuntu image (`preset=ubuntu`) from SmolVM image releases.

Definition used here:

- **Before cold cache**: `~/.smolvm/images` was deleted immediately before that backend/control run.
- **After warm cache**: immediate repeat for the same backend/control run using the image cache produced by the before run.
- **Image Fetch**: `ensure_published_image("ubuntu", arch, vmm, "ubuntu")`, including download, SHA check, and zstd decompression when cold.
- **Create**: facade/config and isolated rootfs materialization for `SmolVM(config=...)`.
- **Start**: runtime launch from `vm.start()`.
- **Ready**: selected control channel readiness from `vm.wait_for_ready()`.
- **First Exec**: first `vm.run("true", shell="raw")` after readiness.
- **Warm Exec Mean**: mean of five subsequent `true` commands over the same selected control channel.

These numbers are local machine measurements, not CI timings. They use the current SmolVM source tree and the current published image pins.

## Environment

- `generated_at_utc`: `2026-06-13T09:28:42+00:00`
- `git_commit`: `ff255006fae9c0fb261e69a8c06fef186572d9d3`
- `git_branch`: `main`
- `images_release_tag`: `images-2026.06.12.0`
- `published_image`: `ubuntu`
- `guest_os`: `ubuntu`
- `memory_mib`: `1024`
- `python`: `3.14.4`
- `platform`: `Linux-7.0.0-15-generic-x86_64-with-glibc2.43`
- `qemu`: `QEMU emulator version 10.2.1 (Debian 1:10.2.1+ds-1ubuntu3)`
- `firecracker`: `Firecracker v1.14.1`
- `kvm`: `True`
- `vhost_vsock`: `True`
- `cache_dir`: `/home/celesto/.smolvm/images`
- `warm_exec_runs`: `5`

## Summary

| Backend | Control | Phase | Image Fetch | Create | Start | Ready | Total Ready | First Exec | Warm Exec Mean | Cache Before->After | Result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qemu | ssh | before cold cache | 7.217s | 0.015s | 0.053s | 1.927s | 9.213s | 0.010s | 0.042s | 0 -> 4.2G | ok |
| qemu | ssh | after warm cache | 0.055s | 0.012s | 0.053s | 1.527s | 1.647s | 0.010s | 0.042s | 4.2G -> 4.2G | ok |
| qemu | vsock | before cold cache | 5.483s | 0.017s | 0.053s | 1.643s | 7.196s | 0.001s | 0.001s | 0 -> 4.2G | ok |
| qemu | vsock | after warm cache | 0.054s | 0.016s | 0.053s | 1.639s | 1.762s | 0.001s | 0.001s | 4.2G -> 4.2G | ok |
| firecracker | ssh | before cold cache | 6.512s | 1.161s | 0.121s | 1.321s | 9.114s | 0.053s | 0.042s | 0 -> 4.2G | ok |
| firecracker | ssh | after warm cache | 0.074s | 1.163s | 0.122s | 1.443s | 2.802s | 0.054s | 0.043s | 4.2G -> 4.2G | ok |
| firecracker | vsock | before cold cache | 6.217s | 1.182s | 0.124s | 1.841s | 9.365s | 0.001s | 0.001s | 0 -> 4.2G | ok |
| firecracker | vsock | after warm cache | 0.071s | 1.121s | 0.119s | 1.244s | 2.555s | 0.001s | 0.001s | 4.2G -> 4.2G | ok |

## Warm-minus-cold deltas

| Backend | Control | Total Ready Delta | Ready Delta | Warm Exec Delta |
|---|---|---|---|---|
| qemu | ssh | -7.566s | -0.400s | -0.001s |
| qemu | vsock | -5.434s | -0.004s | -0.000s |
| firecracker | ssh | -6.312s | +0.122s | +0.001s |
| firecracker | vsock | -6.811s | -0.597s | +0.000s |

## Guest agent checks

- `qemu/ssh` `before cold cache`: exit=0 `/usr/local/bin/smolvm-guest-agent | 113 /usr/local/bin/smolvm-guest-agent --listen vsock://1024 | PRETTY_NAME="Ubuntu 24.04.4 LTS"`
- `qemu/ssh` `after warm cache`: exit=0 `/usr/local/bin/smolvm-guest-agent | 113 /usr/local/bin/smolvm-guest-agent --listen vsock://1024 | PRETTY_NAME="Ubuntu 24.04.4 LTS"`
- `qemu/vsock` `before cold cache`: exit=0 `/usr/local/bin/smolvm-guest-agent | 113 /usr/local/bin/smolvm-guest-agent --listen vsock://1024 | PRETTY_NAME="Ubuntu 24.04.4 LTS"`
- `qemu/vsock` `after warm cache`: exit=0 `/usr/local/bin/smolvm-guest-agent | 113 /usr/local/bin/smolvm-guest-agent --listen vsock://1024 | PRETTY_NAME="Ubuntu 24.04.4 LTS"`
- `firecracker/ssh` `before cold cache`: exit=0 `/usr/local/bin/smolvm-guest-agent | 97 /usr/local/bin/smolvm-guest-agent --listen vsock://1024 | PRETTY_NAME="Ubuntu 24.04.4 LTS"`
- `firecracker/ssh` `after warm cache`: exit=0 `/usr/local/bin/smolvm-guest-agent | 96 /usr/local/bin/smolvm-guest-agent --listen vsock://1024 | PRETTY_NAME="Ubuntu 24.04.4 LTS"`
- `firecracker/vsock` `before cold cache`: exit=0 `/usr/local/bin/smolvm-guest-agent | 97 /usr/local/bin/smolvm-guest-agent --listen vsock://1024 | PRETTY_NAME="Ubuntu 24.04.4 LTS"`
- `firecracker/vsock` `after warm cache`: exit=0 `/usr/local/bin/smolvm-guest-agent | 97 /usr/local/bin/smolvm-guest-agent --listen vsock://1024 | PRETTY_NAME="Ubuntu 24.04.4 LTS"`

## Raw JSON

```json
{
  "metadata": {
    "cache_dir": "/home/celesto/.smolvm/images",
    "firecracker": "Firecracker v1.14.1",
    "generated_at_utc": "2026-06-13T09:28:42+00:00",
    "git_branch": "main",
    "git_commit": "ff255006fae9c0fb261e69a8c06fef186572d9d3",
    "guest_os": "ubuntu",
    "images_release_tag": "images-2026.06.12.0",
    "kvm": "True",
    "memory_mib": 1024,
    "platform": "Linux-7.0.0-15-generic-x86_64-with-glibc2.43",
    "published_image": "ubuntu",
    "python": "3.14.4",
    "qemu": "QEMU emulator version 10.2.1 (Debian 1:10.2.1+ds-1ubuntu3)",
    "vhost_vsock": "True",
    "warm_exec_runs": 5
  },
  "results": [
    {
      "agent_check": "/usr/local/bin/smolvm-guest-agent | 113 /usr/local/bin/smolvm-guest-agent --listen vsock://1024 | PRETTY_NAME=\"Ubuntu 24.04.4 LTS\"",
      "agent_check_exit_code": 0,
      "backend": "qemu",
      "cache_after": "4.2G",
      "cache_before": "0",
      "cold_cache": true,
      "create_ms": 15.0,
      "elapsed_ms": 9572.3,
      "first_exec_ms": 10.0,
      "first_exit_code": 0,
      "image": "published ubuntu",
      "image_fetch_ms": 7217.3,
      "ok": true,
      "phase": "before cold cache",
      "ready_ms": 1927.1,
      "start_ms": 53.1,
      "total_to_first_exec_ms": 9222.5,
      "total_to_ready_ms": 9212.5,
      "transport": "ssh",
      "vm_id": "bench-ubuntu-qemu-ssh-5d8c5c06",
      "warm_exec_mean_ms": 42.4,
      "warm_exec_median_ms": 42.2,
      "warm_exec_ms": [
        43.6,
        42.0,
        42.2,
        41.8,
        42.2
      ]
    },
    {
      "agent_check": "/usr/local/bin/smolvm-guest-agent | 113 /usr/local/bin/smolvm-guest-agent --listen vsock://1024 | PRETTY_NAME=\"Ubuntu 24.04.4 LTS\"",
      "agent_check_exit_code": 0,
      "backend": "qemu",
      "cache_after": "4.2G",
      "cache_before": "4.2G",
      "cold_cache": false,
      "create_ms": 11.7,
      "elapsed_ms": 2003.9,
      "first_exec_ms": 10.4,
      "first_exit_code": 0,
      "image": "published ubuntu",
      "image_fetch_ms": 55.1,
      "ok": true,
      "phase": "after warm cache",
      "ready_ms": 1527.4,
      "start_ms": 52.7,
      "total_to_first_exec_ms": 1657.3,
      "total_to_ready_ms": 1646.9,
      "transport": "ssh",
      "vm_id": "bench-ubuntu-qemu-ssh-96b5a38d",
      "warm_exec_mean_ms": 41.9,
      "warm_exec_median_ms": 42.0,
      "warm_exec_ms": [
        41.8,
        42.1,
        41.8,
        42.0,
        42.0
      ]
    },
    {
      "agent_check": "/usr/local/bin/smolvm-guest-agent | 113 /usr/local/bin/smolvm-guest-agent --listen vsock://1024 | PRETTY_NAME=\"Ubuntu 24.04.4 LTS\"",
      "agent_check_exit_code": 0,
      "backend": "qemu",
      "cache_after": "4.2G",
      "cache_before": "0",
      "cold_cache": true,
      "create_ms": 16.9,
      "elapsed_ms": 7295.5,
      "first_exec_ms": 1.0,
      "first_exit_code": 0,
      "image": "published ubuntu",
      "image_fetch_ms": 5483.3,
      "ok": true,
      "phase": "before cold cache",
      "ready_ms": 1643.2,
      "start_ms": 52.6,
      "total_to_first_exec_ms": 7197.0,
      "total_to_ready_ms": 7196.0,
      "transport": "vsock",
      "vm_id": "bench-ubuntu-qemu-vsock-fc83fd05",
      "warm_exec_mean_ms": 0.8,
      "warm_exec_median_ms": 0.8,
      "warm_exec_ms": [
        0.9,
        0.8,
        0.8,
        0.7,
        0.7
      ]
    },
    {
      "agent_check": "/usr/local/bin/smolvm-guest-agent | 113 /usr/local/bin/smolvm-guest-agent --listen vsock://1024 | PRETTY_NAME=\"Ubuntu 24.04.4 LTS\"",
      "agent_check_exit_code": 0,
      "backend": "qemu",
      "cache_after": "4.2G",
      "cache_before": "4.2G",
      "cold_cache": false,
      "create_ms": 16.3,
      "elapsed_ms": 1860.5,
      "first_exec_ms": 0.9,
      "first_exit_code": 0,
      "image": "published ubuntu",
      "image_fetch_ms": 54.4,
      "ok": true,
      "phase": "after warm cache",
      "ready_ms": 1639.1,
      "start_ms": 52.7,
      "total_to_first_exec_ms": 1763.4,
      "total_to_ready_ms": 1762.5,
      "transport": "vsock",
      "vm_id": "bench-ubuntu-qemu-vsock-aa956247",
      "warm_exec_mean_ms": 0.7,
      "warm_exec_median_ms": 0.7,
      "warm_exec_ms": [
        0.7,
        0.7,
        0.7,
        0.7,
        0.7
      ]
    },
    {
      "agent_check": "/usr/local/bin/smolvm-guest-agent | 97 /usr/local/bin/smolvm-guest-agent --listen vsock://1024 | PRETTY_NAME=\"Ubuntu 24.04.4 LTS\"",
      "agent_check_exit_code": 0,
      "backend": "firecracker",
      "cache_after": "4.2G",
      "cache_before": "0",
      "cold_cache": true,
      "create_ms": 1160.8,
      "elapsed_ms": 12929.4,
      "first_exec_ms": 53.0,
      "first_exit_code": 0,
      "image": "published ubuntu",
      "image_fetch_ms": 6511.9,
      "ok": true,
      "phase": "before cold cache",
      "ready_ms": 1320.8,
      "start_ms": 120.6,
      "total_to_first_exec_ms": 9167.1,
      "total_to_ready_ms": 9114.1,
      "transport": "ssh",
      "vm_id": "bench-ubuntu-firecracker-ssh-ed5c1097",
      "warm_exec_mean_ms": 42.1,
      "warm_exec_median_ms": 42.0,
      "warm_exec_ms": [
        42.8,
        41.7,
        42.0,
        42.1,
        41.9
      ]
    },
    {
      "agent_check": "/usr/local/bin/smolvm-guest-agent | 96 /usr/local/bin/smolvm-guest-agent --listen vsock://1024 | PRETTY_NAME=\"Ubuntu 24.04.4 LTS\"",
      "agent_check_exit_code": 0,
      "backend": "firecracker",
      "cache_after": "4.2G",
      "cache_before": "4.2G",
      "cold_cache": false,
      "create_ms": 1162.8,
      "elapsed_ms": 6593.8,
      "first_exec_ms": 53.7,
      "first_exit_code": 0,
      "image": "published ubuntu",
      "image_fetch_ms": 74.4,
      "ok": true,
      "phase": "after warm cache",
      "ready_ms": 1443.1,
      "start_ms": 122.1,
      "total_to_first_exec_ms": 2856.1,
      "total_to_ready_ms": 2802.4,
      "transport": "ssh",
      "vm_id": "bench-ubuntu-firecracker-ssh-f4983c3d",
      "warm_exec_mean_ms": 42.9,
      "warm_exec_median_ms": 43.0,
      "warm_exec_ms": [
        42.4,
        42.8,
        43.0,
        43.0,
        43.1
      ]
    },
    {
      "agent_check": "/usr/local/bin/smolvm-guest-agent | 97 /usr/local/bin/smolvm-guest-agent --listen vsock://1024 | PRETTY_NAME=\"Ubuntu 24.04.4 LTS\"",
      "agent_check_exit_code": 0,
      "backend": "firecracker",
      "cache_after": "4.2G",
      "cache_before": "0",
      "cold_cache": true,
      "create_ms": 1182.3,
      "elapsed_ms": 12875.7,
      "first_exec_ms": 1.2,
      "first_exit_code": 0,
      "image": "published ubuntu",
      "image_fetch_ms": 6217.3,
      "ok": true,
      "phase": "before cold cache",
      "ready_ms": 1841.2,
      "start_ms": 124.5,
      "total_to_first_exec_ms": 9366.5,
      "total_to_ready_ms": 9365.3,
      "transport": "vsock",
      "vm_id": "bench-ubuntu-firecracker-vsock-f9319c1a",
      "warm_exec_mean_ms": 0.9,
      "warm_exec_median_ms": 0.9,
      "warm_exec_ms": [
        0.9,
        0.9,
        0.9,
        0.9,
        0.9
      ]
    },
    {
      "agent_check": "/usr/local/bin/smolvm-guest-agent | 97 /usr/local/bin/smolvm-guest-agent --listen vsock://1024 | PRETTY_NAME=\"Ubuntu 24.04.4 LTS\"",
      "agent_check_exit_code": 0,
      "backend": "firecracker",
      "cache_after": "4.2G",
      "cache_before": "4.2G",
      "cold_cache": false,
      "create_ms": 1120.5,
      "elapsed_ms": 6067.7,
      "first_exec_ms": 1.2,
      "first_exit_code": 0,
      "image": "published ubuntu",
      "image_fetch_ms": 70.8,
      "ok": true,
      "phase": "after warm cache",
      "ready_ms": 1244.4,
      "start_ms": 119.0,
      "total_to_first_exec_ms": 2555.9,
      "total_to_ready_ms": 2554.7,
      "transport": "vsock",
      "vm_id": "bench-ubuntu-firecracker-vsock-b8eac3a9",
      "warm_exec_mean_ms": 1.0,
      "warm_exec_median_ms": 1.0,
      "warm_exec_ms": [
        1.0,
        1.0,
        1.0,
        1.0,
        1.0
      ]
    }
  ]
}
```
