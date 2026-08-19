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

"""SmolVM delete & cleanup utilities and CLI."""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from smolvm.cli.output import (
    console_stdout,
    emit_error,
    emit_json,
    render_empty,
    render_error,
    status_style,
)
from smolvm.cli.service import CLIService

if TYPE_CHECKING:
    from smolvm.vm import SmolVMManager


# ---------------------------------------------------------------------------
# Shared data types
# ---------------------------------------------------------------------------


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
class LeftoverEntry:
    """One file left behind by a sandbox that no longer exists."""

    vm_id: str
    path: str
    kind: str
    size_bytes: int
    retained: bool


@dataclass(frozen=True)
class PruneResult:
    """Structured result for ``smolvm sandbox prune``."""

    removed: list[LeftoverEntry]
    kept: list[LeftoverEntry]
    freed_bytes: int
    dry_run: bool = False


@dataclass(frozen=True)
class DeleteResult:
    """Structured delete/cleanup result payload."""

    targets: list[str]
    deleted: list[str]
    failed: list[DeleteFailure]
    summary: DeleteSummary
    dry_run: bool = False
    reconciled_stale_ids: list[str] | None = None


# ---------------------------------------------------------------------------
# Shared rendering
# ---------------------------------------------------------------------------


def _error_payload(exc: Exception) -> dict[str, str]:
    return {
        "message": str(exc),
        "code": "runtime_error",
    }


def _render_result(result: DeleteResult, *, command: str, warn_not_root: bool) -> None:
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

    title = command.capitalize()

    if not result.targets:
        render_empty(title, "No matching VMs to delete.")
        return

    targets_table = Table(title=f"{title} Targets ({len(result.targets)})")
    targets_table.add_column("VM")
    for vm_id in result.targets:
        targets_table.add_row(vm_id)
    console.print(targets_table)

    if result.dry_run:
        console.print(
            Panel.fit(
                "Dry run complete. No changes made.",
                title=f"{title} Summary",
                border_style="cyan",
            )
        )
        return

    results_table = Table(title=f"{title} Results")
    results_table.add_column("VM")
    results_table.add_column("Result")
    results_table.add_column("Error")

    failure_map = {f.vm_id: f.error for f in result.failed}
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
            title=f"{title} Summary",
            border_style=summary_style,
        )
    )


# ---------------------------------------------------------------------------
# Concurrent deletion engine
# ---------------------------------------------------------------------------


def _delete_one(sdk: SmolVMManager, vm_id: str) -> None:
    """Delete a sandbox, treating a reclaimed leftover as a successful delete.

    A sandbox whose inventory row is already gone still owns files on disk.
    ``delete()`` removes them and then reports the missing row, so only call
    that a failure when there was nothing left to clean up.
    """
    from smolvm.exceptions import VMNotFoundError

    leftovers = sdk.leftover_paths_for_vm(vm_id)
    try:
        sdk.delete(vm_id)
    except VMNotFoundError:
        if not leftovers or any(path.exists() for path in leftovers):
            raise


def _delete_vms_concurrent(
    sdk: SmolVMManager,
    target_ids: list[str],
) -> tuple[list[str], list[DeleteFailure]]:
    """Delete VMs concurrently and return (deleted, failed) lists."""
    deleted: list[str] = []
    failed: list[DeleteFailure] = []

    if not target_ids:
        return deleted, failed

    if len(target_ids) == 1:
        vm_id = target_ids[0]
        try:
            _delete_one(sdk, vm_id)
            deleted.append(vm_id)
        except Exception as exc:
            failed.append(DeleteFailure(vm_id=vm_id, error=str(exc)))
        return deleted, failed

    def _do_delete(vm_id: str) -> tuple[str, Exception | None]:
        try:
            _delete_one(sdk, vm_id)
            return vm_id, None
        except Exception as exc:  # noqa: BLE001
            return vm_id, exc

    with ThreadPoolExecutor(max_workers=min(len(target_ids), 8)) as pool:
        futures = {pool.submit(_do_delete, vm_id): vm_id for vm_id in target_ids}
        for future in as_completed(futures):
            vm_id, delete_error = future.result()
            if delete_error is None:
                deleted.append(vm_id)
            else:
                failed.append(DeleteFailure(vm_id=vm_id, error=str(delete_error)))

    return deleted, failed


# ---------------------------------------------------------------------------
# smolvm sandbox delete <vm-id> [vm-id ...]
# ---------------------------------------------------------------------------


def run_delete(
    *,
    vm_ids: list[str],
    dry_run: bool = False,
    json_output: bool = False,
    command_name: str = "sandbox.delete",
) -> int:
    """Delete specific VMs by ID."""
    warn_not_root = sys.platform == "linux" and os.geteuid() != 0

    try:
        with CLIService().manager() as sdk:
            deleted: list[str] = []
            failed: list[DeleteFailure] = []
            if not dry_run:
                deleted, failed = _delete_vms_concurrent(sdk, vm_ids)

            result = DeleteResult(
                targets=vm_ids,
                deleted=deleted,
                failed=failed,
                dry_run=dry_run,
                summary=DeleteSummary(
                    target_count=len(vm_ids),
                    deleted_count=len(deleted),
                    failed_count=len(failed),
                ),
            )
            exit_code = 1 if failed else 0

            if json_output:
                emit_json(command_name, exit_code, data=asdict(result))
            else:
                _render_result(result, command="delete", warn_not_root=warn_not_root)

            return exit_code
    except Exception as exc:
        if json_output:
            emit_error(command_name, exit_code=1, **_error_payload(exc))
        else:
            render_error(f"Error: {exc}")
        return 1


# ---------------------------------------------------------------------------
# smolvm sandbox delete --all
# ---------------------------------------------------------------------------


def _confirm_cleanup(
    target_ids: list[str],
    *,
    force: bool,
    json_output: bool,
    command_name: str,
) -> int | None:
    """Confirm the destructive cleanup with the user.

    Returns ``None`` to proceed, or an exit code to return from
    ``run_cleanup``. A clean user abort at the prompt returns ``0``;
    refusing to run unattended (``--json`` without ``--force``, or no TTY)
    returns ``1``.
    """
    if force:
        return None

    if json_output:
        emit_error(
            command_name,
            "refused",
            "Refusing to delete VMs without --force in --json mode. "
            "Run 'smolvm sandbox delete --all --force --json' to confirm.",
            recovery="Run 'smolvm sandbox delete --all --force --json' to confirm.",
            exit_code=1,
        )
        return 1

    if not sys.stdin.isatty():
        render_error(
            "Refusing to delete VMs without a confirmation prompt. Pass --force to confirm."
        )
        return 1

    console = console_stdout()
    console.print(
        f"This will permanently delete [bold]{len(target_ids)}[/bold] VM(s): "
        f"{', '.join(target_ids)}"
    )
    console.print("[yellow]This action cannot be undone.[/yellow]")
    try:
        console.print("Continue? \\[y/N] ", end="")
        answer = input("").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("\nAborted.")
        return 0
    if answer not in {"y", "yes"}:
        console.print("Aborted.")
        return 0
    return None


def run_cleanup(
    *,
    dry_run: bool = False,
    json_output: bool = False,
    force: bool = False,
    command_name: str = "sandbox.delete",
) -> int:
    """Delete all VMs."""
    warn_not_root = sys.platform == "linux" and os.geteuid() != 0

    try:
        with CLIService().manager() as sdk:
            stale_ids = sorted(set(sdk.reconcile()))
            vms = sdk.list_vms()
            target_ids = [vm.vm_id for vm in vms]

            deleted: list[str] = []
            failed: list[DeleteFailure] = []
            if not dry_run and target_ids:
                abort_code = _confirm_cleanup(
                    target_ids,
                    force=force,
                    json_output=json_output,
                    command_name=command_name,
                )
                if abort_code is not None:
                    return abort_code
            if not dry_run:
                deleted, failed = _delete_vms_concurrent(sdk, target_ids)

            result = DeleteResult(
                targets=target_ids,
                deleted=deleted,
                failed=failed,
                dry_run=dry_run,
                reconciled_stale_ids=stale_ids,
                summary=DeleteSummary(
                    target_count=len(target_ids),
                    deleted_count=len(deleted),
                    failed_count=len(failed),
                ),
            )
            exit_code = 1 if failed else 0

            if json_output:
                emit_json(command_name, exit_code, data=asdict(result))
            else:
                _render_result(result, command="delete", warn_not_root=warn_not_root)

            return exit_code
    except Exception as exc:
        if json_output:
            emit_error(command_name, exit_code=1, **_error_payload(exc))
        else:
            render_error(f"Error: {exc}")
        return 1


# ---------------------------------------------------------------------------
# smolvm sandbox prune
# ---------------------------------------------------------------------------


def _format_bytes(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _to_entry(artifact: Any) -> LeftoverEntry:
    return LeftoverEntry(
        vm_id=artifact.vm_id,
        path=str(artifact.path),
        kind=artifact.kind,
        size_bytes=artifact.size_bytes,
        retained=artifact.retained,
    )


def _confirm_prune(entries: list[LeftoverEntry], *, freed_bytes: int) -> bool:
    """Ask before deleting. Returns False when the user backs out."""
    console = console_stdout()
    console.print(
        f"This will delete [bold]{len(entries)}[/bold] file(s) "
        f"({_format_bytes(freed_bytes)}) left over from sandboxes that no longer exist."
    )
    console.print("[yellow]This action cannot be undone.[/yellow]")
    try:
        console.print("Continue? \\[y/N] ", end="")
        answer = input("").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("\nAborted.")
        return False
    if answer not in {"y", "yes"}:
        console.print("Aborted.")
        return False
    return True


def _render_prune(result: PruneResult) -> None:
    console = console_stdout()

    if result.kept:
        kept_bytes = sum(entry.size_bytes for entry in result.kept)
        console.print(
            Panel.fit(
                f"Kept {len(result.kept)} file(s) ({_format_bytes(kept_bytes)}) you asked "
                "SmolVM to save. Run 'smolvm sandbox prune --include-saved' to delete "
                "those too.",
                title="Kept",
                border_style="cyan",
            )
        )

    if not result.removed:
        render_empty("Prune", "Nothing to clean up.")
        return

    table = Table(
        title=f"{'Would remove' if result.dry_run else 'Removed'} ({len(result.removed)})"
    )
    table.add_column("Sandbox")
    table.add_column("File")
    table.add_column("Size", justify="right")
    for entry in result.removed:
        table.add_row(entry.vm_id, entry.path, _format_bytes(entry.size_bytes))
    console.print(table)

    verb = "Would free" if result.dry_run else "Freed"
    console.print(
        Panel.fit(
            f"{verb} {_format_bytes(result.freed_bytes)}",
            title="Prune Summary",
            border_style="cyan" if result.dry_run else "green",
        )
    )


def run_prune_sandboxes(
    *,
    dry_run: bool = False,
    force: bool = False,
    include_retained: bool = False,
    json_output: bool = False,
    command_name: str = "sandbox.prune",
) -> int:
    """Delete disks and logs left behind by sandboxes that no longer exist."""
    try:
        with CLIService().manager() as sdk:
            sdk.reconcile()
            artifacts = sdk.find_leftover_artifacts()
            candidates = [
                _to_entry(artifact)
                for artifact in artifacts
                if include_retained or not artifact.retained
            ]
            kept = (
                []
                if include_retained
                else [_to_entry(artifact) for artifact in artifacts if artifact.retained]
            )
            freed_bytes = sum(entry.size_bytes for entry in candidates)

            if candidates and not dry_run and not force:
                if json_output:
                    return emit_error(
                        command_name,
                        "refused",
                        "Refusing to delete leftover files without --force in --json mode. "
                        "Run 'smolvm sandbox prune --force --json' to confirm.",
                        recovery="Run 'smolvm sandbox prune --force --json' to confirm.",
                        exit_code=1,
                    )
                if not sys.stdin.isatty():
                    render_error(
                        "Refusing to delete leftover files without a confirmation prompt. "
                        "Pass --force to confirm."
                    )
                    return 1
                if not _confirm_prune(candidates, freed_bytes=freed_bytes):
                    return 0

            removed = candidates
            if not dry_run and candidates:
                removed = [
                    _to_entry(artifact)
                    for artifact in sdk.prune_leftover_artifacts(include_retained=include_retained)
                ]
                freed_bytes = sum(entry.size_bytes for entry in removed)

            result = PruneResult(
                removed=removed,
                kept=kept,
                freed_bytes=freed_bytes,
                dry_run=dry_run,
            )

            if json_output:
                emit_json(command_name, 0, data=asdict(result))
            else:
                _render_prune(result)
            return 0
    except Exception as exc:
        if json_output:
            emit_error(command_name, exit_code=1, **_error_payload(exc))
        else:
            render_error(f"Error: {exc}")
        return 1
