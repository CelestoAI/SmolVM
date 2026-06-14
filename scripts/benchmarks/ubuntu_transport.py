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

"""Benchmark published Ubuntu across QEMU/Firecracker and SSH/vsock.

The default mode measures the currently published Ubuntu artifacts. Use
``--rootfs-source current-init`` before an image release to copy the published
Ubuntu rootfs and replace ``/init`` with this checkout's
``scripts/ci/preset-init.sh``. That keeps pre-publish startup measurements from
silently using stale remote image bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import platform
import shutil
import statistics
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from smolvm.facade import SmolVM, _build_auto_config  # noqa: E402
from smolvm.images.published import _images_release_tag  # noqa: E402

logger = logging.getLogger("smolvm.bench.ubuntu_transport")

RootfsSource = Literal["published", "current-init"]
Transport = Literal["ssh", "vsock"]
Backend = Literal["qemu", "firecracker"]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return round(float(statistics.median(values)), 1)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"median": None, "mean": None, "min": None, "max": None, "count": 0}
    return {
        "median": _median(values),
        "mean": _mean(values),
        "min": round(min(values), 1),
        "max": round(max(values), 1),
        "count": len(values),
    }


def _safe_teardown(vm: SmolVM | None) -> None:
    if vm is None:
        return
    with suppress(Exception):
        vm.stop(timeout=15.0)
    with suppress(Exception):
        vm.delete()


def _current_init_path() -> Path:
    return _REPO_ROOT / "scripts" / "ci" / "preset-init.sh"


def _current_init_fingerprint(rootfs_path: Path) -> str:
    init_path = _current_init_path()
    hasher = hashlib.sha256()
    hasher.update(init_path.read_bytes())
    hasher.update(str(rootfs_path.resolve()).encode())
    stat = rootfs_path.stat()
    hasher.update(str(stat.st_size).encode())
    hasher.update(str(int(stat.st_mtime_ns)).encode())
    return hasher.hexdigest()[:16]


def rootfs_with_current_init(rootfs_path: Path, *, cache_dir: Path | None = None) -> Path:
    """Return a copy of *rootfs_path* with this checkout's preset init baked in."""
    if shutil.which("debugfs") is None:
        raise RuntimeError(
            "debugfs is required for --rootfs-source current-init; "
            "on Debian/Ubuntu install with: sudo apt install e2fsprogs"
        )

    rootfs_path = rootfs_path.resolve()
    fingerprint = _current_init_fingerprint(rootfs_path)
    cache_root = cache_dir or (Path.home() / ".smolvm" / "benchmarks" / "current-init")
    cache_root.mkdir(parents=True, exist_ok=True)

    target = cache_root / f"{rootfs_path.parent.name}-{fingerprint}.ext4"
    sidecar = target.with_suffix(target.suffix + ".fingerprint")
    if target.is_file() and sidecar.is_file() and sidecar.read_text().strip() == fingerprint:
        return target

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    subprocess.run(
        ["cp", "--reflink=auto", "--sparse=always", str(rootfs_path), str(tmp)],
        check=True,
    )

    init_copy = cache_root / f"preset-init-{fingerprint}.sh"
    init_copy.write_bytes(_current_init_path().read_bytes())
    init_copy.chmod(0o755)

    subprocess.run(["debugfs", "-w", "-R", "rm /init", str(tmp)], check=False)
    subprocess.run(["debugfs", "-w", "-R", f"write {init_copy} /init", str(tmp)], check=True)
    subprocess.run(["debugfs", "-w", "-R", "sif /init mode 0100755", str(tmp)], check=True)

    tmp.replace(target)
    sidecar.write_text(fingerprint + "\n")
    return target


def _config_for_variant(
    backend: Backend,
    *,
    vm_name: str,
    rootfs_source: RootfsSource,
) -> tuple[Any, str | None, Path, Path | None]:
    config, ssh_key_path = _build_auto_config(os="ubuntu", backend=backend, vm_name=vm_name)
    published_rootfs = Path(config.rootfs_path)
    patched_rootfs = None
    if rootfs_source == "current-init":
        patched_rootfs = rootfs_with_current_init(published_rootfs)
        config = config.model_copy(update={"rootfs_path": patched_rootfs})
    return config, ssh_key_path, published_rootfs, patched_rootfs


def _run_one(
    backend: Backend,
    transport: Transport,
    iteration: int,
    *,
    rootfs_source: RootfsSource,
    warm_exec_runs: int,
    warmup: bool = False,
) -> dict[str, Any]:
    vm_name = f"bench-{backend[:2]}-{transport[:2]}-{uuid.uuid4().hex[:8]}"
    record: dict[str, Any] = {"iter": iteration, "vm_id": vm_name, "warmup": warmup}
    vm: SmolVM | None = None
    try:
        started = time.perf_counter()
        config, ssh_key_path, published_rootfs, patched_rootfs = _config_for_variant(
            backend,
            vm_name=vm_name,
            rootfs_source=rootfs_source,
        )
        vm = SmolVM(config=config, ssh_key_path=ssh_key_path, comm_channel=transport)
        record["host_create_ms"] = round((time.perf_counter() - started) * 1000, 1)
        record["create_ms"] = record["host_create_ms"]
        record["published_rootfs_path"] = str(published_rootfs)
        record["rootfs_path"] = str(config.rootfs_path)
        record["patched_rootfs_path"] = str(patched_rootfs) if patched_rootfs else None
        record["ssh_host_port"] = vm._info.network.ssh_host_port if vm._info.network else None
        record["vsock_guest_cid"] = (
            vm._info.config.vsock.guest_cid if vm._info.config.vsock else None
        )

        started = time.perf_counter()
        vm.start()
        record["vmm_start_ms"] = round((time.perf_counter() - started) * 1000, 1)
        record["start_ms"] = record["vmm_start_ms"]

        started = time.perf_counter()
        vm.wait_for_ready(timeout=60.0)
        record["guest_ready_wait_ms"] = round((time.perf_counter() - started) * 1000, 1)
        record["ready_wait_ms"] = record["guest_ready_wait_ms"]
        record["control_kind"] = getattr(vm._control_channel, "kind", None)

        started = time.perf_counter()
        first = vm.run("true", shell="raw", timeout=10.0)
        record["first_command_ms"] = round((time.perf_counter() - started) * 1000, 1)
        record["first_command_exit_code"] = first.exit_code

        warm_exec_ms: list[float] = []
        for _ in range(warm_exec_runs):
            started = time.perf_counter()
            result = vm.run("true", shell="raw", timeout=10.0)
            warm_exec_ms.append(round((time.perf_counter() - started) * 1000, 1))
            if result.exit_code != 0:
                record.setdefault("warm_exec_errors", []).append(result.exit_code)
        record["warm_exec_ms"] = warm_exec_ms
        record["warm_exec_median_ms"] = _median(warm_exec_ms)

        with suppress(Exception):
            uptime = vm.run("cat /proc/uptime", shell="raw", timeout=10.0)
            record["guest_uptime_after_ready_s"] = round(float(uptime.stdout.split()[0]), 3)

        record["total_fresh_ready_ms"] = round(
            record["host_create_ms"] + record["vmm_start_ms"] + record["guest_ready_wait_ms"],
            1,
        )
        record["total_ready_ms"] = record["total_fresh_ready_ms"]
        record["total_first_command_ms"] = round(
            record["total_fresh_ready_ms"] + record["first_command_ms"],
            1,
        )
    finally:
        _safe_teardown(vm)
    return record


def _summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    fields = [
        "host_create_ms",
        "vmm_start_ms",
        "guest_ready_wait_ms",
        "total_fresh_ready_ms",
        "first_command_ms",
        "total_first_command_ms",
        "warm_exec_median_ms",
        "guest_uptime_after_ready_s",
        "create_ms",
        "start_ms",
        "ready_wait_ms",
        "total_ready_ms",
    ]
    return {
        field: _stats(
            [
                float(record[field])
                for record in records
                if isinstance(record.get(field), int | float)
            ]
        )
        for field in fields
    }


def run_benchmark(
    *,
    iterations: int,
    warm_exec_runs: int,
    rootfs_source: RootfsSource,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "date": datetime.now(timezone.utc).isoformat(),
        "host": {
            "system": platform.system(),
            "machine": platform.machine(),
            "release": platform.release(),
        },
        "images_release_tag": _images_release_tag(),
        "rootfs_source": rootfs_source,
        "iterations": iterations,
        "warm_exec_runs": warm_exec_runs,
        "variants": {},
    }

    variants: tuple[tuple[Backend, Transport], ...] = (
        ("qemu", "ssh"),
        ("qemu", "vsock"),
        ("firecracker", "ssh"),
        ("firecracker", "vsock"),
    )
    for backend, transport in variants:
        key = f"{backend}-{transport}"
        logger.info("[%s] warm-up", key)
        warmup_record: dict[str, Any] | None = None
        with suppress(Exception):
            warmup_record = _run_one(
                backend,
                transport,
                -1,
                rootfs_source=rootfs_source,
                warm_exec_runs=warm_exec_runs,
                warmup=True,
            )

        records: list[dict[str, Any]] = []
        for iteration in range(iterations):
            logger.info("[%s] measured %d/%d", key, iteration + 1, iterations)
            try:
                record = _run_one(
                    backend,
                    transport,
                    iteration,
                    rootfs_source=rootfs_source,
                    warm_exec_runs=warm_exec_runs,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] iteration failed: %s", key, exc)
                record = {"iter": iteration, "error": repr(exc)}
            records.append(record)

        report["variants"][key] = {
            "backend": backend,
            "transport": transport,
            "warmup": warmup_record,
            "raw": records,
            "summary": _summarize(records),
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--warm-exec-runs", type=int, default=5)
    parser.add_argument(
        "--rootfs-source",
        choices=("published", "current-init"),
        default="published",
        help="Use published rootfs bytes, or patch /init from this checkout before booting.",
    )
    parser.add_argument("--output", type=Path, default=Path("/tmp/smolvm-ubuntu-transport.json"))
    parser.add_argument("--json", action="store_true", help="Print the full JSON report.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.iterations < 1:
        parser.error("--iterations must be >= 1")
    if args.warm_exec_runs < 1:
        parser.error("--warm-exec-runs must be >= 1")

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(message)s",
    )
    report = run_benchmark(
        iterations=args.iterations,
        warm_exec_runs=args.warm_exec_runs,
        rootfs_source=args.rootfs_source,
    )
    payload = json.dumps(report, indent=2)
    args.output.write_text(payload + "\n")
    if args.json:
        print(payload)
    else:
        print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
