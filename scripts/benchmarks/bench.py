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

"""SmolVM benchmark suite — drives the public SDK and measures real lifecycle timings.

Benchmarks:
    cold-start    Time from SmolVM(...) to a booted, SSH-reachable VM (first time, no warm caches).
    tti           Time-to-interactive on subsequent boots (image cache warm).
    pause-resume  How fast we can freeze and unfreeze a running VM.
    snapshot      How fast we can persist VM state and bring it back.

Backends:
    macOS  -> QEMU      (auto)
    Linux  -> Firecracker (auto)

Usage:
    uv run python scripts/benchmarks/bench.py
    uv run python scripts/benchmarks/bench.py --only cold-start,tti --iterations 3
    uv run python scripts/benchmarks/bench.py --json --output /tmp/bench.json
"""

from __future__ import annotations

import argparse
import json
import logging
import platform as _platform
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow running from a source checkout without installing.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Local sibling import — works whether invoked as a script or a module.
from metrics import Phase, stats  # noqa: E402

logger = logging.getLogger("smolvm.bench")

ALL_BENCHMARKS = ("cold-start", "tti", "pause-resume", "snapshot")


# ── Platform / backend resolution ────────────────────────────────────


def _resolve_and_validate_backend(requested: str) -> str:
    """Resolve the backend and reject mismatches with the host platform."""
    from smolvm.runtime.backends import (
        BACKEND_FIRECRACKER,
        BACKEND_QEMU,
        resolve_backend,
    )

    backend = resolve_backend(requested)
    system = _platform.system().lower()

    if system == "darwin" and backend == BACKEND_FIRECRACKER:
        raise SystemExit("Firecracker is not supported on macOS. Use --backend qemu (or auto).")
    if system == "linux" and backend == BACKEND_QEMU:
        # Not a hard error — QEMU works on Linux — but warn.
        logger.warning(
            "Running QEMU on Linux. Native default would be Firecracker; "
            "pass --backend auto to switch."
        )
    return backend


# ── VM lifecycle helpers ─────────────────────────────────────────────


def _new_vm(backend: str):
    """Construct an auto-configured SSH-capable VM bound to the given backend."""
    from smolvm.facade import SmolVM

    return SmolVM(backend=backend)


def _safe_teardown(vm) -> None:
    """Best-effort stop + delete; never raises.

    Cleanup chain:
      1. vm.stop(timeout=15) + vm.delete()
      2. on failure: SmolVMManager().delete(vm_id) (reloads fresh state)
      3. on failure: SIGKILL the PID directly + delete DB row.

    QEMU's stop path on macOS occasionally reports the process as still alive
    even after SIGKILL has cleared it; we recover by forcing the cleanup.
    """
    if vm is None:
        return
    vm_id = getattr(vm, "_vm_id", None)

    with suppress(Exception):
        vm.stop(timeout=15.0)

    try:
        vm.delete()
        return
    except Exception as e:  # noqa: BLE001
        logger.warning("vm.delete() failed (%s); retrying via SmolVMManager", e)

    if not vm_id:
        return

    from smolvm.vm import SmolVMManager

    try:
        with SmolVMManager() as sdk:
            sdk.delete(vm_id)
        return
    except Exception as e:  # noqa: BLE001
        logger.warning("SmolVMManager.delete failed (%s); forcing cleanup", e)

    _force_cleanup(vm_id)


def _force_cleanup(vm_id: str) -> None:
    """Last-resort cleanup: SIGKILL the PID, retry the manager's delete (which
    also tears down NAT, port forwards, sockets, and isolated disks), and only
    fall back to a raw state-row delete if that still fails."""
    import os
    import signal

    from smolvm.vm import SmolVMManager

    with suppress(Exception), SmolVMManager() as sdk:
        try:
            info = sdk.get(vm_id)
        except Exception:  # noqa: BLE001
            info = None
        if info is not None and info.pid:
            with suppress(ProcessLookupError, PermissionError):
                os.kill(info.pid, signal.SIGKILL)

        try:
            sdk.delete(vm_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "force-cleanup: sdk.delete still failed (%s); dropping DB row directly", e
            )
            with suppress(Exception):
                sdk.state.delete_vm(vm_id)
    logger.warning("force-cleaned %s", vm_id)


def _safe_delete_snapshot(snapshot_id: str) -> None:
    """Best-effort snapshot delete via SmolVMManager."""
    from smolvm.vm import SmolVMManager

    with suppress(Exception), SmolVMManager() as sdk:
        sdk.delete_snapshot(snapshot_id)


def _is_unsupported_error(exc: BaseException) -> bool:
    """Detect 'this backend does not support X' errors from the SDK.

    The runtime adapters (libkrun in particular) raise SmolVMError with messages
    like 'libkrun backend does not support snapshots yet'. NotImplementedError
    can also surface here from future adapters.
    """
    if isinstance(exc, NotImplementedError):
        return True
    return "does not support" in str(exc).lower()


# ── Individual benchmarks ────────────────────────────────────────────


def _bench_boot(backend: str, iterations: int, label: str) -> dict[str, Any]:
    """Shared implementation for cold-start and tti — they differ only in cache state."""
    from smolvm.facade import SmolVM

    construct: list[float] = []
    boot_to_ssh: list[float] = []
    totals: list[float] = []
    raw: list[dict[str, Any]] = []

    for i in range(iterations):
        logger.info("[%s] iter %d/%d", label, i + 1, iterations)
        vm: SmolVM | None = None
        record: dict[str, Any] = {"iter": i}
        try:
            with Phase() as p_construct:
                vm = _new_vm(backend)
            record["construct_ms"] = round(p_construct.elapsed_ms, 1)
            construct.append(record["construct_ms"])

            with Phase() as p_boot:
                vm.start()
                vm.wait_for_ssh()
            record["boot_to_ssh_ms"] = round(p_boot.elapsed_ms, 1)
            boot_to_ssh.append(record["boot_to_ssh_ms"])

            record["total_ms"] = round(record["construct_ms"] + record["boot_to_ssh_ms"], 1)
            totals.append(record["total_ms"])
        except Exception as e:  # noqa: BLE001
            record["error"] = repr(e)
            logger.warning("[%s] iter %d failed: %s", label, i + 1, e)
        finally:
            _safe_teardown(vm)
            raw.append(record)

    return {
        "stats": {
            "construct_ms": stats(construct),
            "boot_to_ssh_ms": stats(boot_to_ssh),
            "total_ms": stats(totals),
        },
        "raw": raw,
    }


def bench_cold_start(backend: str, iterations: int) -> dict[str, Any]:
    """First VM boot in this process. Image is assumed already pulled to disk;
    'cold' here means 'no warm SmolVM caches in memory, no per-VM disk overlay yet'."""
    return _bench_boot(backend, iterations, "cold-start")


def bench_tti(backend: str, iterations: int) -> dict[str, Any]:
    """Time-to-interactive after a warm-up boot (caches populated)."""
    logger.info("[tti] running excluded warm-up boot before measured iterations...")
    _bench_boot(backend, 1, "tti-warmup")
    return _bench_boot(backend, iterations, "tti")


def bench_pause_resume(backend: str, iterations: int) -> dict[str, Any]:
    """Pause and resume a single long-lived VM repeatedly."""
    pause: list[float] = []
    resume: list[float] = []
    raw: list[dict[str, Any]] = []

    vm = None
    try:
        logger.info("[pause-resume] starting source VM...")
        vm = _new_vm(backend)
        vm.start()
        vm.wait_for_ssh()

        for i in range(iterations):
            logger.info("[pause-resume] iter %d/%d", i + 1, iterations)
            record: dict[str, Any] = {"iter": i}
            try:
                with Phase() as p_pause:
                    vm.pause()
                record["pause_ms"] = round(p_pause.elapsed_ms, 1)
                pause.append(record["pause_ms"])

                with Phase() as p_resume:
                    vm.resume()
                record["resume_ms"] = round(p_resume.elapsed_ms, 1)
                resume.append(record["resume_ms"])
            except Exception as e:  # noqa: BLE001
                if _is_unsupported_error(e):
                    logger.warning("[pause-resume] backend does not support pause/resume: %s", e)
                    return {"status": "unsupported", "backend": backend, "reason": str(e)}
                # A non-unsupported failure mid-cycle (e.g. resume fails after pause
                # succeeded) leaves the VM in an indeterminate state. Record it and
                # stop — further iterations would measure noise on a broken VM.
                record["error"] = repr(e)
                logger.warning("[pause-resume] iter %d failed, aborting bench: %s", i + 1, e)
                raw.append(record)
                break
            raw.append(record)
    finally:
        _safe_teardown(vm)

    return {
        "stats": {
            "pause_ms": stats(pause),
            "resume_ms": stats(resume),
        },
        "raw": raw,
    }


def bench_snapshot(backend: str, iterations: int) -> dict[str, Any]:
    """Snapshot create + restore. Each iteration uses a fresh source VM."""
    from smolvm.facade import SmolVM

    create: list[float] = []
    restore: list[float] = []
    restore_to_ssh: list[float] = []
    raw: list[dict[str, Any]] = []

    for i in range(iterations):
        logger.info("[snapshot] iter %d/%d", i + 1, iterations)
        snapshot_id = f"bench-snap-{uuid.uuid4().hex[:8]}"
        record: dict[str, Any] = {"iter": i, "snapshot_id": snapshot_id}
        source_vm: SmolVM | None = None
        restored_vm: SmolVM | None = None
        try:
            source_vm = _new_vm(backend)
            source_vm.start()
            source_vm.wait_for_ssh()

            with Phase() as p_create:
                source_vm.snapshot(snapshot_id=snapshot_id)
            record["snapshot_create_ms"] = round(p_create.elapsed_ms, 1)
            create.append(record["snapshot_create_ms"])

            # snapshot() leaves source paused/stopped — fully delete it before restore.
            _safe_teardown(source_vm)
            source_vm = None

            with Phase() as p_restore:
                restored_vm = SmolVM.from_snapshot(snapshot_id, resume_vm=True)
            record["snapshot_restore_ms"] = round(p_restore.elapsed_ms, 1)
            restore.append(record["snapshot_restore_ms"])

            with Phase() as p_ssh:
                restored_vm.wait_for_ssh()
            record["snapshot_restore_to_ssh_ms"] = round(p_restore.elapsed_ms + p_ssh.elapsed_ms, 1)
            restore_to_ssh.append(record["snapshot_restore_to_ssh_ms"])
        except Exception as e:  # noqa: BLE001
            if _is_unsupported_error(e):
                logger.warning("[snapshot] backend does not support snapshots: %s", e)
                return {"status": "unsupported", "backend": backend, "reason": str(e)}
            record["error"] = repr(e)
            logger.warning("[snapshot] iter %d failed: %s", i + 1, e)
        finally:
            _safe_teardown(source_vm)
            _safe_teardown(restored_vm)
            _safe_delete_snapshot(snapshot_id)
            raw.append(record)

    return {
        "stats": {
            "snapshot_create_ms": stats(create),
            "snapshot_restore_ms": stats(restore),
            "snapshot_restore_to_ssh_ms": stats(restore_to_ssh),
        },
        "raw": raw,
    }


BENCHMARKS: dict[str, Callable[[str, int], dict[str, Any]]] = {
    "cold-start": bench_cold_start,
    "tti": bench_tti,
    "pause-resume": bench_pause_resume,
    "snapshot": bench_snapshot,
}


# ── Reporting ────────────────────────────────────────────────────────


def _print_human(report: dict[str, Any]) -> None:
    print("=" * 72)
    print("SmolVM Benchmark Report")
    print("=" * 72)
    print(f"smolvm   : {report['smolvm_version']}")
    print(f"platform : {report['platform']['system']} {report['platform']['machine']}")
    print(f"backend  : {report['backend']}")
    print(f"iters    : {report['iterations']}")
    print(f"duration : {report['duration_s']:.1f}s")
    print()

    for name, result in report["results"].items():
        if result.get("status") == "unsupported":
            print(f"── {name}: UNSUPPORTED on backend {result['backend']} ──")
            print(f"   {result.get('reason', '')}\n")
            continue

        print(f"── {name} ──")
        header = (
            f"  {'metric':30s} {'p50':>9s} {'p95':>9s} "
            f"{'mean':>9s} {'min':>9s} {'max':>9s} {'n':>4s}"
        )
        print(header)
        print("  " + "-" * (len(header) - 2))
        for metric, s in result["stats"].items():
            print(
                f"  {metric:30s} {s['p50']:8.1f}  {s['p95']:8.1f}  "
                f"{s['mean']:8.1f}  {s['min']:8.1f}  {s['max']:8.1f}  {s['count']:4d}"
            )
        print()


# ── Main ─────────────────────────────────────────────────────────────


def _smolvm_version() -> str:
    try:
        from importlib.metadata import version

        return version("smolvm")
    except Exception:  # noqa: BLE001
        return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark SmolVM lifecycle through the public SDK.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=5,
        help="Number of repetitions per benchmark (default: 5).",
    )
    parser.add_argument(
        "--only",
        type=str,
        default=None,
        help=f"Comma-separated subset of benchmarks. Choices: {', '.join(ALL_BENCHMARKS)}.",
    )
    parser.add_argument(
        "--backend",
        type=str,
        default="auto",
        choices=["auto", "qemu", "firecracker", "libkrun"],
        help="VM backend (default: auto -> qemu on macOS, firecracker on Linux).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Write JSON results to this path (implies --json).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(message)s",
    )

    if args.iterations < 1:
        parser.error("--iterations must be >= 1")

    if args.only:
        selected = [s.strip() for s in args.only.split(",") if s.strip()]
        unknown = [s for s in selected if s not in BENCHMARKS]
        if unknown:
            parser.error(
                f"Unknown benchmark(s): {', '.join(unknown)}. Choices: {', '.join(ALL_BENCHMARKS)}"
            )
    else:
        selected = list(ALL_BENCHMARKS)

    backend = _resolve_and_validate_backend(args.backend)

    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()

    results: dict[str, Any] = {}
    for name in selected:
        logger.info("=== Running benchmark: %s ===", name)
        results[name] = BENCHMARKS[name](backend, args.iterations)

    duration_s = round(time.monotonic() - started, 1)

    report: dict[str, Any] = {
        "smolvm_version": _smolvm_version(),
        "platform": {
            "system": _platform.system(),
            "release": _platform.release(),
            "machine": _platform.machine(),
        },
        "backend": backend,
        "iterations": args.iterations,
        "started_at": started_at,
        "duration_s": duration_s,
        "results": results,
    }

    emit_json = args.json or args.output is not None
    if emit_json:
        payload = json.dumps(report, indent=2)
        if args.output:
            Path(args.output).write_text(payload + "\n")
            print(f"Wrote {args.output}")
        else:
            print(payload)
    else:
        _print_human(report)


if __name__ == "__main__":
    main()
