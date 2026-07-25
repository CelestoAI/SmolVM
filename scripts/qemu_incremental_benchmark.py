#!/usr/bin/env python3
# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Benchmark 12 QEMU incremental intervals against equivalent full backups."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from smolvm import SmolVM
from smolvm.qmp import QMPClient

_BITMAP_NAME = "celesto-phase0-benchmark"

logger = logging.getLogger(__name__)


def _run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True)


def _qemu_img_info(qemu_img: str, path: Path) -> dict[str, Any]:
    result = _run(qemu_img, "info", "--output=json", str(path))
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError(f"qemu-img returned invalid metadata for {path}")
    return value


def _create_target(qemu_img: str, path: Path, virtual_size: int) -> None:
    _run(qemu_img, "create", "-f", "qcow2", str(path), str(virtual_size))


def _terminal_latency_ms(vm: SmolVM) -> float:
    started = time.monotonic()
    result = vm.run("true")
    elapsed = (time.monotonic() - started) * 1000
    if result.exit_code != 0:
        raise RuntimeError("guest terminal latency probe failed")
    return elapsed


def _process_cpu_seconds(pid: int) -> float:
    fields = Path(f"/proc/{pid}/stat").read_text().split()
    ticks = int(fields[13]) + int(fields[14])
    return ticks / os.sysconf("SC_CLK_TCK")


def _root_diskstats() -> dict[str, int]:
    device = os.stat("/").st_dev
    major, minor = os.major(device), os.minor(device)
    for line in Path("/proc/diskstats").read_text().splitlines():
        fields = line.split()
        if int(fields[0]) == major and int(fields[1]) == minor:
            return {
                "reads": int(fields[3]),
                "sectors_read": int(fields[5]),
                "writes": int(fields[7]),
                "sectors_written": int(fields[9]),
                "io_ticks_ms": int(fields[12]),
            }
    raise RuntimeError("root volume is missing from /proc/diskstats")


def _difference(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
    return {key: after[key] - before[key] for key in before}


def _capture(
    client: QMPClient,
    vm: SmolVM,
    qemu_img: str,
    path: Path,
    virtual_size: int,
    *,
    suffix: str,
    mode: str,
) -> dict[str, float | int]:
    _create_target(qemu_img, path, virtual_size)
    node_name = f"target-{suffix}"
    job_id = f"job-{suffix}"
    client.blockdev_add(node_name, path)
    started = time.monotonic()
    if mode == "base":
        client.start_full_backup_with_dirty_bitmap(
            job_id=job_id,
            source_node="rootdisk0",
            target_node=node_name,
            bitmap_name=_BITMAP_NAME,
        )
    elif mode == "incremental":
        client.blockdev_backup_incremental(
            job_id=job_id,
            source_node="rootdisk0",
            target_node=node_name,
            bitmap_name=_BITMAP_NAME,
        )
    elif mode == "full":
        client.blockdev_backup(
            job_id=job_id,
            source_node="rootdisk0",
            target_node=node_name,
        )
    else:
        raise ValueError(f"unsupported capture mode: {mode}")
    terminal_latency_ms = _terminal_latency_ms(vm)
    client.wait_for_job(job_id, timeout=600)
    duration_seconds = time.monotonic() - started
    client.blockdev_del(node_name)
    info = _qemu_img_info(qemu_img, path)
    if info.get("backing-filename"):
        raise RuntimeError(f"capture unexpectedly has a backing file: {path}")
    return {
        "duration_seconds": duration_seconds,
        "terminal_latency_ms": terminal_latency_ms,
        "file_bytes": path.stat().st_size,
        "allocated_bytes": int(info["actual-size"]),
    }


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "minimum": min(values),
        "median": statistics.median(values),
        "maximum": max(values),
    }


def benchmark(output_dir: Path, *, intervals: int, write_mib: int) -> dict[str, Any]:
    qemu_img = shutil.which("qemu-img")
    if qemu_img is None:
        raise RuntimeError("qemu-img is required")

    output_dir.mkdir(parents=True, exist_ok=False)
    artifacts = output_dir / "artifacts"
    artifacts.mkdir()
    source_data = output_dir / "source-data"
    terminal_baseline: list[float] = []
    increments: list[dict[str, float | int]] = []
    full_captures: list[dict[str, float | int]] = []
    dirty_bytes: list[int] = []

    with tempfile.TemporaryDirectory(prefix="qinc-bench-") as socket_raw:
        vm = SmolVM(
            backend="qemu",
            os="ubuntu",
            data_dir=source_data,
            socket_dir=Path(socket_raw),
            disk_size=4096,
            comm_channel="ssh",
        )
        started = time.monotonic()
        try:
            vm.start(boot_timeout=180)
            for _ in range(10):
                terminal_baseline.append(_terminal_latency_ms(vm))

            disk = vm._info.config.rootfs_path
            control_socket = vm._info.control_socket_path
            pid = vm._info.pid
            if control_socket is None or pid is None:
                raise RuntimeError("running QEMU VM did not expose runtime metadata")
            virtual_size = int(
                json.loads(_run(qemu_img, "info", "-U", "--output=json", str(disk)).stdout)[
                    "virtual-size"
                ]
            )
            diskstats_before = _root_diskstats()
            cpu_before = _process_cpu_seconds(pid)

            with QMPClient(control_socket) as client:
                client.connect()
                qemu_version = client.query_version()
                base = _capture(
                    client,
                    vm,
                    qemu_img,
                    artifacts / "base.qcow2",
                    virtual_size,
                    suffix="base",
                    mode="base",
                )
                full_captures.append(base)

                for interval in range(1, intervals + 1):
                    workload = vm.run(
                        "dd if=/dev/urandom of=/root/qemu-incremental-benchmark.bin "
                        f"bs=1M count={write_mib} conv=notrunc status=none && sync",
                        timeout=120,
                    )
                    if workload.exit_code != 0:
                        raise RuntimeError(f"guest workload failed at interval {interval}")
                    bitmap = next(
                        value
                        for value in client.query_dirty_bitmaps("rootdisk0")
                        if value.name == _BITMAP_NAME
                    )
                    dirty_bytes.append(bitmap.dirty_bytes)

                    increment = _capture(
                        client,
                        vm,
                        qemu_img,
                        artifacts / f"increment-{interval:02d}.qcow2",
                        virtual_size,
                        suffix=f"inc-{interval:02d}",
                        mode="incremental",
                    )
                    increments.append(increment)

                    calibration = artifacts / f"full-{interval:02d}.qcow2"
                    full_capture = _capture(
                        client,
                        vm,
                        qemu_img,
                        calibration,
                        virtual_size,
                        suffix=f"full-{interval:02d}",
                        mode="full",
                    )
                    full_captures.append(full_capture)
                    calibration.unlink()

            cpu_seconds = _process_cpu_seconds(pid) - cpu_before
            diskstats = _difference(_root_diskstats(), diskstats_before)
        finally:
            try:
                vm.stop(timeout=10)
            except Exception:
                logger.exception("Failed to stop benchmark VM %s", vm.vm_id)
            try:
                vm.delete()
            except Exception:
                logger.exception("Failed to delete benchmark VM %s", vm.vm_id)

    incremental_bytes = int(full_captures[0]["file_bytes"]) + sum(
        int(value["file_bytes"]) for value in increments
    )
    repeated_full_bytes = sum(int(value["file_bytes"]) for value in full_captures)
    reduction_ratio = 1 - (incremental_bytes / repeated_full_bytes)
    capture_terminal = [
        float(value["terminal_latency_ms"]) for value in [*increments, *full_captures]
    ]
    report: dict[str, Any] = {
        "qemu_version": qemu_version,
        "intervals": intervals,
        "write_mib_per_interval": write_mib,
        "virtual_size_bytes": virtual_size,
        "elapsed_seconds": time.monotonic() - started,
        "incremental_bytes": incremental_bytes,
        "repeated_full_bytes": repeated_full_bytes,
        "reduction_ratio": reduction_ratio,
        "passes_80_percent_gate": reduction_ratio >= 0.8,
        "dirty_bytes": dirty_bytes,
        "base": full_captures[0],
        "increments": increments,
        "full_captures": full_captures,
        "terminal_latency_ms": {
            "baseline": _summary(terminal_baseline),
            "during_capture": _summary(capture_terminal),
        },
        "qemu_cpu_seconds": cpu_seconds,
        "root_volume_io": diskstats,
    }
    (output_dir / "benchmark.json").write_text(json.dumps(report, indent=2, sort_keys=True))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--intervals", type=int, default=12)
    parser.add_argument("--write-mib", type=int, default=32)
    args = parser.parse_args()
    if args.intervals < 1 or args.write_mib < 1:
        parser.error("--intervals and --write-mib must be positive")
    report = benchmark(args.output_dir, intervals=args.intervals, write_mib=args.write_mib)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passes_80_percent_gate"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
