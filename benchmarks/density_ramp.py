#!/usr/bin/env python3
"""
SmolVM Density Ramp Test

Ramps VMs until failure, measuring boot latency and host resource consumption.
Tiers: tiny / small / med. Sustain check runs across ALL VMs concurrently after
peak density is reached, not inline per-VM.

Usage:
    pip install smolvm psutil
    python density_ramp.py --tier tiny --max-attempts 500
    python density_ramp.py --tier small --shared-disk --parallel 4
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psutil

try:
    from smolvm import SSH_BOOT_ARGS, ImageBuilder, SmolVM, VMConfig
    from smolvm.utils import ensure_ssh_key  # internal, stable across versions
except ImportError:
    print("ERROR: pip install smolvm")
    sys.exit(1)

# Hard ceiling from SmolVM's IP pool (172.16.0.2–172.16.0.254)
SMOLVM_MAX_VMS = 253


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@dataclass
class Metrics:
    timestamp: float
    vms_running: int
    host_cpu_pct: float
    host_mem_used_gb: float
    host_disk_used_gb: float
    tap_count: int = 0
    firecracker_rss_mb: float = 0
    fc_socket_count: int = 0  # live Firecracker sockets in socket_dir
    sqlite_db_kb: float = 0

    @classmethod
    def snapshot(cls, vms_running: int, socket_dir: Path) -> "Metrics":
        ts = time.time()
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory().used / (1024**3)
        # Measure the filesystem that actually holds SmolVM disk clones and images.
        # On EC2 this is /mnt/nvme (symlinked from ~/.local/state/smolvm); fall back
        # to the home filesystem when the SmolVM state dir doesn't exist yet.
        _smolvm_state = Path.home() / ".local" / "state" / "smolvm"
        _disk_probe = _smolvm_state if _smolvm_state.exists() else Path.home()
        disk = psutil.disk_usage(str(_disk_probe.resolve())).used / (1024**3)

        # Count live Firecracker API sockets (accurate VM count proxy)
        fc_sockets = len(list(socket_dir.glob("fc-*.sock")))

        # TAP device count
        try:
            ip_out = subprocess.run(
                ["ip", "-o", "link", "show"],
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout
            tap_count = sum(1 for line in ip_out.splitlines() if ": tap" in line)
        except Exception:
            tap_count = 0

        # Firecracker aggregate RSS
        try:
            fc_rss = sum(
                p.memory_info().rss / 1024**2
                for p in psutil.process_iter(["name"])
                if "firecracker" in (p.info["name"] or "").lower()
            )
        except Exception:
            fc_rss = 0.0

        # SmolVM SQLite state DB size
        db_path = Path.home() / ".local" / "state" / "smolvm" / "smolvm.db"
        sqlite_kb = db_path.stat().st_size / 1024 if db_path.exists() else 0.0

        return cls(ts, vms_running, cpu, mem, disk, tap_count, fc_rss, fc_sockets, sqlite_kb)


# ---------------------------------------------------------------------------
# VM lifecycle helpers
# ---------------------------------------------------------------------------

running_vms: list[SmolVM] = []


def cleanup() -> float:
    """Delete all running VMs; return elapsed teardown seconds."""
    start = time.time()
    for vm in running_vms:
        try:
            vm.delete()
        except Exception as exc:
            print(f"WARNING: failed to delete VM {getattr(vm, 'vm_id', '?')}: {exc}")
    return time.time() - start


def build_image(
    tier_config: dict[str, int],
    backend: str | None,
) -> tuple[Path, Path, str]:
    """Pre-build (or load from cache) the Alpine SSH image.

    Returns (kernel_path, rootfs_path, ssh_key_path).
    """
    print("Building/loading image (cached after first run)...")
    priv_key, pub_key = ensure_ssh_key()

    disk_size_mib = tier_config["disk_size_mib"]
    image_name = "alpine-ssh-key"
    if disk_size_mib != 512:
        image_name = f"{image_name}-{disk_size_mib}m"

    builder = ImageBuilder()
    kernel, rootfs = builder.build_alpine_ssh_key(
        pub_key,
        name=image_name,
        rootfs_size_mb=disk_size_mib,
    )
    print(f"Image ready: kernel={kernel.name} rootfs={rootfs.name}")
    return kernel, rootfs, str(priv_key)


def _read_vm_log(vm: SmolVM, tail_lines: int = 60) -> str:
    """Read the Firecracker/QEMU log for a VM (last N lines)."""
    try:
        log_path = vm.data_dir / f"{vm.vm_id}.log"
        if log_path.exists():
            lines = log_path.read_text(errors="replace").splitlines()
            return "\n".join(lines[-tail_lines:]) if lines else "(log file is empty)"
        return f"(log not found: {log_path})"
    except Exception as exc:
        return f"(could not read log: {exc})"


def boot_one(
    seq: int,
    kernel: Path,
    rootfs: Path,
    ssh_key_path: str,
    tier_config: dict[str, int],
    disk_mode: str,
    backend: str | None,
    socket_dir: Path,
) -> tuple[int, SmolVM | None, float, dict[str, Any]]:
    """Boot a single VM. Returns (seq, vm_or_None, boot_seconds, error_dict)."""
    start_t = time.time()
    vm: SmolVM | None = None
    try:
        config = VMConfig(
            vm_id=f"vm-{uuid.uuid4().hex[:8]}",
            vcpu_count=1,
            mem_size_mib=tier_config["mem_size_mib"],
            kernel_path=kernel,
            rootfs_path=rootfs,
            boot_args=SSH_BOOT_ARGS,
            disk_mode=disk_mode,
            backend=backend,
        )
        vm = SmolVM(config, ssh_key_path=ssh_key_path, socket_dir=socket_dir)
        vm.start()
        vm.wait_for_ssh(timeout=30.0)
        return seq, vm, time.time() - start_t, {}
    except Exception as e:
        fc_log = _read_vm_log(vm) if vm is not None else "(VM object not created)"
        err: dict[str, Any] = {"error": str(e), "fc_log": fc_log}
        if vm is not None:
            try:
                vm.delete()
            except Exception as cleanup_exc:
                err["cleanup_error"] = str(cleanup_exc)
        return seq, None, time.time() - start_t, err


def check_vm_alive(vm: SmolVM, seq: int) -> tuple[int, bool, str]:
    """Run a health check on one VM. Returns (seq, alive, error_msg)."""
    try:
        result = vm.run("uptime", timeout=10)
        return seq, result.exit_code == 0, ""
    except Exception as e:
        return seq, False, str(e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SmolVM Density Ramp")
    parser.add_argument("--tier", choices=["tiny", "small", "med"], default="tiny")
    parser.add_argument("--max-attempts", type=int, default=500)
    parser.add_argument(
        "--sustain-sec",
        type=int,
        default=60,
        help="Idle duration (seconds) after peak density before health-checking all VMs",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Number of VMs to boot concurrently per batch",
    )
    parser.add_argument(
        "--shared-disk",
        action="store_true",
        help="Use disk_mode=shared (no per-VM rootfs clone); saves disk, less isolation",
    )
    parser.add_argument(
        "--backend",
        default=None,
        help="Backend override: firecracker, qemu, or auto",
    )
    parser.add_argument(
        "--socket-dir",
        type=Path,
        default=Path("/tmp"),
        help="Firecracker socket directory (must match SmolVM runtime default)",
    )
    parser.add_argument("--output", default="density.json")
    args = parser.parse_args()

    tiers = {
        "tiny": dict(mem_size_mib=128, disk_size_mib=512),
        "small": dict(mem_size_mib=512, disk_size_mib=1024),
        "med": dict(mem_size_mib=2048, disk_size_mib=4096),
    }
    tier_config = tiers[args.tier]
    disk_mode = "shared" if args.shared_disk else "isolated"

    # Guard: SmolVM IP pool hard ceiling
    if args.max_attempts > SMOLVM_MAX_VMS:
        print(
            f"WARNING: SmolVM IP pool caps at {SMOLVM_MAX_VMS} VMs. "
            f"Clamping --max-attempts from {args.max_attempts} to {SMOLVM_MAX_VMS}."
        )
        args.max_attempts = SMOLVM_MAX_VMS

    # Warn about disk usage in isolated mode
    if disk_mode == "isolated":
        est_disk_gb = (args.max_attempts * tier_config["disk_size_mib"]) / 1024
        _smolvm_state = Path.home() / ".local" / "state" / "smolvm"
        _disk_probe = _smolvm_state if _smolvm_state.exists() else Path.home()
        free_gb = psutil.disk_usage(str(_disk_probe.resolve())).free / (1024**3)
        print(
            f"INFO: isolated mode — up to {est_disk_gb:.0f} GB disk needed; "
            f"{free_gb:.1f} GB free on home filesystem."
        )
        if est_disk_gb > free_gb * 0.8:
            print(
                "WARNING: Risk of disk exhaustion before memory. "
                "Consider --shared-disk to eliminate per-VM clones."
            )

    kernel, rootfs, ssh_key_path = build_image(tier_config, args.backend)

    results: list[dict[str, Any]] = []
    _diag_printed = False  # Print Firecracker log only for the first failure

    try:
        i = 0
        while i < args.max_attempts:
            batch_size = min(args.parallel, args.max_attempts - i)

            # Boot one batch (serial or parallel).
            # Each result is a 5-tuple (seq, vm, boot_t, err, snap) where snap
            # is captured immediately when the boot completes — not post-batch —
            # so parallel runs record per-VM resource state accurately.
            # Results are sorted by seq before processing to preserve launch order.
            if batch_size == 1:
                seq_r, vm, boot_t, err = boot_one(
                    i + 1,
                    kernel,
                    rootfs,
                    ssh_key_path,
                    tier_config,
                    disk_mode,
                    args.backend,
                    args.socket_dir,
                )
                density = len(running_vms) + (1 if vm is not None else 0)
                batch_results = [(seq_r, vm, boot_t, err, Metrics.snapshot(density, args.socket_dir))]
            else:
                with ThreadPoolExecutor(max_workers=batch_size) as ex:
                    futures = [
                        ex.submit(
                            boot_one,
                            i + j + 1,
                            kernel,
                            rootfs,
                            ssh_key_path,
                            tier_config,
                            disk_mode,
                            args.backend,
                            args.socket_dir,
                        )
                        for j in range(batch_size)
                    ]
                    batch_results = []
                    batch_ok_so_far = 0
                    for f in as_completed(futures):
                        seq_r, vm, boot_t, err = f.result()
                        if vm is not None:
                            batch_ok_so_far += 1
                        density = len(running_vms) + batch_ok_so_far
                        snap = Metrics.snapshot(density, args.socket_dir)
                        batch_results.append((seq_r, vm, boot_t, err, snap))

            failed_this_batch = 0
            for seq_r, vm, boot_t, err, snap in sorted(batch_results):
                i += 1

                if vm is not None:
                    running_vms.append(vm)
                    print(
                        f"VM {seq_r}: boot={boot_t:.2f}s | "
                        f"mem={snap.host_mem_used_gb:.2f}GB "
                        f"disk={snap.host_disk_used_gb:.2f}GB "
                        f"tap={snap.tap_count} "
                        f"fc_rss={snap.firecracker_rss_mb:.0f}MB"
                    )
                    results.append(
                        {
                            "event": "boot_ok",
                            "vm_seq": seq_r,
                            "boot_time_s": round(boot_t, 3),
                            "density_at_boot": len(running_vms),
                            **asdict(snap),
                        }
                    )
                else:
                    print(f"FAIL @ VM {seq_r}: {err.get('error', '?')}")
                    if not _diag_printed and err.get("fc_log"):
                        _diag_printed = True
                        print("--- Firecracker log (first failure) ---")
                        print(err["fc_log"])
                        print("--- End Firecracker log ---")
                    results.append(
                        {
                            "event": "boot_fail",
                            "vm_seq": seq_r,
                            **err,
                            **asdict(snap),
                        }
                    )
                    failed_this_batch += 1

            if failed_this_batch == batch_size:
                print("Entire batch failed — stopping ramp.")
                break

        peak = len(running_vms)
        print(f"\nPeak density: {peak} VMs (tier={args.tier}, disk_mode={disk_mode})")

        # ------------------------------------------------------------------
        # Sustain phase: idle all VMs together, then health-check concurrently
        # ------------------------------------------------------------------
        if peak > 0 and args.sustain_sec > 0:
            print(f"Sustaining {peak} VMs for {args.sustain_sec}s...")
            time.sleep(args.sustain_sec)

            print(f"Running concurrent health checks across all {peak} VMs...")
            workers = min(peak, 64)
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {
                    ex.submit(check_vm_alive, vm, idx + 1): idx
                    for idx, vm in enumerate(running_vms)
                }
                alive_count = 0
                dead_vms: list[int] = []
                for future in as_completed(futures):
                    seq, alive, err_msg = future.result()
                    if alive:
                        alive_count += 1
                    else:
                        dead_vms.append(seq)
                        if err_msg:
                            print(f"  VM {seq} dead: {err_msg}")

            snap = Metrics.snapshot(peak, args.socket_dir)
            print(
                f"Sustain result: {alive_count}/{peak} VMs alive "
                f"({len(dead_vms)} dead: {dead_vms[:10]}{'...' if len(dead_vms) > 10 else ''})"
            )
            results.append(
                {
                    "event": "sustain_check",
                    "sustain_sec": args.sustain_sec,
                    "vms_alive": alive_count,
                    "vms_dead": dead_vms,
                    "vms_checked": peak,
                    **asdict(snap),
                }
            )

    finally:
        print("Tearing down VMs...")
        teardown_s = cleanup()
        snap = Metrics.snapshot(0, args.socket_dir)
        print(
            f"Teardown: {teardown_s:.1f}s | "
            f"disk after cleanup={snap.host_disk_used_gb:.2f}GB "
            f"(delta verifies rootfs clones released)"
        )
        results.append(
            {
                "event": "teardown",
                "teardown_time_s": round(teardown_s, 2),
                **asdict(snap),
            }
        )

    with open(args.output, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Results written to {args.output}")
