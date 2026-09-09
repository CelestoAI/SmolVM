#!/usr/bin/env python3
"""Measure cached-image policy startup; run on a disposable Linux Firecracker host.

Run this same script against the baseline checkout with --mode open, then the
candidate checkout. Keep runner, image, URL, and concurrency identical.
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from smolvm import SmolVM
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

    inventory = MemoryStateManager(args.data_dir)

    def sample(index: int) -> dict:
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
        target = sandbox
        snapshot_id = None
        try:
            sandbox.start()
            assert sandbox.run("true").exit_code == 0
            result = {"sample": index, "first_command_ms": (time.monotonic() - started) * 1000}
            if args.url and args.mode != "off":
                assert (
                    sandbox.run(f"wget -T 5 -qO /dev/null {shlex.quote(args.url)}").exit_code == 0
                )
                result["first_request_ms"] = (time.monotonic() - started) * 1000
            if args.restore:
                snap = sandbox.snapshot(snapshot_type=SnapshotType.DISK)
                snapshot_id = snap.snapshot_id
                sandbox.stop()
                sandbox.delete()
                started = time.monotonic()
                target = SmolVM.from_snapshot(
                    snap.snapshot_id,
                    backend="firecracker",
                    resume_vm=True,
                    data_dir=args.data_dir,
                    state_manager=inventory,
                )
                assert target.run("true").exit_code == 0
                result["restore_first_command_ms"] = (time.monotonic() - started) * 1000
            return result
        finally:
            try:
                target.delete()
            finally:
                if snapshot_id is not None:
                    target._sdk.delete_snapshot(snapshot_id)

    # Explicit unrecorded warmup separates image download/build from startup.
    sample(-1)
    with args.output.open("w") as output, ThreadPoolExecutor(args.concurrency) as pool:
        output.write(json.dumps({"configuration": vars(args)}, default=str) + "\n")
        for result in pool.map(sample, range(args.samples)):
            output.write(json.dumps(result) + "\n")
            output.flush()


if __name__ == "__main__":
    main()
