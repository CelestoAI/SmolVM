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

"""Top-level SmolVM CLI."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import os
import platform
import re
import subprocess
from collections.abc import Sequence
from typing import TYPE_CHECKING, TypedDict

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from smolvm.cleanup import add_cleanup_args, run_cleanup
from smolvm.cli_output import console_stdout, emit_json, render_empty, render_error, status_style
from smolvm.doctor import run_doctor
from smolvm.types import BrowserSessionState, GuestOS, VMState

if TYPE_CHECKING:
    from smolvm.types import BrowserSessionInfo, SnapshotInfo, VMInfo

DASHBOARD_ALLOW_BETA_ENV = "SMOLVM_DASHBOARD_ALLOW_BETA"
DASHBOARD_URL_ENV = "SMOLVM_DASHBOARD_URL"
ENV_RELOAD_HINT = "source /etc/profile.d/smolvm_env.sh"

# Matches PEP 440 pre-release and dev-release version suffixes,
# e.g. "0.0.5.a1", "0.0.5b2", "0.0.5.dev1", "0.0.5rc1".
_PRERELEASE_RE = re.compile(r"[._]?(a|b|rc|alpha|beta|dev)\d*", re.IGNORECASE)


class VmRow(TypedDict):
    """Machine-readable data for a listed VM."""

    name: str
    status: str
    pid: int | None
    ip_address: str | None
    ssh_port: int | None


class ListFiltersPayload(TypedDict):
    """Filter metadata included with list output."""

    all: bool
    status: str | None


class ListPayload(TypedDict):
    """JSON payload for ``smolvm list``."""

    filters: ListFiltersPayload
    vms: list[VmRow]


class CreateVmPayload(TypedDict):
    """Machine-readable VM details for ``smolvm create``."""

    name: str
    status: str
    os: str
    backend: str
    ip_address: str | None
    ssh_port: int | None


class CreateNextPayload(TypedDict):
    """Suggested follow-up action for ``smolvm create``."""

    ssh_command: str


class CreatePayload(TypedDict):
    """JSON payload for ``smolvm create``."""

    vm: CreateVmPayload
    next: CreateNextPayload


class StopVmPayload(TypedDict):
    """Machine-readable VM details for lifecycle commands."""

    name: str
    status: str


class StopPayload(TypedDict):
    """JSON payload for VM lifecycle commands."""

    vm: StopVmPayload


class SnapshotRow(TypedDict):
    """Machine-readable data for a listed snapshot."""

    snapshot_id: str
    vm_id: str
    restored: bool
    restored_vm_id: str | None
    created_at: str
    snapshot_path: str
    mem_file_path: str
    disk_path: str


class SnapshotListFiltersPayload(TypedDict):
    """Filter metadata included with snapshot list output."""

    vm_id: str | None


class SnapshotListPayload(TypedDict):
    """JSON payload for ``smolvm snapshot list``."""

    filters: SnapshotListFiltersPayload
    snapshots: list[SnapshotRow]


class SnapshotPayload(TypedDict):
    """JSON payload for snapshot create/restore/delete operations."""

    snapshot: SnapshotRow


class SnapshotRestoreVmPayload(TypedDict):
    """Machine-readable VM details for ``smolvm snapshot restore``."""

    name: str
    status: str
    ip_address: str | None
    ssh_port: int | None


class SnapshotRestorePayload(TypedDict):
    """JSON payload for ``smolvm snapshot restore``."""

    snapshot: SnapshotRow
    vm: SnapshotRestoreVmPayload


class BrowserRow(TypedDict):
    """Machine-readable data for a listed browser session."""

    session_id: str
    vm_id: str
    status: str
    cdp_url: str | None
    live_url: str | None
    profile_id: str | None


class BrowserListFiltersPayload(TypedDict):
    """Filter metadata included with browser list output."""

    status: str | None


class BrowserListPayload(TypedDict):
    """JSON payload for ``smolvm browser list``."""

    filters: BrowserListFiltersPayload
    sessions: list[BrowserRow]


class BrowserSessionPayload(TypedDict):
    """Machine-readable session details for ``smolvm browser start``."""

    session_id: str
    vm_id: str
    status: str
    cdp_url: str | None
    live_url: str | None
    profile_id: str | None
    artifacts_dir: str | None


def _current_version_is_prerelease() -> bool:
    """Return True if the installed smolvm package version is a pre-release."""
    try:
        ver = importlib.metadata.version("smolvm")
    except importlib.metadata.PackageNotFoundError:
        return False

    try:
        from packaging.version import InvalidVersion, Version

        return Version(ver).is_prerelease
    except (ImportError, InvalidVersion):
        return bool(_PRERELEASE_RE.search(ver))


def _positive_float(value: str) -> float:
    """argparse type enforcing a strictly positive floating-point number."""
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be > 0")
    return parsed


class _LinuxOnlyOption(argparse.Action):
    """Reject setup flags that are only valid on Linux when used on macOS."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[str] | None,
        option_string: str | None = None,
    ) -> None:
        if platform.system() == "Darwin":
            parser.error(f"argument {option_string}: only supported on Linux")

        if self.nargs == 0:
            setattr(namespace, self.dest, self.const if self.const is not None else True)
            return

        setattr(namespace, self.dest, values)


def _add_ssh_auth_args(command_parser: argparse.ArgumentParser) -> None:
    """Add common SSH identity arguments to a command parser."""
    command_parser.add_argument(
        "--ssh-key",
        default=None,
        help="SSH private key path (default fallback: ~/.smolvm/keys/id_ed25519).",
    )
    command_parser.add_argument(
        "--ssh-user",
        default="root",
        help="SSH user (default: root).",
    )


def _add_boot_timeout_arg(command_parser: argparse.ArgumentParser) -> None:
    """Add a shared boot/SSH readiness timeout flag."""
    command_parser.add_argument(
        "--boot-timeout",
        type=_positive_float,
        default=30.0,
        help="Seconds to wait for VM boot and SSH readiness (default: 30).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smolvm",
        description="SmolVM command-line tools",
        epilog=(
            "Most non-interactive commands support --json to emit machine-readable "
            "output for LLMs, agents, and automation."
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    cleanup = subparsers.add_parser(
        "cleanup",
        help="Clean stale SmolVM resources",
    )
    add_cleanup_args(cleanup)

    doctor = subparsers.add_parser(
        "doctor",
        help="Run host diagnostics for the selected backend",
    )
    doctor.add_argument(
        "--backend",
        choices=["auto", "firecracker", "qemu"],
        default=None,
        help="Backend to validate (default: auto).",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    doctor.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as failures.",
    )

    setup = subparsers.add_parser(
        "setup",
        help="Install or validate one-time host prerequisites",
    )
    setup.add_argument(
        "--check-only",
        action="store_true",
        help="Validate prerequisites without installing anything.",
    )
    setup.add_argument(
        "--with-docker",
        action="store_true",
        help="Install or validate Docker too.",
    )
    setup.add_argument(
        "--no-configure-runtime",
        action=_LinuxOnlyOption,
        nargs=0,
        const=True,
        default=False,
        help="Skip scoped runtime sudoers setup (Linux only).",
    )
    setup.add_argument(
        "--skip-deps",
        action=_LinuxOnlyOption,
        nargs=0,
        const=True,
        default=False,
        help="Skip Linux package dependency installation (Linux only).",
    )
    setup.add_argument(
        "--runtime-user",
        action=_LinuxOnlyOption,
        default=None,
        help="Target user for Linux runtime privilege configuration (Linux only).",
    )
    setup.add_argument(
        "--remove-runtime-config",
        action=_LinuxOnlyOption,
        nargs=0,
        const=True,
        default=False,
        help="Remove Linux runtime privilege configuration (Linux only).",
    )

    def _add_ui_args(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--host",
            default="127.0.0.1",
            help="Bind host (default: 127.0.0.1).",
        )
        command_parser.add_argument(
            "--port",
            type=int,
            default=8080,
            help="Bind port (default: 8080).",
        )
        command_parser.add_argument(
            "--allow-beta",
            action="store_true",
            help="Allow dashboard UI downloads from prerelease/beta tags.",
        )

    ui = subparsers.add_parser(
        "ui",
        help="Start the SmolVM dashboard UI server",
    )
    _add_ui_args(ui)

    list_parser = subparsers.add_parser(
        "list",
        help="List SmolVMs and their status",
    )
    list_filters = list_parser.add_mutually_exclusive_group()
    list_filters.add_argument(
        "--all",
        action="store_true",
        help="Include paused, stopped, created, and error VMs in addition to running ones.",
    )
    list_filters.add_argument(
        "--status",
        choices=[state.value for state in VMState],
        default=None,
        help="Filter VMs by lifecycle status.",
    )
    list_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    create_parser = subparsers.add_parser(
        "create",
        help="Create an SSH-ready VM and leave it running",
    )
    create_parser.add_argument(
        "--name",
        help="VM identifier to create (default: auto-generated).",
    )
    create_parser.add_argument(
        "--os",
        choices=[guest_os.value for guest_os in GuestOS],
        default=None,
        help="Guest OS for the auto-configured VM (default: alpine).",
    )
    create_parser.add_argument(
        "--memory-mib",
        type=int,
        default=None,
        help="Guest memory in MiB (default: 512).",
    )
    create_parser.add_argument(
        "--disk-size-mib",
        type=int,
        default=None,
        help="Root filesystem size in MiB (default: 512).",
    )
    create_parser.add_argument(
        "--backend",
        choices=["auto", "firecracker", "qemu"],
        default=None,
        help="Backend override (default: auto).",
    )
    create_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    _add_boot_timeout_arg(create_parser)

    stop_parser = subparsers.add_parser(
        "stop",
        help="Stop an existing VM",
    )
    stop_parser.add_argument("vm_id", help="VM identifier")
    stop_parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=3.0,
        help="Seconds to wait for graceful shutdown (default: 3).",
    )
    stop_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    pause_parser = subparsers.add_parser(
        "pause",
        help="Pause a running Firecracker VM",
    )
    pause_parser.add_argument("vm_id", help="VM identifier")
    pause_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    resume_parser = subparsers.add_parser(
        "resume",
        help="Resume a paused Firecracker VM",
    )
    resume_parser.add_argument("vm_id", help="VM identifier")
    resume_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    snapshot_parser = subparsers.add_parser(
        "snapshot",
        help="Manage Firecracker VM snapshots",
    )
    snapshot_sub = snapshot_parser.add_subparsers(dest="snapshot_action")

    snapshot_create = snapshot_sub.add_parser(
        "create",
        help="Create a full snapshot for a VM",
    )
    snapshot_create.add_argument("vm_id", help="VM identifier")
    snapshot_create.add_argument(
        "--snapshot-id",
        default=None,
        help="Optional snapshot identifier.",
    )
    snapshot_create.add_argument(
        "--resume-source",
        action="store_true",
        help="Resume the source VM after snapshot creation.",
    )
    snapshot_create.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    snapshot_restore = snapshot_sub.add_parser(
        "restore",
        help="Restore a snapshot back into its original VM identity",
    )
    snapshot_restore.add_argument("snapshot_id", help="Snapshot identifier")
    snapshot_restore.add_argument(
        "--resume",
        action="store_true",
        help="Resume the restored VM immediately.",
    )
    snapshot_restore.add_argument(
        "--force",
        action="store_true",
        help="Allow restoring a snapshot that was already restored before.",
    )
    snapshot_restore.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    snapshot_delete = snapshot_sub.add_parser(
        "delete",
        help="Delete a snapshot and its files",
    )
    snapshot_delete.add_argument("snapshot_id", help="Snapshot identifier")
    snapshot_delete.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    snapshot_list = snapshot_sub.add_parser(
        "list",
        help="List snapshots",
    )
    snapshot_list.add_argument(
        "--vm-id",
        default=None,
        help="Filter snapshots by source VM identifier.",
    )
    snapshot_list.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    ssh_parser = subparsers.add_parser(
        "ssh",
        help="SSH into an existing VM, auto-starting it when needed",
    )
    ssh_parser.add_argument("vm_id", help="VM identifier")
    _add_ssh_auth_args(ssh_parser)
    _add_boot_timeout_arg(ssh_parser)

    browser_parser = subparsers.add_parser(
        "browser",
        help="Manage disposable browser sessions",
    )
    browser_sub = browser_parser.add_subparsers(dest="browser_action")

    browser_start = browser_sub.add_parser(
        "start",
        help="Create and start a browser session",
    )
    browser_start.add_argument(
        "--session-id",
        default=None,
        help="Optional browser session identifier.",
    )
    browser_start.add_argument(
        "--backend",
        choices=["auto", "firecracker", "qemu"],
        default="auto",
        help="Backend override (default: auto).",
    )
    browser_start.add_argument(
        "--live",
        action="store_true",
        help="Start the browser session in live mode.",
    )
    browser_start.add_argument(
        "--profile-mode",
        choices=["ephemeral", "persistent"],
        default="ephemeral",
        help="Profile lifecycle mode (default: ephemeral).",
    )
    browser_start.add_argument(
        "--profile-id",
        default=None,
        help="Persistent profile identifier.",
    )
    browser_start.add_argument(
        "--timeout-minutes",
        type=int,
        default=30,
        help="Session TTL metadata in minutes (default: 30).",
    )
    browser_start.add_argument(
        "--viewport-width",
        type=int,
        default=1280,
        help="Browser viewport width (default: 1280).",
    )
    browser_start.add_argument(
        "--viewport-height",
        type=int,
        default=720,
        help="Browser viewport height (default: 720).",
    )
    browser_start.add_argument(
        "--memory-mib",
        type=int,
        default=2048,
        help="Guest memory in MiB (default: 2048).",
    )
    browser_start.add_argument(
        "--disk-size-mib",
        type=int,
        default=4096,
        help="Root filesystem size in MiB (default: 4096).",
    )
    browser_start.add_argument(
        "--record-video",
        action="store_true",
        help="Record live sessions inside the guest artifact directory.",
    )
    browser_start.add_argument(
        "--no-downloads",
        action="store_true",
        help="Disable writeable browser downloads inside the guest.",
    )
    browser_start.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    _add_boot_timeout_arg(browser_start)

    browser_stop = browser_sub.add_parser(
        "stop",
        help="Stop and delete a browser session",
    )
    browser_stop_target = browser_stop.add_mutually_exclusive_group(required=True)
    browser_stop_target.add_argument(
        "session_id",
        nargs="?",
        help="Browser session identifier",
    )
    browser_stop_target.add_argument(
        "--all",
        action="store_true",
        help="Stop and delete all persisted browser sessions.",
    )

    browser_list = browser_sub.add_parser(
        "list",
        help="List browser sessions",
    )
    browser_list.add_argument(
        "--status",
        choices=["created", "starting", "ready", "stopping", "error"],
        default=None,
        help="Filter sessions by browser session status.",
    )
    browser_list.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    browser_open = browser_sub.add_parser(
        "open",
        help="Open a live browser session in the local default browser",
    )
    browser_open.add_argument("session_id", help="Browser session identifier")

    browser_logs = browser_sub.add_parser(
        "logs",
        help="Print browser session logs",
    )
    browser_logs.add_argument("session_id", help="Browser session identifier")
    browser_logs.add_argument(
        "--tail",
        type=int,
        default=100,
        help="Number of log lines to tail from each source (default: 100).",
    )

    env_parser = subparsers.add_parser(
        "env",
        help="Manage environment variables on a running VM",
    )
    env_sub = env_parser.add_subparsers(dest="env_action")

    env_set = env_sub.add_parser(
        "set",
        help="Set environment variables (merges with existing)",
    )
    env_set.add_argument("vm_id", help="VM identifier")
    env_set.add_argument(
        "pairs",
        nargs="+",
        metavar="KEY=VALUE",
        help="One or more KEY=VALUE pairs",
    )
    env_set.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    _add_ssh_auth_args(env_set)

    env_unset = env_sub.add_parser(
        "unset",
        help="Remove environment variables",
    )
    env_unset.add_argument("vm_id", help="VM identifier")
    env_unset.add_argument(
        "keys",
        nargs="+",
        metavar="KEY",
        help="Variable names to remove",
    )
    env_unset.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    _add_ssh_auth_args(env_unset)

    env_list = env_sub.add_parser(
        "list",
        help="List current environment variables",
    )
    env_list.add_argument("vm_id", help="VM identifier")
    env_list.add_argument(
        "--show-values",
        action="store_true",
        help="Show values (they are masked by default).",
    )
    env_list.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    _add_ssh_auth_args(env_list)

    return parser


def _parse_env_pairs(pairs: list[str]) -> dict[str, str]:
    """Parse ``KEY=VALUE`` pairs, raising on malformed entries."""
    from smolvm.env import validate_env_key

    result: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"malformed pair (expected KEY=VALUE): {pair!r}")
        key, _, value = pair.partition("=")
        if not key:
            raise ValueError(f"empty key in pair: {pair!r}")
        try:
            validate_env_key(key)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        result[key] = value
    return result


def _error_type(exc: Exception) -> str:
    """Classify a CLI exception for JSON output."""
    if isinstance(exc, (FileNotFoundError, ImportError)):
        return "missing_dependency"
    if isinstance(exc, ValueError):
        return "invalid_input"
    return "runtime_error"


def _emit_cli_error(
    command: str,
    exit_code: int,
    exc: Exception,
    *,
    json_output: bool,
    hint: str | None = None,
) -> int:
    """Emit a CLI error in JSON or Rich form."""
    if json_output:
        emit_json(
            command,
            exit_code,
            data=None,
            error={
                "message": str(exc),
                "type": _error_type(exc),
            },
        )
    else:
        render_error(f"Error: {exc}", hint=hint)
    return exit_code


def _vm_rows(vms: Sequence[VMInfo]) -> list[VmRow]:
    """Normalize VM info objects into CLI list rows."""
    rows: list[VmRow] = []
    for vm in vms:
        network = vm.network
        rows.append(
            {
                "name": vm.vm_id,
                "status": vm.status.value,
                "pid": vm.pid,
                "ip_address": network.guest_ip if network else None,
                "ssh_port": network.ssh_host_port if network else None,
            }
        )
    return rows


def _render_list(rows: list[VmRow]) -> None:
    """Render the human-facing VM list."""
    table = Table(title="SmolVM Instances")
    table.add_column("Name")
    table.add_column("Status")
    table.add_column("PID", justify="right")
    for row in rows:
        table.add_row(
            str(row["name"]),
            Text(str(row["status"]), style=status_style(str(row["status"]))),
            str(row["pid"] or "-"),
        )

    console = console_stdout()
    console.print(table)
    console.print(f"Total: {len(rows)} VM(s).")


def _run_setup(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Handle ``smolvm setup``."""
    from smolvm.setup import SetupOptions, run_setup

    invalid_remove_runtime_flags: list[str] = []
    if args.check_only:
        invalid_remove_runtime_flags.append("--check-only")
    if args.with_docker:
        invalid_remove_runtime_flags.append("--with-docker")
    if args.no_configure_runtime:
        invalid_remove_runtime_flags.append("--no-configure-runtime")
    if args.skip_deps:
        invalid_remove_runtime_flags.append("--skip-deps")

    if args.remove_runtime_config and invalid_remove_runtime_flags:
        parser.error(
            "argument --remove-runtime-config: not allowed with "
            + ", ".join(invalid_remove_runtime_flags)
        )

    options = SetupOptions(
        check_only=args.check_only,
        with_docker=args.with_docker,
        configure_runtime=not args.no_configure_runtime,
        skip_deps=args.skip_deps,
        runtime_user=args.runtime_user,
        remove_runtime_config=args.remove_runtime_config,
    )

    try:
        return run_setup(options)
    except Exception as exc:
        return _emit_cli_error("setup", 1, exc, json_output=False)


def _run_list(*, include_all: bool, status_filter: str | None, json_output: bool) -> int:
    """Handle ``smolvm list``."""
    from smolvm.vm import SmolVMManager

    with SmolVMManager() as sdk:
        try:
            effective_status = status_filter or (None if include_all else VMState.RUNNING.value)
            state = VMState(effective_status) if effective_status else None
            vms = sdk.list_vms(status=state)
            rows = _vm_rows(vms)
            data: ListPayload = {
                "filters": {
                    "all": include_all,
                    "status": effective_status,
                },
                "vms": rows,
            }
            if json_output:
                emit_json("list", 0, data=data)
                return 0

            if not vms:
                if status_filter:
                    message = f"No VMs found with status '{status_filter}'."
                elif include_all:
                    message = "No VMs found."
                else:
                    message = "No running VMs found."
                render_empty("SmolVM Instances", message)
                return 0

            _render_list(rows)
            return 0
        except Exception as exc:
            return _emit_cli_error("list", 1, exc, json_output=json_output)



def _render_create_result(data: CreatePayload) -> None:
    """Render the human-facing create result."""
    console = console_stdout()
    vm_data = data["vm"]
    next_step = data["next"]

    console.print(
        Panel.fit(
            f"Created VM '{vm_data['name']}'.",
            title="VM Created",
            border_style="green",
        )
    )

    details = Table(title="VM Details", show_header=False)
    details.add_column("Field")
    details.add_column("Value")
    details.add_row("Name", str(vm_data["name"]))
    details.add_row(
        "Status",
        Text(str(vm_data["status"]), style=status_style(str(vm_data["status"]))),
    )
    details.add_row("OS", str(vm_data["os"]))
    details.add_row("Backend", str(vm_data["backend"]))
    details.add_row("IP Address", str(vm_data["ip_address"] or "-"))
    details.add_row(
        "SSH Port",
        str(vm_data["ssh_port"]) if vm_data["ssh_port"] is not None else "-",
    )
    console.print(details)
    console.print(f"Next: [bold]{next_step['ssh_command']}[/bold]")


def _run_create(args: argparse.Namespace) -> int:
    """Handle ``smolvm create``."""
    from smolvm.facade import SmolVM, _build_auto_config

    vm: SmolVM | None = None
    try:
        resolved_guest_os = GuestOS(args.os) if args.os is not None else GuestOS.ALPINE
        config, ssh_key_path = _build_auto_config(
            vm_name=args.name,
            os=args.os,
            backend=args.backend,
            mem_size_mib=args.memory_mib,
            disk_size_mib=args.disk_size_mib,
            ssh_key_path=None,
        )
        vm = SmolVM(config, ssh_key_path=ssh_key_path)
        vm.start(boot_timeout=args.boot_timeout)
        vm.wait_for_ssh(timeout=args.boot_timeout)

        network = vm.info.network
        data: CreatePayload = {
            "vm": {
                "name": vm.vm_id,
                "status": (
                    vm.info.status.value
                    if isinstance(vm.info.status, VMState)
                    else VMState.RUNNING.value
                ),
                "os": resolved_guest_os.value,
                "backend": vm.info.config.backend or "auto",
                "ip_address": network.guest_ip if network else None,
                "ssh_port": network.ssh_host_port if network else None,
            },
            "next": {
                "ssh_command": f"smolvm ssh {vm.vm_id}",
            },
        }

        if args.json:
            emit_json("create", 0, data=data)
        else:
            _render_create_result(data)
        return 0
    except Exception as exc:
        return _emit_cli_error("create", 1, exc, json_output=args.json)
    finally:
        if vm is not None:
            vm.close()


def _render_vm_lifecycle_result(
    data: StopPayload,
    *,
    message: str,
    title: str,
    border_style: str,
) -> None:
    """Render the human-facing VM lifecycle result."""
    console = console_stdout()
    vm_data = data["vm"]

    console.print(
        Panel.fit(
            message,
            title=title,
            border_style=border_style,
        )
    )

    details = Table(title="VM Details", show_header=False)
    details.add_column("Field")
    details.add_column("Value")
    details.add_row("Name", str(vm_data["name"]))
    details.add_row(
        "Status",
        Text(str(vm_data["status"]), style=status_style(str(vm_data["status"]))),
    )
    console.print(details)


def _vm_lifecycle_payload(vm_id: str, status: VMState) -> StopPayload:
    """Build a standard payload for VM lifecycle commands."""
    return {
        "vm": {
            "name": vm_id,
            "status": status.value,
        }
    }


def _snapshot_row(snapshot: SnapshotInfo) -> SnapshotRow:
    """Normalize SnapshotInfo into a CLI row."""
    return {
        "snapshot_id": snapshot.snapshot_id,
        "vm_id": snapshot.vm_id,
        "restored": snapshot.restored,
        "restored_vm_id": snapshot.restored_vm_id,
        "created_at": snapshot.created_at.isoformat(),
        "snapshot_path": str(snapshot.snapshot_path),
        "mem_file_path": str(snapshot.mem_file_path),
        "disk_path": str(snapshot.disk_path),
    }


def _render_snapshot_list(rows: list[SnapshotRow]) -> None:
    """Render the human-facing snapshot list."""
    table = Table(title="SmolVM Snapshots")
    table.add_column("Snapshot")
    table.add_column("VM")
    table.add_column("Restored")
    table.add_column("Restored VM")
    for row in rows:
        table.add_row(
            row["snapshot_id"],
            row["vm_id"],
            "yes" if row["restored"] else "no",
            row["restored_vm_id"] or "-",
        )

    console = console_stdout()
    console.print(table)
    console.print(f"Total: {len(rows)} snapshot(s).")


def _render_snapshot_create(snapshot: SnapshotRow) -> None:
    """Render a created snapshot."""
    console = console_stdout()
    console.print(
        Panel.fit(
            f"Created snapshot '{snapshot['snapshot_id']}' from VM '{snapshot['vm_id']}'.",
            title="Snapshot Created",
            border_style="cyan",
        )
    )


def _render_snapshot_restore(data: SnapshotRestorePayload) -> None:
    """Render a restored snapshot result."""
    console = console_stdout()
    snapshot = data["snapshot"]
    vm_data = data["vm"]
    console.print(
        Panel.fit(
            f"Restored snapshot '{snapshot['snapshot_id']}' into VM '{vm_data['name']}'.",
            title="Snapshot Restored",
            border_style="green",
        )
    )

    details = Table(title="Restore Details", show_header=False)
    details.add_column("Field")
    details.add_column("Value")
    details.add_row("VM", vm_data["name"])
    details.add_row("Status", Text(vm_data["status"], style=status_style(vm_data["status"])))
    details.add_row("IP Address", str(vm_data["ip_address"] or "-"))
    details.add_row(
        "SSH Port",
        str(vm_data["ssh_port"]) if vm_data["ssh_port"] is not None else "-",
    )
    console.print(details)


def _run_stop(args: argparse.Namespace) -> int:
    """Handle ``smolvm stop``."""
    from smolvm.facade import SmolVM

    vm: SmolVM | None = None
    try:
        vm = SmolVM.from_id(args.vm_id)
        vm.stop(timeout=args.timeout)

        data = _vm_lifecycle_payload(vm.vm_id, VMState.STOPPED)

        if args.json:
            emit_json("stop", 0, data=data)
        else:
            _render_vm_lifecycle_result(
                data,
                message=f"Stopped VM '{vm.vm_id}'.",
                title="VM Stopped",
                border_style="yellow",
            )
        return 0
    except Exception as exc:
        return _emit_cli_error("stop", 1, exc, json_output=args.json)
    finally:
        if vm is not None:
            vm.close()


def _run_pause(args: argparse.Namespace) -> int:
    """Handle ``smolvm pause``."""
    from smolvm.facade import SmolVM

    vm: SmolVM | None = None
    try:
        vm = SmolVM.from_id(args.vm_id)
        vm.pause()

        data = _vm_lifecycle_payload(vm.vm_id, VMState.PAUSED)
        if args.json:
            emit_json("pause", 0, data=data)
        else:
            _render_vm_lifecycle_result(
                data,
                message=f"Paused VM '{vm.vm_id}'.",
                title="VM Paused",
                border_style="blue",
            )
        return 0
    except Exception as exc:
        return _emit_cli_error("pause", 1, exc, json_output=args.json)
    finally:
        if vm is not None:
            vm.close()


def _run_resume(args: argparse.Namespace) -> int:
    """Handle ``smolvm resume``."""
    from smolvm.facade import SmolVM

    vm: SmolVM | None = None
    try:
        vm = SmolVM.from_id(args.vm_id)
        vm.resume()

        data = _vm_lifecycle_payload(vm.vm_id, VMState.RUNNING)
        if args.json:
            emit_json("resume", 0, data=data)
        else:
            _render_vm_lifecycle_result(
                data,
                message=f"Resumed VM '{vm.vm_id}'.",
                title="VM Resumed",
                border_style="green",
            )
        return 0
    except Exception as exc:
        return _emit_cli_error("resume", 1, exc, json_output=args.json)
    finally:
        if vm is not None:
            vm.close()


def _run_snapshot(args: argparse.Namespace) -> int:
    """Handle ``smolvm snapshot`` commands."""
    from smolvm.facade import SmolVM
    from smolvm.vm import SmolVMManager

    json_output = getattr(args, "json", False)
    command_name = f"snapshot.{args.snapshot_action}" if args.snapshot_action else "snapshot"

    if args.snapshot_action is None:
        render_error("Usage: smolvm snapshot {create,restore,delete,list} ...")
        return 2

    if args.snapshot_action == "create":
        vm: SmolVM | None = None
        try:
            vm = SmolVM.from_id(args.vm_id)
            snapshot = vm.snapshot(
                snapshot_id=args.snapshot_id,
                resume_source=args.resume_source,
            )
            row = _snapshot_row(snapshot)
            data: SnapshotPayload = {"snapshot": row}
            if json_output:
                emit_json(command_name, 0, data=data)
            else:
                _render_snapshot_create(row)
            return 0
        except Exception as exc:
            return _emit_cli_error(command_name, 1, exc, json_output=json_output)
        finally:
            if vm is not None:
                vm.close()

    if args.snapshot_action == "restore":
        vm: SmolVM | None = None
        try:
            vm = SmolVM.from_snapshot(
                args.snapshot_id,
                resume_vm=args.resume,
                force=args.force,
            )
            with SmolVMManager() as sdk:
                snapshot = sdk.get_snapshot(args.snapshot_id)
            row = _snapshot_row(snapshot)
            network = vm.info.network
            data: SnapshotRestorePayload = {
                "snapshot": row,
                "vm": {
                    "name": vm.vm_id,
                    "status": vm.status.value,
                    "ip_address": network.guest_ip if network else None,
                    "ssh_port": network.ssh_host_port if network else None,
                },
            }
            if json_output:
                emit_json(command_name, 0, data=data)
            else:
                _render_snapshot_restore(data)
            return 0
        except Exception as exc:
            return _emit_cli_error(command_name, 1, exc, json_output=json_output)
        finally:
            if vm is not None:
                vm.close()

    if args.snapshot_action == "delete":
        try:
            with SmolVMManager() as sdk:
                snapshot = sdk.get_snapshot(args.snapshot_id)
                sdk.delete_snapshot(args.snapshot_id)
            row = _snapshot_row(snapshot)
            data: SnapshotPayload = {"snapshot": row}
            if json_output:
                emit_json(command_name, 0, data=data)
            else:
                console_stdout().print(
                    Panel.fit(
                        f"Deleted snapshot '{args.snapshot_id}'.",
                        title="Snapshot Deleted",
                        border_style="yellow",
                    )
                )
            return 0
        except Exception as exc:
            return _emit_cli_error(command_name, 1, exc, json_output=json_output)

    try:
        with SmolVMManager() as sdk:
            snapshots = sdk.list_snapshots(vm_id=args.vm_id)
        rows = [_snapshot_row(snapshot) for snapshot in snapshots]
        data: SnapshotListPayload = {
            "filters": {"vm_id": args.vm_id},
            "snapshots": rows,
        }
        if json_output:
            emit_json(command_name, 0, data=data)
            return 0

        if not rows:
            if args.vm_id:
                render_empty("SmolVM Snapshots", f"No snapshots found for VM '{args.vm_id}'.")
            else:
                render_empty("SmolVM Snapshots", "No snapshots found.")
            return 0

        _render_snapshot_list(rows)
        return 0
    except Exception as exc:
        return _emit_cli_error(command_name, 1, exc, json_output=json_output)


def _render_env_change(
    *,
    title: str,
    border_style: str,
    message: str,
    rows: list[tuple[str, str]],
    show_reload_hint: bool,
) -> None:
    """Render a human-facing env set/unset result."""
    console = console_stdout()
    console.print(Panel.fit(message, title=title, border_style=border_style))
    if rows:
        table = Table(title="Environment Summary")
        table.add_column("Key")
        table.add_column("Result")
        for key, result in rows:
            table.add_row(key, result)
        console.print(table)
    if show_reload_hint:
        console.print(f"Reload existing sessions: [bold]{ENV_RELOAD_HINT}[/bold]")


def _render_env_list(vm_id: str, data: dict[str, object]) -> None:
    """Render the human-facing env list."""
    variables = data["variables"]
    assert isinstance(variables, dict)
    if not variables:
        render_empty(
            "Environment Variables",
            f"No SmolVM-managed environment variables on '{vm_id}'.",
        )
        return

    table = Table(title=f"Environment Variables for '{vm_id}'")
    table.add_column("Key")
    table.add_column("Value")
    for key, value in variables.items():
        table.add_row(key, str(value))

    console = console_stdout()
    console.print(table)
    if data["masked"]:
        console.print("Use --show-values to reveal values.")


def _run_env(args: argparse.Namespace) -> int:
    """Handle ``smolvm env set|unset|list``."""
    from smolvm.facade import SmolVM

    if args.env_action is None:
        render_error("Usage: smolvm env {set,unset,list} <vm_id> ...")
        return 2

    vm: SmolVM | None = None
    json_output = getattr(args, "json", False)
    command_name = f"env.{args.env_action}"
    try:
        parsed_env_vars: dict[str, str] | None = None
        if args.env_action == "set":
            parsed_env_vars = _parse_env_pairs(args.pairs)

        vm = SmolVM.from_id(
            args.vm_id,
            ssh_user=args.ssh_user,
            ssh_key_path=args.ssh_key,
        )

        if args.env_action == "set":
            assert parsed_env_vars is not None
            present_keys = sorted(vm.set_env_vars(parsed_env_vars))
            data = {
                "vm_id": args.vm_id,
                "requested_keys": sorted(parsed_env_vars),
                "present_keys": present_keys,
                "reload_hint": ENV_RELOAD_HINT,
            }
            if json_output:
                emit_json(command_name, 0, data=data)
            else:
                rows = [(key, "present") for key in present_keys]
                _render_env_change(
                    title="Environment Updated",
                    border_style="green",
                    message=(
                        f"Set {len(present_keys)} env var(s) on '{args.vm_id}': "
                        f"{', '.join(present_keys)}"
                    ),
                    rows=rows,
                    show_reload_hint=True,
                )
            return 0

        if args.env_action == "unset":
            removed = vm.unset_env_vars(args.keys)
            removed_keys = sorted(removed)
            missing_keys = sorted(set(args.keys) - set(removed_keys))
            data = {
                "vm_id": args.vm_id,
                "requested_keys": sorted(args.keys),
                "removed_keys": removed_keys,
                "missing_keys": missing_keys,
                "reload_hint": ENV_RELOAD_HINT,
            }
            if json_output:
                emit_json(command_name, 0, data=data)
            else:
                if removed_keys:
                    message = (
                        f"Removed {len(removed_keys)} env var(s) from '{args.vm_id}': "
                        f"{', '.join(removed_keys)}"
                    )
                    rows = [(key, "removed") for key in removed_keys] + [
                        (key, "not found") for key in missing_keys
                    ]
                    _render_env_change(
                        title="Environment Updated",
                        border_style="green",
                        message=message,
                        rows=rows,
                        show_reload_hint=True,
                    )
                else:
                    _render_env_change(
                        title="Environment Updated",
                        border_style="yellow",
                        message=(
                            f"No matching variables found on '{args.vm_id}': "
                            f"{', '.join(args.keys)}"
                        ),
                        rows=[],
                        show_reload_hint=False,
                    )
            return 0

        current = vm.list_env_vars()
        variables = {
            key: current[key] if args.show_values else "****"
            for key in sorted(current)
        }
        data = {
            "vm_id": args.vm_id,
            "masked": not args.show_values,
            "variables": variables,
        }
        if json_output:
            emit_json(command_name, 0, data=data)
        else:
            _render_env_list(args.vm_id, data)
        return 0
    except Exception as exc:
        return _emit_cli_error(command_name, 1, exc, json_output=json_output)
    finally:
        if vm is not None:
            vm.close()


def _run_ssh(args: argparse.Namespace) -> int:
    """Handle ``smolvm ssh``."""
    from smolvm.facade import SmolVM

    vm: SmolVM | None = None
    try:
        vm = SmolVM.from_id(
            args.vm_id,
            ssh_user=args.ssh_user,
            ssh_key_path=args.ssh_key,
        )

        if vm.status in {VMState.CREATED, VMState.STOPPED}:
            print(
                f"Notice: VM '{args.vm_id}' isn't running yet. "
                "SSH may take a little longer while SmolVM starts it."
            )
            vm.start(boot_timeout=args.boot_timeout)
        elif vm.status == VMState.PAUSED:
            print(f"Notice: VM '{args.vm_id}' is paused. Resuming it before attaching.")
            vm.resume()
        elif vm.status == VMState.ERROR:
            raise RuntimeError(
                f"VM '{args.vm_id}' is in error state. Recreate it or inspect the VM logs "
                "before attaching."
            )

        vm.wait_for_ssh(timeout=args.boot_timeout)
        completed = subprocess.run(vm._ssh_attach_command(), check=False)
        return completed.returncode
    except FileNotFoundError:
        return _emit_cli_error(
            "ssh",
            1,
            FileNotFoundError("ssh binary not found. Install openssh-client."),
            json_output=False,
        )
    except Exception as exc:
        return _emit_cli_error("ssh", 1, exc, json_output=False)
    finally:
        if vm is not None:
            vm.close()


def _render_ui_startup(
    host: str,
    port: int,
    dashboard_url: str,
    *,
    allow_beta: bool,
    auto_beta: bool,
) -> None:
    """Render the UI startup panel."""
    lines = [
        f"Starting SmolVM UI on http://{host}:{port} ...",
        f"Once started, open {dashboard_url} in your browser.",
    ]
    if allow_beta:
        if auto_beta:
            lines.append(
                "Using prerelease dashboard UI assets (auto-enabled for pre-release version)."
            )
        else:
            lines.append("Using prerelease dashboard UI assets (--allow-beta enabled).")
    console_stdout().print(Panel.fit("\n".join(lines), title="SmolVM UI", border_style="cyan"))


def _run_ui(host: str, port: int, allow_beta: bool) -> int:
    """Start the dashboard UI server with optional beta asset allowance."""
    try:
        uvicorn = importlib.import_module("uvicorn")
    except ImportError:
        return _emit_cli_error(
            "ui",
            1,
            ImportError("Dashboard dependencies are not installed."),
            json_output=False,
            hint="Install with: pip install 'smolvm[dashboard]'",
        )

    if port < 1 or port > 65535:
        return _emit_cli_error(
            "ui",
            2,
            ValueError(f"invalid port {port}. Expected 1-65535."),
            json_output=False,
        )

    auto_beta = not allow_beta and _current_version_is_prerelease()
    if auto_beta:
        allow_beta = True

    display_host = "localhost" if host in {"0.0.0.0", "::"} else host
    dashboard_url = f"http://{display_host}:{port}"

    previous_allow_beta = os.environ.get(DASHBOARD_ALLOW_BETA_ENV)
    previous_dashboard_url = os.environ.get(DASHBOARD_URL_ENV)

    if allow_beta:
        os.environ[DASHBOARD_ALLOW_BETA_ENV] = "1"
    os.environ[DASHBOARD_URL_ENV] = dashboard_url

    _render_ui_startup(host, port, dashboard_url, allow_beta=allow_beta, auto_beta=auto_beta)

    try:
        uvicorn.run("smolvm.dashboard.server:app", host=host, port=port)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        return _emit_cli_error(
            "ui",
            1,
            RuntimeError(f"failed to start UI: {exc}"),
            json_output=False,
        )
    finally:
        if allow_beta:
            if previous_allow_beta is None:
                os.environ.pop(DASHBOARD_ALLOW_BETA_ENV, None)
            else:
                os.environ[DASHBOARD_ALLOW_BETA_ENV] = previous_allow_beta

        if previous_dashboard_url is None:
            os.environ.pop(DASHBOARD_URL_ENV, None)
        else:
            os.environ[DASHBOARD_URL_ENV] = previous_dashboard_url


def _browser_rows(sessions: Sequence[BrowserSessionInfo]) -> list[BrowserRow]:
    """Normalize browser session info objects into CLI rows."""
    rows: list[BrowserRow] = []
    for session in sessions:
        rows.append(
            {
                "session_id": session.session_id,
                "vm_id": session.vm_id,
                "status": session.status.value,
                "cdp_url": session.cdp_url,
                "live_url": session.live_url,
                "profile_id": session.profile_id,
            }
        )
    return rows


def _render_browser_list(rows: list[BrowserRow]) -> None:
    """Render the human-facing browser session list."""
    table = Table(title="SmolVM Browser Sessions")
    table.add_column("Session")
    table.add_column("Status")
    table.add_column("VM")
    table.add_column("Live URL")
    for row in rows:
        table.add_row(
            str(row["session_id"]),
            Text(str(row["status"]), style=status_style(str(row["status"]))),
            str(row["vm_id"]),
            str(row["live_url"] or "-"),
        )

    console = console_stdout()
    console.print(table)
    console.print(f"Total: {len(rows)} session(s).")


def _run_browser(args: argparse.Namespace) -> int:
    """Handle ``smolvm browser`` commands."""
    from smolvm.browser import BrowserSession
    from smolvm.storage import StateManager
    from smolvm.types import BrowserSessionConfig
    from smolvm.vm import resolve_data_dir

    json_output = getattr(args, "json", False)
    command_name = f"browser.{args.browser_action}" if args.browser_action else "browser"

    if args.browser_action is None:
        render_error("Usage: smolvm browser {start,stop,list,open,logs} ...")
        return 2

    if args.browser_action == "start":
        session: BrowserSession | None = None
        try:
            config = BrowserSessionConfig(
                session_id=args.session_id,
                backend=args.backend,
                mode="live" if args.live else "headless",
                profile_mode=args.profile_mode,
                profile_id=args.profile_id,
                timeout_minutes=args.timeout_minutes,
                viewport_width=args.viewport_width,
                viewport_height=args.viewport_height,
                viewport={"width": args.viewport_width, "height": args.viewport_height},
                record_video=args.record_video,
                allow_downloads=not args.no_downloads,
                mem_size_mib=args.memory_mib,
                disk_size_mib=args.disk_size_mib,
            )
            session = BrowserSession(config)
            session.start(boot_timeout=args.boot_timeout)

            data: BrowserSessionPayload = {
                "session_id": session.session_id,
                "vm_id": session.vm_id,
                "status": session.status.value,
                "cdp_url": session.cdp_url,
                "live_url": session.live_url,
                "profile_id": session.info.profile_id,
                "artifacts_dir": str(session.artifacts_dir) if session.artifacts_dir else None,
            }

            if json_output:
                emit_json(command_name, 0, data=data)
            else:
                print(f"Started browser session '{session.session_id}'.")
                print(f"  VM: {session.vm_id}")
                print(f"  Mode: {config.mode}")
                print(f"  CDP URL: {session.cdp_url}")
                if session.live_url:
                    print(f"  Live URL: {session.live_url}")
                if session.artifacts_dir:
                    print(f"  Artifacts: {session.artifacts_dir}")
            return 0
        except Exception as exc:
            return _emit_cli_error(command_name, 1, exc, json_output=json_output)
        finally:
            if session is not None:
                session.close()

    if args.browser_action == "stop":
        if args.all:
            state = StateManager(resolve_data_dir() / "smolvm.db")
            try:
                sessions = state.list_browser_sessions()
            except Exception as exc:
                return _emit_cli_error(command_name, 1, exc, json_output=False)

            if not sessions:
                render_empty("SmolVM Browser Sessions", "No browser sessions found.")
                return 0

            failures: list[str] = []
            stopped_session_ids: list[str] = []
            for session_info in sessions:
                session: BrowserSession | None = None
                try:
                    session = BrowserSession.from_id(session_info.session_id)
                    session.stop()
                    stopped_session_ids.append(session_info.session_id)
                except Exception as exc:
                    failures.append(f"{session_info.session_id}: {exc}")
                finally:
                    if session is not None:
                        session.close()

            if failures:
                render_error(
                    "Failed to stop one or more browser sessions.",
                    hint="; ".join(failures),
                )
                return 1

            print(f"Stopped {len(stopped_session_ids)} browser session(s).")
            return 0

        session: BrowserSession | None = None
        try:
            assert args.session_id is not None
            session = BrowserSession.from_id(args.session_id)
            session.stop()
            print(f"Stopped browser session '{args.session_id}'.")
            return 0
        except Exception as exc:
            return _emit_cli_error(command_name, 1, exc, json_output=False)
        finally:
            if session is not None:
                session.close()

    if args.browser_action == "open":
        session: BrowserSession | None = None
        try:
            session = BrowserSession.from_id(args.session_id)
            if session.live_url is None:
                raise RuntimeError(
                    f"Browser session '{args.session_id}' does not have a live_url."
                )
            opened = session.open_live_view()
            if not opened:
                print(f"Open this URL manually: {session.live_url}")
            else:
                print(f"Opened {session.live_url}")
            return 0
        except Exception as exc:
            return _emit_cli_error(command_name, 1, exc, json_output=False)
        finally:
            if session is not None:
                session.close()

    if args.browser_action == "logs":
        session: BrowserSession | None = None
        try:
            session = BrowserSession.from_id(args.session_id)
            output = session.logs(tail=args.tail)
            if output:
                print(output)
            return 0
        except Exception as exc:
            return _emit_cli_error(command_name, 1, exc, json_output=False)
        finally:
            if session is not None:
                session.close()

    state = StateManager(resolve_data_dir() / "smolvm.db")
    try:
        status = BrowserSessionState(args.status) if args.status else None
        sessions = state.list_browser_sessions(status=status)
        rows = _browser_rows(sessions)
        data: BrowserListPayload = {
            "filters": {
                "status": args.status,
            },
            "sessions": rows,
        }
        if json_output:
            emit_json(command_name, 0, data=data)
            return 0

        if not sessions:
            render_empty("SmolVM Browser Sessions", "No browser sessions found.")
            return 0

        _render_browser_list(rows)
        return 0
    except Exception as exc:
        return _emit_cli_error(command_name, 1, exc, json_output=json_output)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for `smolvm`."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "cleanup":
        return run_cleanup(
            delete_all=args.all,
            prefix=args.prefix,
            dry_run=args.dry_run,
            json_output=args.json,
        )

    if args.command == "setup":
        return _run_setup(parser, args)

    if args.command == "list":
        return _run_list(
            include_all=args.all,
            status_filter=args.status,
            json_output=args.json,
        )

    if args.command == "create":
        return _run_create(args)

    if args.command == "stop":
        return _run_stop(args)

    if args.command == "pause":
        return _run_pause(args)

    if args.command == "resume":
        return _run_resume(args)

    if args.command == "snapshot":
        return _run_snapshot(args)

    if args.command == "ssh":
        return _run_ssh(args)

    if args.command == "doctor":
        return run_doctor(
            backend=args.backend,
            json_output=args.json,
            strict=args.strict,
        )

    if args.command == "ui":
        return _run_ui(host=args.host, port=args.port, allow_beta=args.allow_beta)

    if args.command == "browser":
        return _run_browser(args)

    if args.command == "env":
        return _run_env(args)

    parser.print_help()
    return 2
