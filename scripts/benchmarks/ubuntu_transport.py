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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from boot_telemetry import collect_boot_telemetry, summarize_boot_telemetry  # noqa: E402

from smolvm.facade import SmolVM, _build_auto_config  # noqa: E402
from smolvm.images.published import _images_release_tag  # noqa: E402
from smolvm.types import SnapshotType  # noqa: E402
from smolvm.vm import SmolVMManager, resolve_data_dir  # noqa: E402

logger = logging.getLogger("smolvm.bench.ubuntu_transport")

RootfsSource = Literal["published", "current-init"]
Transport = Literal["ssh", "vsock"]
Backend = Literal["qemu", "firecracker"]
SnapshotChoice = Literal["auto", "full", "diff", "disk"]
Variant = tuple[Backend, Transport]
ALL_VARIANTS: tuple[Variant, ...] = (
    ("qemu", "ssh"),
    ("qemu", "vsock"),
    ("firecracker", "ssh"),
    ("firecracker", "vsock"),
)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return round(float(statistics.median(values)), 1)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1.0 - weight) + ordered[upper] * weight, 1)


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "median": None,
            "p90": None,
            "p95": None,
            "mean": None,
            "min": None,
            "max": None,
            "count": 0,
        }
    return {
        "median": _median(values),
        "p90": _percentile(values, 0.90),
        "p95": _percentile(values, 0.95),
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


def _safe_delete_snapshot(snapshot_id: str) -> None:
    with suppress(Exception), SmolVMManager() as sdk:
        sdk.delete_snapshot(snapshot_id)


def _vm_log_path(vm: SmolVM | None) -> Path | None:
    """Return the runtime log path for a benchmark VM."""
    vm_id = getattr(vm, "_vm_id", None)
    if not vm_id:
        return None
    return resolve_data_dir() / f"{vm_id}.log"


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
    log_path: Path | None = None
    try:
        started = time.perf_counter()
        config, ssh_key_path, published_rootfs, patched_rootfs = _config_for_variant(
            backend,
            vm_name=vm_name,
            rootfs_source=rootfs_source,
        )
        vm = SmolVM(config=config, ssh_key_path=ssh_key_path, comm_channel=transport)
        log_path = _vm_log_path(vm)
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
        if log_path is None:
            log_path = _vm_log_path(vm)
        _safe_teardown(vm)
        record["boot_telemetry"] = collect_boot_telemetry(log_path)
    return record


def _summarize_fields(records: list[dict[str, Any]], fields: list[str]) -> dict[str, Any]:
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


def _summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    summary = _summarize_fields(
        records,
        [
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
        ],
    )
    boot_telemetry_stats = summarize_boot_telemetry(records, _stats)
    if boot_telemetry_stats:
        summary["boot_telemetry_stats"] = boot_telemetry_stats
    return summary


def _snapshot_type_for_choice(choice: SnapshotChoice) -> SnapshotType:
    if choice == "auto":
        return SnapshotType.DIFF
    return SnapshotType(choice)


def _variant_key(variant: Variant) -> str:
    backend, transport = variant
    return f"{backend}-{transport}"


def _parse_variants(raw: str) -> tuple[Variant, ...]:
    if raw == "all":
        return ALL_VARIANTS

    by_key = {_variant_key(variant): variant for variant in ALL_VARIANTS}
    selected: list[Variant] = []
    invalid: list[str] = []
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        variant = by_key.get(item)
        if variant is None:
            invalid.append(item)
            continue
        if variant not in selected:
            selected.append(variant)
    if invalid or not selected:
        allowed = ", ".join(["all", *by_key])
        requested = ", ".join(invalid) if invalid else raw
        raise ValueError(
            f"Unknown variant {requested!r}; choose one of: {allowed}. "
            "Run: uv run python scripts/benchmarks/ubuntu_transport.py --variants qemu-vsock"
        )
    return tuple(selected)


def _format_ms(value: Any) -> str:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return f"{value:.1f} ms"
    return "-"


def _summary_metric(summary: dict[str, Any], metric: str, stat: str = "median") -> Any:
    value = summary.get(metric)
    if not isinstance(value, dict):
        return None
    return value.get(stat)


def _phase_summary(summary: dict[str, Any]) -> str:
    phase_stats = summary.get("boot_telemetry_stats", {}).get("guest_init_phases_ms", {})
    if not isinstance(phase_stats, dict):
        return "-"

    ranked: list[tuple[str, float]] = []
    for phase_name, stats in phase_stats.items():
        if not isinstance(stats, dict):
            continue
        value = stats.get("median")
        if isinstance(value, int | float) and not isinstance(value, bool):
            ranked.append((phase_name, float(value)))
    if not ranked:
        return "-"

    ranked.sort(key=lambda item: item[1], reverse=True)
    return ", ".join(f"{name}={value:.1f} ms" for name, value in ranked[:3])


def _format_variant_summary_table(report: dict[str, Any]) -> str:
    rows = [
        "| Backend | Transport | Total ready p50 | Total ready p95 | "
        "First command p50 | Warm exec p50 | Top guest phases |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    variants = report.get("variants", {})
    if not isinstance(variants, dict):
        return "\n".join(rows)

    for key in sorted(variants):
        variant = variants[key]
        if not isinstance(variant, dict):
            continue
        summary = variant.get("summary", {})
        if not isinstance(summary, dict):
            continue
        rows.append(
            "| "
            f"{variant.get('backend', '-')} | "
            f"{variant.get('transport', '-')} | "
            f"{_format_ms(_summary_metric(summary, 'total_fresh_ready_ms'))} | "
            f"{_format_ms(_summary_metric(summary, 'total_fresh_ready_ms', 'p95'))} | "
            f"{_format_ms(_summary_metric(summary, 'first_command_ms'))} | "
            f"{_format_ms(_summary_metric(summary, 'warm_exec_median_ms'))} | "
            f"{_phase_summary(summary)} |"
        )
    return "\n".join(rows)


def _run_snapshot_one(
    backend: Backend,
    transport: Transport,
    iteration: int,
    *,
    rootfs_source: RootfsSource,
    warm_exec_runs: int,
    snapshot_choice: SnapshotChoice,
    warmup: bool = False,
) -> dict[str, Any]:
    vm_name = f"bench-snap-{backend[:2]}-{transport[:2]}-{uuid.uuid4().hex[:8]}"
    snapshot_id = f"{vm_name}-snap"
    snapshot_type = _snapshot_type_for_choice(snapshot_choice)
    record: dict[str, Any] = {
        "iter": iteration,
        "vm_id": vm_name,
        "snapshot_id": snapshot_id,
        "snapshot_type": snapshot_type.value,
        "warmup": warmup,
    }
    source_vm: SmolVM | None = None
    restored_vm: SmolVM | None = None
    source_log_path: Path | None = None
    restored_log_path: Path | None = None
    try:
        started = time.perf_counter()
        config, ssh_key_path, published_rootfs, patched_rootfs = _config_for_variant(
            backend,
            vm_name=vm_name,
            rootfs_source=rootfs_source,
        )
        source_vm = SmolVM(config=config, ssh_key_path=ssh_key_path, comm_channel=transport)
        source_log_path = _vm_log_path(source_vm)
        record["snapshot_source_host_create_ms"] = round(
            (time.perf_counter() - started) * 1000, 1
        )
        record["published_rootfs_path"] = str(published_rootfs)
        record["rootfs_path"] = str(config.rootfs_path)
        record["patched_rootfs_path"] = str(patched_rootfs) if patched_rootfs else None
        record["ssh_host_port"] = (
            source_vm._info.network.ssh_host_port if source_vm._info.network else None
        )
        record["vsock_guest_cid"] = (
            source_vm._info.config.vsock.guest_cid if source_vm._info.config.vsock else None
        )

        started = time.perf_counter()
        source_vm.start()
        record["snapshot_source_vmm_start_ms"] = round(
            (time.perf_counter() - started) * 1000, 1
        )

        started = time.perf_counter()
        source_vm.wait_for_ready(timeout=60.0)
        record["snapshot_source_ready_wait_ms"] = round(
            (time.perf_counter() - started) * 1000, 1
        )
        record["snapshot_source_total_ready_ms"] = round(
            record["snapshot_source_host_create_ms"]
            + record["snapshot_source_vmm_start_ms"]
            + record["snapshot_source_ready_wait_ms"],
            1,
        )
        record["source_control_kind"] = getattr(source_vm._control_channel, "kind", None)

        started = time.perf_counter()
        source_vm.snapshot(snapshot_id=snapshot_id, snapshot_type=snapshot_type)
        record["snapshot_create_ms"] = round((time.perf_counter() - started) * 1000, 1)

        _safe_teardown(source_vm)
        source_vm = None

        started = time.perf_counter()
        restored_vm = SmolVM.from_snapshot(
            snapshot_id,
            backend=backend,
            resume_vm=True,
            ssh_key_path=ssh_key_path,
            comm_channel=transport,
        )
        restored_log_path = _vm_log_path(restored_vm)
        record["snapshot_restore_ms"] = round((time.perf_counter() - started) * 1000, 1)

        started = time.perf_counter()
        restored_vm.wait_for_ready(timeout=60.0)
        record["snapshot_restore_ready_wait_ms"] = round(
            (time.perf_counter() - started) * 1000, 1
        )
        record["snapshot_restore_to_ready_ms"] = round(
            record["snapshot_restore_ms"] + record["snapshot_restore_ready_wait_ms"],
            1,
        )
        record["restore_control_kind"] = getattr(restored_vm._control_channel, "kind", None)

        started = time.perf_counter()
        first = restored_vm.run("true", shell="raw", timeout=10.0)
        record["snapshot_first_command_ms"] = round((time.perf_counter() - started) * 1000, 1)
        record["snapshot_first_command_exit_code"] = first.exit_code
        record["snapshot_restore_to_first_command_ms"] = round(
            record["snapshot_restore_to_ready_ms"] + record["snapshot_first_command_ms"],
            1,
        )

        warm_exec_ms: list[float] = []
        for _ in range(warm_exec_runs):
            started = time.perf_counter()
            result = restored_vm.run("true", shell="raw", timeout=10.0)
            warm_exec_ms.append(round((time.perf_counter() - started) * 1000, 1))
            if result.exit_code != 0:
                record.setdefault("snapshot_warm_exec_errors", []).append(result.exit_code)
        record["snapshot_warm_exec_ms"] = warm_exec_ms
        record["snapshot_warm_exec_median_ms"] = _median(warm_exec_ms)

        with suppress(Exception):
            uptime = restored_vm.run("cat /proc/uptime", shell="raw", timeout=10.0)
            record["snapshot_guest_uptime_after_ready_s"] = round(
                float(uptime.stdout.split()[0]), 3
            )
    finally:
        if source_log_path is None:
            source_log_path = _vm_log_path(source_vm)
        if restored_log_path is None:
            restored_log_path = _vm_log_path(restored_vm)
        _safe_teardown(source_vm)
        _safe_teardown(restored_vm)
        _safe_delete_snapshot(snapshot_id)
        record["snapshot_source_boot_telemetry"] = collect_boot_telemetry(source_log_path)
        record["snapshot_restore_boot_telemetry"] = collect_boot_telemetry(restored_log_path)
    return record


def _summarize_snapshot(records: list[dict[str, Any]]) -> dict[str, Any]:
    summary = _summarize_fields(
        records,
        [
            "snapshot_source_host_create_ms",
            "snapshot_source_vmm_start_ms",
            "snapshot_source_ready_wait_ms",
            "snapshot_source_total_ready_ms",
            "snapshot_create_ms",
            "snapshot_restore_ms",
            "snapshot_restore_ready_wait_ms",
            "snapshot_restore_to_ready_ms",
            "snapshot_first_command_ms",
            "snapshot_restore_to_first_command_ms",
            "snapshot_warm_exec_median_ms",
            "snapshot_guest_uptime_after_ready_s",
        ],
    )
    source_telemetry_stats = _summarize_boot_telemetry_key(
        records,
        "snapshot_source_boot_telemetry",
    )
    if source_telemetry_stats:
        summary["snapshot_source_boot_telemetry_stats"] = source_telemetry_stats
    restore_telemetry_stats = _summarize_boot_telemetry_key(
        records,
        "snapshot_restore_boot_telemetry",
    )
    if restore_telemetry_stats:
        summary["snapshot_restore_boot_telemetry_stats"] = restore_telemetry_stats
    return summary


def _summarize_boot_telemetry_key(
    records: list[dict[str, Any]],
    key: str,
) -> dict[str, Any]:
    return summarize_boot_telemetry(
        [{"boot_telemetry": record.get(key)} for record in records],
        _stats,
    )


def _run_variant_group(
    *,
    iterations: int,
    warm_exec_runs: int,
    rootfs_source: RootfsSource,
    variants: tuple[Variant, ...],
    runner: Any,
    summary: Any,
    logger_prefix: str,
    runner_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    extra = runner_extra or {}
    for backend, transport in variants:
        key = _variant_key((backend, transport))
        logger.info("[%s %s] warm-up", logger_prefix, key)
        warmup_record: dict[str, Any] | None = None
        with suppress(Exception):
            warmup_record = runner(
                backend,
                transport,
                -1,
                rootfs_source=rootfs_source,
                warm_exec_runs=warm_exec_runs,
                warmup=True,
                **extra,
            )

        records: list[dict[str, Any]] = []
        for iteration in range(iterations):
            logger.info("[%s %s] measured %d/%d", logger_prefix, key, iteration + 1, iterations)
            try:
                record = runner(
                    backend,
                    transport,
                    iteration,
                    rootfs_source=rootfs_source,
                    warm_exec_runs=warm_exec_runs,
                    **extra,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s %s] iteration failed: %s", logger_prefix, key, exc)
                record = {"iter": iteration, "error": repr(exc)}
            records.append(record)

        results[key] = {
            "backend": backend,
            "transport": transport,
            "warmup": warmup_record,
            "raw": records,
            "summary": summary(records),
        }
    return results


def run_benchmark(
    *,
    iterations: int,
    warm_exec_runs: int,
    rootfs_source: RootfsSource,
    variants: tuple[Variant, ...] = ALL_VARIANTS,
    include_snapshot: bool = False,
    snapshot_type: SnapshotChoice = "auto",
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
        "selected_variants": [_variant_key(variant) for variant in variants],
        "include_snapshot": include_snapshot,
        "snapshot_type": snapshot_type,
        "variants": {},
    }

    report["variants"] = _run_variant_group(
        iterations=iterations,
        warm_exec_runs=warm_exec_runs,
        rootfs_source=rootfs_source,
        variants=variants,
        runner=_run_one,
        summary=_summarize,
        logger_prefix="fresh",
    )
    if include_snapshot:
        report["snapshot_variants"] = _run_variant_group(
            iterations=iterations,
            warm_exec_runs=warm_exec_runs,
            rootfs_source=rootfs_source,
            variants=variants,
            runner=_run_snapshot_one,
            summary=_summarize_snapshot,
            logger_prefix="snapshot",
            runner_extra={"snapshot_choice": snapshot_type},
        )
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
    parser.add_argument(
        "--variants",
        default="all",
        help=(
            "Comma-separated variants to run: qemu-ssh,qemu-vsock,"
            "firecracker-ssh,firecracker-vsock, or all."
        ),
    )
    parser.add_argument(
        "--include-snapshot",
        action="store_true",
        help="Also measure snapshot restore-to-ready and restore-to-first-command.",
    )
    parser.add_argument(
        "--snapshot-type",
        choices=("auto", "full", "diff", "disk"),
        default="auto",
        help="Snapshot type for --include-snapshot. auto uses diff snapshots.",
    )
    parser.add_argument("--output", type=Path, default=Path("/tmp/smolvm-ubuntu-transport.json"))
    parser.add_argument("--json", action="store_true", help="Print the full JSON report.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.iterations < 1:
        parser.error("--iterations must be >= 1")
    if args.warm_exec_runs < 1:
        parser.error("--warm-exec-runs must be >= 1")
    try:
        selected_variants = _parse_variants(args.variants)
    except ValueError as exc:
        parser.error(str(exc))

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(message)s",
    )
    report = run_benchmark(
        iterations=args.iterations,
        warm_exec_runs=args.warm_exec_runs,
        rootfs_source=args.rootfs_source,
        variants=selected_variants,
        include_snapshot=args.include_snapshot,
        snapshot_type=args.snapshot_type,
    )
    payload = json.dumps(report, indent=2)
    args.output.write_text(payload + "\n")
    if args.json:
        print(payload)
    else:
        print(f"Wrote {args.output}")
        print(_format_variant_summary_table(report))


if __name__ == "__main__":
    main()
