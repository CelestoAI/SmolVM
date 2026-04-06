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
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from smolvm.cli_output import console_stdout, emit_json, render_empty, render_error, status_style
from smolvm.vm import SmolVMManager


@dataclass(frozen=True)
class CleanupFailure:
    """One failed VM deletion during cleanup."""

    vm_id: str
    error: str


@dataclass(frozen=True)
class CleanupSummary:
    """Cleanup result counters."""

    target_count: int
    deleted_count: int
    failed_count: int


@dataclass(frozen=True)
class CleanupResult:
    """Structured cleanup result payload."""

    delete_all: bool
    prefix: str
    dry_run: bool
    reconciled_stale_ids: list[str]
    targets: list[str]
    deleted: list[str]
    failed: list[CleanupFailure]
    summary: CleanupSummary


def add_cleanup_args(parser: argparse.ArgumentParser) -> None:
    """Add shared cleanup CLI arguments to a parser."""
    parser.add_argument(
        "--all",
        action="store_true",
        help="Delete all sandboxes, not just stale ones.",
    )
    parser.add_argument(
        "--prefix",
        default="vm-",
        help='Only clean sandboxes whose name starts with this prefix (default: "vm-").',
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be cleaned up without actually deleting.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )


def _cleanup_error_payload(exc: Exception) -> dict[str, str]:
    """Build the JSON error payload for cleanup failures."""
    return {
        "message": str(exc),
        "type": "runtime_error",
    }


def _render_cleanup_result(result: CleanupResult, *, warn_not_root: bool) -> None:
    """Render the human-friendly cleanup result."""
    console = console_stdout()

    if warn_not_root:
        console.print(
            Panel.fit(
                "Warning: not running as root. Cleanup may fail for TAP/nftables resources.",
                title="Warning",
                border_style="yellow",
            )
        )

    if result.reconciled_stale_ids:
        console.print(
            Panel.fit(
                "Reconciled stale VMs: " + ", ".join(result.reconciled_stale_ids),
                title="Reconciled",
                border_style="cyan",
            )
        )

    if not result.targets:
        render_empty("Cleanup", "No matching VMs to clean.")
        return

    targets_table = Table(title=f"Cleanup Targets ({len(result.targets)})")
    targets_table.add_column("VM")
    for vm_id in result.targets:
        targets_table.add_row(vm_id)
    console.print(targets_table)

    if result.dry_run:
        console.print(
            Panel.fit(
                "Dry run complete. No changes made.",
                title="Cleanup Summary",
                border_style="cyan",
            )
        )
        return

    results_table = Table(title="Cleanup Results")
    results_table.add_column("VM")
    results_table.add_column("Result")
    results_table.add_column("Error")

    failure_map = {failure.vm_id: failure.error for failure in result.failed}
    for vm_id in result.targets:
        if vm_id in failure_map:
            status = "failed"
            error = failure_map[vm_id]
        else:
            status = "deleted"
            error = "-"
        results_table.add_row(
            vm_id,
            Text(status, style=status_style(status)),
            error,
        )
    console.print(results_table)

    summary_style = "red" if result.summary.failed_count else "green"
    summary_body = (
        f"Deleted: {result.summary.deleted_count}\n"
        f"Failed: {result.summary.failed_count}\n"
        f"Targets: {result.summary.target_count}"
    )
    console.print(
        Panel.fit(
            summary_body,
            title="Cleanup Summary",
            border_style=summary_style,
        )
    )


def run_cleanup(
    *,
    delete_all: bool = False,
    prefix: str = "vm-",
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    """Clean stale/auto-created VMs and related resources."""
    warn_not_root = sys.platform == "linux" and os.geteuid() != 0

    try:
        with SmolVMManager() as sdk:
            stale_ids = sorted(set(sdk.reconcile()))

            vms = sdk.list_vms()
            if delete_all:
                target_ids = [vm.vm_id for vm in vms]
            else:
                target_ids = sorted(
                    {vm.vm_id for vm in vms if vm.vm_id.startswith(prefix) or vm.vm_id in stale_ids}
                )

            deleted: list[str] = []
            failed: list[CleanupFailure] = []
            if not dry_run:
                for vm_id in target_ids:
                    try:
                        sdk.delete(vm_id)
                        deleted.append(vm_id)
                    except Exception as exc:
                        failed.append(CleanupFailure(vm_id=vm_id, error=str(exc)))

            result = CleanupResult(
                delete_all=delete_all,
                prefix=prefix,
                dry_run=dry_run,
                reconciled_stale_ids=stale_ids,
                targets=target_ids,
                deleted=deleted,
                failed=failed,
                summary=CleanupSummary(
                    target_count=len(target_ids),
                    deleted_count=len(deleted),
                    failed_count=len(failed),
                ),
            )
            exit_code = 1 if failed else 0

            if json_output:
                emit_json("cleanup", exit_code, data=asdict(result))
            else:
                _render_cleanup_result(result, warn_not_root=warn_not_root)

            return exit_code
    except Exception as exc:
        if json_output:
            emit_json("cleanup", 1, data=None, error=_cleanup_error_payload(exc))
        else:
            render_error(f"Error: {exc}")
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cleanup stale SmolVM VMs/resources")
    add_cleanup_args(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Cleanup CLI entrypoint."""
    args = build_parser().parse_args(argv)
    return run_cleanup(
        delete_all=args.all,
        prefix=args.prefix,
        dry_run=args.dry_run,
        json_output=args.json,
    )
