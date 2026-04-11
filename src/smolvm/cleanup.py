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

"""SmolVM delete utilities and CLI."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from smolvm.cli_output import console_stdout, emit_json, render_empty, render_error, status_style
from smolvm.vm import SmolVMManager


@dataclass(frozen=True)
class DeleteFailure:
    """One failed VM deletion."""

    vm_id: str
    error: str


@dataclass(frozen=True)
class DeleteSummary:
    """Delete result counters."""

    target_count: int
    deleted_count: int
    failed_count: int


@dataclass(frozen=True)
class DeleteResult:
    """Structured delete result payload."""

    delete_all: bool
    prefix: str
    dry_run: bool
    reconciled_stale_ids: list[str]
    targets: list[str]
    deleted: list[str]
    failed: list[DeleteFailure]
    summary: DeleteSummary


# ---------------------------------------------------------------------------
# Backward-compatible aliases so existing imports keep working
# ---------------------------------------------------------------------------
CleanupFailure = DeleteFailure
CleanupSummary = DeleteSummary
CleanupResult = DeleteResult


def add_delete_args(parser: argparse.ArgumentParser) -> None:
    """Add shared delete CLI arguments to a parser."""
    parser.add_argument(
        "vm_ids",
        nargs="*",
        metavar="vm-id",
        help="One or more VM IDs to delete.",
    )
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
        help="Show what would be deleted without actually deleting.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )


# Keep old name available for callers that import it.
add_cleanup_args = add_delete_args


def _delete_error_payload(exc: Exception) -> dict[str, str]:
    """Build the JSON error payload for delete failures."""
    return {
        "message": str(exc),
        "type": "runtime_error",
    }


def _render_delete_result(result: DeleteResult, *, warn_not_root: bool) -> None:
    """Render the human-friendly delete result."""
    console = console_stdout()

    if warn_not_root:
        console.print(
            Panel.fit(
                "Warning: not running as root. Deletion may fail for TAP/nftables resources.",
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
        render_empty("Delete", "No matching VMs to delete.")
        return

    targets_table = Table(title=f"Delete Targets ({len(result.targets)})")
    targets_table.add_column("VM")
    for vm_id in result.targets:
        targets_table.add_row(vm_id)
    console.print(targets_table)

    if result.dry_run:
        console.print(
            Panel.fit(
                "Dry run complete. No changes made.",
                title="Delete Summary",
                border_style="cyan",
            )
        )
        return

    results_table = Table(title="Delete Results")
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
            title="Delete Summary",
            border_style=summary_style,
        )
    )


def _delete_vms_concurrent(
    sdk: SmolVMManager,
    target_ids: list[str],
) -> tuple[list[str], list[DeleteFailure]]:
    """Delete VMs concurrently and return (deleted, failed) lists."""
    deleted: list[str] = []
    failed: list[DeleteFailure] = []

    if not target_ids:
        return deleted, failed

    # Single VM — skip thread overhead.
    if len(target_ids) == 1:
        vm_id = target_ids[0]
        try:
            sdk.delete(vm_id)
            deleted.append(vm_id)
        except Exception as exc:
            failed.append(DeleteFailure(vm_id=vm_id, error=str(exc)))
        return deleted, failed

    def _do_delete(vm_id: str) -> tuple[str, Exception | None]:
        try:
            sdk.delete(vm_id)
            return vm_id, None
        except Exception as exc:  # noqa: BLE001
            return vm_id, exc

    with ThreadPoolExecutor(max_workers=min(len(target_ids), 8)) as pool:
        futures = {pool.submit(_do_delete, vm_id): vm_id for vm_id in target_ids}
        for future in as_completed(futures):
            vm_id, exc = future.result()
            if exc is None:
                deleted.append(vm_id)
            else:
                failed.append(DeleteFailure(vm_id=vm_id, error=str(exc)))

    return deleted, failed


def run_delete(
    *,
    vm_ids: list[str] | None = None,
    delete_all: bool = False,
    prefix: str = "vm-",
    dry_run: bool = False,
    json_output: bool = False,
) -> int:
    """Delete VMs — by explicit IDs, by prefix, or all."""
    warn_not_root = sys.platform == "linux" and os.geteuid() != 0

    try:
        with SmolVMManager() as sdk:
            stale_ids = sorted(set(sdk.reconcile()))

            if vm_ids:
                # Explicit VM IDs provided — delete exactly those.
                target_ids = list(vm_ids)
            elif delete_all:
                vms = sdk.list_vms()
                target_ids = [vm.vm_id for vm in vms]
            else:
                vms = sdk.list_vms()
                target_ids = sorted(
                    {vm.vm_id for vm in vms if vm.vm_id.startswith(prefix) or vm.vm_id in stale_ids}
                )

            deleted: list[str] = []
            failed: list[DeleteFailure] = []
            if not dry_run:
                deleted, failed = _delete_vms_concurrent(sdk, target_ids)

            result = DeleteResult(
                delete_all=delete_all,
                prefix=prefix,
                dry_run=dry_run,
                reconciled_stale_ids=stale_ids,
                targets=target_ids,
                deleted=deleted,
                failed=failed,
                summary=DeleteSummary(
                    target_count=len(target_ids),
                    deleted_count=len(deleted),
                    failed_count=len(failed),
                ),
            )
            exit_code = 1 if failed else 0

            if json_output:
                emit_json("delete", exit_code, data=asdict(result))
            else:
                _render_delete_result(result, warn_not_root=warn_not_root)

            return exit_code
    except Exception as exc:
        if json_output:
            emit_json("delete", 1, data=None, error=_delete_error_payload(exc))
        else:
            render_error(f"Error: {exc}")
        return 1


# Backward-compatible alias.
run_cleanup = run_delete


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Delete SmolVM VMs/resources")
    add_delete_args(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Delete CLI entrypoint."""
    args = build_parser().parse_args(argv)
    return run_delete(
        vm_ids=args.vm_ids or None,
        delete_all=args.all,
        prefix=args.prefix,
        dry_run=args.dry_run,
        json_output=args.json,
    )
