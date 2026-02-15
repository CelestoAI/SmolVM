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

"""SmolVM cleanup utilities and CLI."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

from smolvm.vm import SmolVMManager


def run_cleanup(*, delete_all: bool = False, prefix: str = "vm-", dry_run: bool = False) -> int:
    """Clean stale/auto-created VMs and related resources.

    Args:
        delete_all: If True, delete every VM known to state.
        prefix: VM ID prefix for auto-created VMs to target by default.
        dry_run: If True, print targets without deleting.

    Returns:
        Process-style exit code (0 success, non-zero if failures occurred).
    """
    if os.geteuid() != 0:
        print("Warning: not running as root. Cleanup may fail for TAP/iptables resources.")

    sdk = SmolVMManager()
    try:
        stale_ids = set(sdk.reconcile())
        if stale_ids:
            print("Reconciled stale VMs:", ", ".join(sorted(stale_ids)))

        vms = sdk.list_vms()
        if delete_all:
            target_ids = [vm.vm_id for vm in vms]
        else:
            target_ids = sorted(
                {
                    vm.vm_id
                    for vm in vms
                    if vm.vm_id.startswith(prefix) or vm.vm_id in stale_ids
                }
            )

        if not target_ids:
            print("No matching VMs to clean.")
            return 0

        print(f"Targets ({len(target_ids)}):")
        for vm_id in target_ids:
            print(f"  - {vm_id}")

        if dry_run:
            print("Dry run complete. No changes made.")
            return 0

        failed = 0
        for vm_id in target_ids:
            try:
                sdk.delete(vm_id)
                print(f"Deleted: {vm_id}")
            except Exception as exc:
                failed += 1
                print(f"Failed:  {vm_id} ({exc})")

        if failed:
            print(f"Cleanup completed with {failed} failure(s).")
            return 1

        print("Cleanup complete.")
        return 0
    finally:
        sdk.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cleanup stale SmolVM VMs/resources")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Delete all VMs (not just stale/auto-created ones).",
    )
    parser.add_argument(
        "--prefix",
        default="vm-",
        help='Auto-VM prefix to clean (default: "vm-").',
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print targets without deleting.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Cleanup CLI entrypoint."""
    args = build_parser().parse_args(argv)
    return run_cleanup(delete_all=args.all, prefix=args.prefix, dry_run=args.dry_run)
