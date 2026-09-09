#!/usr/bin/env python3
"""Measure cached-image policy startup on a disposable test machine.

Run this same script against the baseline checkout with --mode open, then the
candidate checkout. Keep runner, image, URL, and concurrency identical.
"""

from __future__ import annotations

import argparse
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener
from uuid import uuid4

from smolvm import SmolVM
from smolvm import facade as _facade
from smolvm.storage import MemoryStateManager
from smolvm.types import SnapshotType, VMConfig


def claim_forward_port(claimed: set[int], lock: Lock) -> int:
    """Keep an in-process claim after closing the probe socket for QEMU.

    Claims last through the batch's restore and cleanup. Only allocation is
    locked, so independent QEMU starts still run concurrently.
    """
    with lock:
        for _ in range(100):
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
                if port not in claimed:
                    claimed.add(port)
                    return port
    raise RuntimeError("Cannot allocate a benchmark forwarding port; retry on an idle runner")


def is_forward_bind_failure(error: Exception, log_path: Path) -> bool:
    """Retry only the generated host-forward's bind error, not workload failures."""
    detail = str(error).lower()
    if log_path.exists():
        detail += log_path.read_text(errors="replace").lower()
    return "could not set up host forwarding rule" in detail or (
        "local port" in detail and "already in use" in detail
    )


def wait_for_application(url: str, timeout: float = 30) -> None:
    """Time the first successful response, without SSH or a guest agent."""
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while True:
        try:
            with opener.open(url, timeout=0.25) as response:
                if response.status == 200 and response.read() == b"ready\n":
                    return
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Application did not become ready at {url}")
        time.sleep(0.01)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["open", "off", "restricted"], default="open")
    parser.add_argument("--allow", action="append", default=[], help="Allowed IPv4 address/range")
    parser.add_argument("--url", help="Controlled HTTP endpoint for first-request timing")
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--restore", action="store_true", help="Also measure disk snapshot restore")
    parser.add_argument("--vm-config", type=Path, help="QEMU VMConfig JSON for a cached test image")
    parser.add_argument(
        "--application-port",
        type=int,
        default=18080,
        help="Baked-in QEMU HTTP application returning ready\\n",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True, help="Disposable benchmark storage")
    args = parser.parse_args()
    if args.samples < 1 or args.concurrency < 1:
        parser.error("samples and concurrency must be positive")
    if args.mode == "restricted" and not args.allow:
        parser.error("restricted requires --allow")
    if args.url and urlparse(args.url).scheme not in {"http", "https"}:
        parser.error("url must use http or https")
    import shlex

    # Baseline auto-naming chooses from a small human-name pool without checking
    # the shared inventory. Avoid name collisions in this load test without
    # changing the create/start path or production code, on BOTH versions.
    _facade.generate_sandbox_name = lambda _existing, prefix="sbx": f"{prefix}-{uuid4().hex[:16]}"
    inventory = MemoryStateManager(args.data_dir)
    template = json.loads(args.vm_config.read_text()) if args.vm_config else None
    if template and template.get("backend") != "qemu":
        parser.error("--vm-config requires backend='qemu'")
    if template and (args.url or template.get("internet_settings") is not None):
        parser.error("--vm-config uses the baked application and --mode; omit url and saved policy")
    claimed_ports: set[int] = set()
    port_lock = Lock()

    def start_sample(work: dict, attempt: int = 0) -> None:
        kwargs = {
            "backend": "firecracker",
            "os": "alpine",
            "comm_channel": "vsock",
            "memory": 512,
            "data_dir": args.data_dir,
            "state_manager": inventory,
        }
        # Omission lets this script run unchanged against the baseline version.
        if args.mode != "open":
            kwargs["internet_settings"] = {"mode": args.mode, "allowed_cidrs": args.allow}
        if template:
            config = dict(template, vm_id=f"bench-{uuid4().hex[:16]}")
            if config.get("qemu_network", "slirp") == "slirp":
                port = claim_forward_port(claimed_ports, port_lock)
                work.setdefault("claimed_ports", []).append(port)
                config["port_forwards"] = [{"host_port": port, "guest_port": args.application_port}]
                work["url"] = f"http://127.0.0.1:{port}/"
            kwargs = {
                "config": VMConfig.model_validate(config),
                "data_dir": args.data_dir,
                "state_manager": inventory,
                **(
                    {"internet_settings": kwargs["internet_settings"]}
                    if "internet_settings" in kwargs
                    else {}
                ),
            }
        started = work.setdefault("started", time.monotonic())
        try:
            sandbox = SmolVM(**kwargs)
            work["vm"] = sandbox
            work["sdk"] = sandbox._sdk
            sandbox.start()
        except Exception as error:
            if not (
                template
                and config.get("qemu_network", "slirp") == "slirp"
                and attempt < 2
                and is_forward_bind_failure(error, args.data_dir / f"{config['vm_id']}.log")
            ):
                raise
            if work["vm"] is not None:
                work["vm"].stop(timeout=3)
                work["vm"].delete()
                work["vm"] = None
            # External processes do not participate in our claim set. Retry with
            # a different port, including the failed attempt in measured latency.
            return start_sample(work, attempt + 1)
        if template:
            work.setdefault(
                "url", f"http://{sandbox.info.network.guest_ip}:{args.application_port}/"
            )
            wait_for_application(work["url"])
            work["result"] = {
                "sample": work["index"],
                "first_application_ms": (time.monotonic() - started) * 1000,
                "forward_bind_retries": attempt,
            }
            return
        assert sandbox.run("true").exit_code == 0
        result = {"sample": work["index"], "first_command_ms": (time.monotonic() - started) * 1000}
        if args.url and args.mode != "off":
            assert sandbox.run(f"wget -T 5 -qO /dev/null {shlex.quote(args.url)}").exit_code == 0
            result["first_request_ms"] = (time.monotonic() - started) * 1000
        work["result"] = result

    def restore_sample(work: dict) -> None:
        started = time.monotonic()
        restored = SmolVM.from_snapshot(
            work["snapshot_id"],
            backend="qemu" if template else "firecracker",
            resume_vm=True,
            data_dir=args.data_dir,
            state_manager=inventory,
        )
        work["vm"] = restored
        if template:
            wait_for_application(work["url"])
            work["result"]["restore_first_application_ms"] = (time.monotonic() - started) * 1000
            return
        assert restored.run("true").exit_code == 0
        work["result"]["restore_first_command_ms"] = (time.monotonic() - started) * 1000

    def parallel(pool, function, batch):
        futures = [pool.submit(function, work) for work in batch]
        wait(futures)  # Finish every participant before cleanup or the next phase.
        for future in futures:
            future.result()

    def measure_batch(pool, indices):
        batch = [{"index": index, "vm": None, "snapshot_id": None} for index in indices]
        try:
            parallel(pool, start_sample, batch)
            if args.restore:
                # Snapshot preparation and disposal are outside timing. Complete
                # the whole batch before restore so no new start can take an IP
                # required by another sample's snapshot, and old TAPs are gone.
                for work in batch:
                    sandbox = work["vm"]
                    work["snapshot_id"] = sandbox.snapshot(
                        snapshot_type=SnapshotType.DISK,
                        # The baked application fixture has no writable workload
                        # or control agent. Measure crash-consistent disk restore.
                        **({"flush_policy": "skip"} if template else {}),
                    ).snapshot_id
                    sandbox.stop(timeout=3 if template else 0)
                    sandbox.delete()
                    work["vm"] = None
                parallel(pool, restore_sample, batch)
            return [work["result"] for work in batch]
        finally:
            # Preserve the workload error if cleanup also fails, but never call
            # a successful sample clean when disposal failed.
            import sys
            import traceback

            failed = sys.exc_info()[0] is not None
            cleanup_errors = []
            for work in batch:
                try:
                    if work["vm"] is not None:
                        try:
                            work["vm"].stop(timeout=3 if template else 0)
                        finally:
                            work["vm"].delete()
                except Exception as error:
                    cleanup_errors.append(error)
                    traceback.print_exc()
                finally:
                    if work["snapshot_id"] is not None:
                        try:
                            work["sdk"].delete_snapshot(work["snapshot_id"])
                        except Exception as error:
                            cleanup_errors.append(error)
                            traceback.print_exc()
            if cleanup_errors and not failed:
                raise cleanup_errors[0]
            with port_lock:
                for work in batch:
                    claimed_ports.difference_update(work.get("claimed_ports", []))

    with ThreadPoolExecutor(args.concurrency) as pool:
        # Explicit unrecorded warmup excludes image download/build from startup.
        measure_batch(pool, [-1])
        with args.output.open("w") as output:
            output.write(json.dumps({"configuration": vars(args)}, default=str) + "\n")
            for offset in range(0, args.samples, args.concurrency):
                indices = range(offset, min(offset + args.concurrency, args.samples))
                for result in measure_batch(pool, indices):
                    output.write(json.dumps(result) + "\n")
                output.flush()


if __name__ == "__main__":
    main()
