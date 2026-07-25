# QEMU incremental snapshot spike

This spike proves that Celesto can capture small QEMU disk increments and rebuild bootable standalone disks without retaining source-host state. The local and production-image gates pass.

## Test baseline

- SmolVM source: `38c962f8afd89cc0fcbada5d437b3632eeba790b`
- Local host: macOS arm64
- Local QEMU and `qemu-img`: 11.0.0
- Production image preflight: Ubuntu 24.04, QEMU 8.2.2 (`1:8.2.2+ds-0ubuntu1.17`), Linux 6.17 AWS x86_64
- Bitmap granularity selected by QEMU: 64 KiB

The production preflight found five running QEMU processes. No disk workload was run on that busy host. A diskless `-machine none` QMP check confirmed that QEMU 8.2.2 exposes every required bitmap, transaction, backup, job, and block-node command.

## Proven locally

`tests/test_qemu_incremental_spike.py` uses a real QEMU process and proves:

- one transaction adds an enabled persistent bitmap and starts the full backup;
- increment A contains only writes made after the base;
- increment B contains writes made after increment A;
- `qemu-img rebase -u` plus `qemu-img convert` produces a standalone qcow2;
- a clean QEMU restart preserves the persistent bitmap;
- forced cancellation leaves all 16 MiB of dirty writes in the bitmap;
- the next successful increment contains the cancelled job's writes.

`tests/e2e/test_qemu_incremental_snapshot.py` uses a disposable Ubuntu VM and proves:

- base+A boots with marker A and without marker B;
- base+A+B boots with both markers;
- both restores work after the source VM, managed disk, QMP socket, and SmolVM state are deleted;
- both materialized outputs are standalone qcow2 files.

Local artifact allocation from the boot test:

| Artifact | Virtual size | Allocated bytes |
|---|---:|---:|
| Base | 4 GiB | 479,985,664 |
| Increment A | 4 GiB | 917,504 |
| Increment B | 4 GiB | 983,040 |

This idle marker workload demonstrates the artifact shape; the production-image benchmark below measures the byte-reduction target under active writes.

## Compatibility finding

Forced cancellation previously sent `force` to generic `job-cancel`. QEMU 11 rejects that argument. Forced block-backup cancellation now uses `block-job-cancel` with `device` and `force=true`; normal cancellation still uses generic `job-cancel`.

## Commands

```bash
uv run pytest tests/test_qmp.py tests/test_qemu_incremental_spike.py tests/test_snapshot_qemu.py
uv run pytest -m e2e tests/e2e/test_qemu_incremental_snapshot.py
uv run python scripts/qemu_incremental_benchmark.py \
  --output-dir /tmp/qemu-incremental-benchmark \
  --intervals 12 \
  --write-mib 32
```

## Production-image result

The tests ran on an isolated `c5.metal` instance launched from the existing production AMI. No new AMI was created, no production user-data was supplied, and the host agent remained disabled. The instance and its root volume were terminated after the run.

QEMU 8.2.2 with KVM passed:

- the focused QMP, cancellation, and snapshot suite: 51 tests;
- source-state deletion and boot of both materialized leaves;
- 12 simulated five-minute intervals with 32 MiB of active guest writes per interval.

Benchmark result:

| Measurement | Result |
|---|---:|
| Repeated standalone full bytes | 6,188,171,264 |
| Base plus 12 increments | 857,145,344 |
| Byte reduction | **86.15%** |
| Increment capture duration | 0.03–0.13 seconds |
| Full capture duration | 2.65–3.74 seconds |
| Baseline terminal latency, median | 45.12 ms |
| During-capture terminal latency, median | 21.45 ms |

The accelerated 12-interval run validates bytes and immediate latency. The 24-hour live canary in the rollout plan remains the long-duration operational test. Disk-heavy benchmarks must not run on a host serving customer VMs.
