#!/usr/bin/env python3
"""Measure cached-image policy startup; run on a disposable Linux Firecracker host.

Run this same script against the baseline checkout with --mode open, then the
candidate checkout. Keep runner, image, URL, and concurrency identical.
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from smolvm import SmolVM
from smolvm import facade as _facade
from smolvm.storage import MemoryStateManager
from smolvm.types import SnapshotType


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["open", "off", "restricted"], default="open")
    parser.add_argument("--allow", action="append", default=[], help="Allowed IPv4 address/range")
    parser.add_argument("--url", help="Controlled HTTP endpoint for first-request timing")
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--restore", action="store_true", help="Also measure disk snapshot restore")
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

    def start_sample(work: dict) -> None:
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
        started = time.monotonic()
        sandbox = SmolVM(**kwargs)
        work["vm"] = sandbox
        work["sdk"] = sandbox._sdk
        sandbox.start()
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
            backend="firecracker",
            resume_vm=True,
            data_dir=args.data_dir,
            state_manager=inventory,
        )
        work["vm"] = restored
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
                        snapshot_type=SnapshotType.DISK
                    ).snapshot_id
                    sandbox.stop(timeout=0)
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
                            work["vm"].stop(timeout=0)
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
