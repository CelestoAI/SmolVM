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
import sys
from collections.abc import Sequence
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich.text import Text

from smolvm.cli._kvm_session import maybe_reexec_for_kvm_group
from smolvm.cli.cleanup import add_cleanup_args, add_delete_args, run_cleanup, run_delete
from smolvm.cli.output import console_stdout, emit_json, render_empty, render_error, status_style
from smolvm.cli.version_check import maybe_print_update_notice
from smolvm.host.doctor import run_doctor
from smolvm.types import BrowserSessionState, GuestOS, SnapshotType, VMState

if TYPE_CHECKING:
    from smolvm.images.published import Arch, Vmm
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
    warnings: list[str]


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
    started_at: str


class CreateNextPayload(TypedDict):
    """Suggested follow-up actions for ``smolvm create``."""

    ssh_command: str
    info_command: str


class InfoVmPayload(TypedDict):
    """Machine-readable VM details for ``smolvm info``.

    Memory and disk fields are in MiB (SmolVM's house unit, matching
    :attr:`VMConfig.memory`).
    """

    name: str
    status: str
    os: str | None
    backend: str
    ip_address: str | None
    ssh_port: int | None
    pid: int | None
    vcpus: int
    memory: int
    memory_used: int | None
    disk_size: int | None


class InfoPayload(TypedDict):
    """JSON payload for ``smolvm info``."""

    vm: InfoVmPayload


class CreatePayload(TypedDict):
    """JSON payload for ``smolvm create``."""

    vm: CreateVmPayload
    next: CreateNextPayload


class StartPresetPayload(TypedDict):
    """Preset application summary for ``smolvm <preset> start``."""

    name: str
    copied_configs: list[str]
    injected_env_keys: list[str]
    no_env_hint: str | None


class StartPayload(TypedDict):
    """JSON payload for ``smolvm <preset> start``."""

    vm: CreateVmPayload
    preset: StartPresetPayload
    next: CreateNextPayload


def _create_progress_message(backend: str, guest_os: GuestOS) -> str:
    """Return the human-facing create progress message."""
    if backend == "qemu" and guest_os == GuestOS.UBUNTU:
        return (
            "Preparing ubuntu operating system image "
            "(first run may download the kernel, initrd, and rootfs)..."
        )
    return (
        f"Preparing {guest_os.value} operating system image (first run may build or download it)..."
    )


class StopVmPayload(TypedDict):
    """Machine-readable VM details for lifecycle commands."""

    name: str
    status: str


class StopPayload(TypedDict):
    """JSON payload for VM lifecycle commands."""

    vm: StopVmPayload


class SnapshotArtifactsRow(TypedDict):
    """Machine-readable snapshot artifact paths."""

    state_path: str | None
    memory_path: str | None
    disk_path: str


class SnapshotRow(TypedDict):
    """Machine-readable data for a listed snapshot."""

    snapshot_id: str
    vm_id: str
    backend: str
    snapshot_type: str
    restored: bool
    restored_vm_id: str | None
    created_at: str
    artifacts: SnapshotArtifactsRow


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


class FileUploadPayload(TypedDict):
    """JSON payload for ``smolvm file upload``."""

    vm_id: str
    local_path: str
    guest_path: str


class FileDownloadPayload(TypedDict):
    """JSON payload for ``smolvm file download``."""

    vm_id: str
    guest_path: str
    local_path: str


class BrowserRow(TypedDict):
    """Machine-readable data for a listed browser sandbox."""

    session_id: str
    vm_id: str
    status: str
    cdp_url: str | None
    viewer_url: str | None
    display_url: str | None
    profile_id: str | None


class BrowserListFiltersPayload(TypedDict):
    """Filter metadata included with browser list output."""

    status: str | None


class BrowserListPayload(TypedDict):
    """JSON payload for ``smolvm browser list``."""

    filters: BrowserListFiltersPayload
    sessions: list[BrowserRow]


class BrowserSandboxPayload(TypedDict):
    """Machine-readable sandbox details for ``smolvm browser start``."""

    session_id: str
    vm_id: str
    status: str
    cdp_url: str | None
    viewer_url: str | None
    display_url: str | None
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


def _positive_int(value: str) -> int:
    """argparse type enforcing a strictly positive integer."""
    parsed = int(value)
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
        if platform.system() != "Linux":
            parser.error(
                f"argument {option_string}: only supported on Linux "
                "(configures Firecracker/KVM runtime). "
                f"Detected OS: {platform.system()}. "
                "Run `smolvm setup` without this flag."
            )

        if self.nargs == 0:
            setattr(namespace, self.dest, self.const if self.const is not None else True)
            return

        setattr(namespace, self.dest, values)


def _add_ssh_auth_args(command_parser: argparse.ArgumentParser) -> None:
    """Add common SSH identity arguments to a command parser."""
    command_parser.add_argument(
        "--ssh-key",
        default=None,
        help="Path to SSH private key (default: ~/.smolvm/keys/id_ed25519).",
    )
    command_parser.add_argument(
        "--ssh-user",
        default="root",
        help="SSH user (default: root).",
    )


def _add_comm_channel_arg(command_parser: argparse.ArgumentParser) -> None:
    """Add the host↔guest control-channel selector to a command parser."""
    command_parser.add_argument(
        "--comm-channel",
        choices=["ssh", "vsock"],
        default=None,
        help=(
            "Host-to-guest control channel for this command (default: auto — "
            "vsock when the guest agent answers on a QEMU/Linux host, else SSH)."
        ),
    )


def _add_boot_timeout_arg(command_parser: argparse.ArgumentParser) -> None:
    """Add a shared boot/SSH readiness timeout flag."""
    command_parser.add_argument(
        "--boot-timeout",
        type=_positive_float,
        default=30.0,
        help="Seconds to wait for the sandbox to be ready (default: 30).",
    )


def _add_preset_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Wire ``smolvm <preset> <action>`` into the CLI.

    Each agent harness is a top-level subcommand (``smolvm codex``,
    ``smolvm claude-code``) with its own action subcommands. This follows
    the project's NOUN-VERB CLI convention so future harness actions
    (``logs``, ``status``, etc.) compose naturally.
    """
    from smolvm.presets import list_presets

    for preset in list_presets():
        preset_parser = subparsers.add_parser(
            preset.name,
            aliases=list(preset.aliases),
            help=preset.summary,
            description=preset.summary,
        )
        action_sub = preset_parser.add_subparsers(
            dest="preset_action",
            metavar="ACTION",
            required=True,
        )

        start_p = action_sub.add_parser(
            "start",
            help=f"Start a sandbox preconfigured for {preset.name}.",
            description=(
                f"Boot a fresh sandbox and preinstall {preset.name}. "
                "Copies relevant host config (e.g. ~/.codex, ~/.claude) "
                "and forwards API keys from the host environment."
            ),
        )
        start_p.set_defaults(preset_name=preset.name)

        start_p.add_argument(
            "-n",
            "--name",
            help="Name for the sandbox (default: auto-generated).",
        )
        start_p.add_argument(
            "--memory",
            dest="memory_mib",
            type=int,
            default=None,
            metavar="MIB",
            help=f"Sandbox memory in MiB (default: {preset.default_mem_mib}).",
        )
        start_p.add_argument(
            "--disk-size",
            dest="disk_size_mib",
            type=int,
            default=None,
            metavar="MIB",
            help=f"Sandbox disk size in MiB (default: {preset.default_disk_mib}).",
        )
        start_p.add_argument(
            "--backend",
            choices=["auto", "firecracker", "qemu", "libkrun"],
            default=None,
            help="Virtualization backend (default: qemu, required by ubuntu).",
        )
        start_p.add_argument(
            "--os",
            choices=[guest_os.value for guest_os in GuestOS],
            default=None,
            help="Operating system image (default: ubuntu).",
        )
        start_p.add_argument(
            "--mount",
            action="append",
            default=None,
            dest="mounts",
            metavar="HOST_PATH[:GUEST_PATH]",
            help=(
                "Host directory to mount inside the sandbox. "
                "Defaults to /workspace if no guest path is given. "
                "Can be repeated."
            ),
        )
        start_p.add_argument(
            "--writable-mounts",
            action="store_true",
            dest="writable_mounts",
            help=(
                "Allow the sandbox to write back to mounted host directories. "
                "Default is read-only with a writable in-VM overlay; writes "
                "from the guest do not reach the host."
            ),
        )
        start_p.add_argument(
            "--install-timeout",
            type=_positive_float,
            default=600.0,
            help="Seconds to wait for the harness install (default: 600).",
        )
        if preset.launch_command is not None:
            attach_group = start_p.add_mutually_exclusive_group()
            attach_group.add_argument(
                "--attach",
                dest="attach",
                action="store_true",
                default=None,
                help=(
                    f"After install, ssh in and run `{preset.launch_command}` without prompting."
                ),
            )
            attach_group.add_argument(
                "--no-attach",
                dest="attach",
                action="store_false",
                help="Skip the post-install attach prompt.",
            )
        start_p.add_argument(
            "--json",
            action="store_true",
            help="Emit machine-readable JSON output.",
        )
        _add_boot_timeout_arg(start_p)


def _is_preset_command(args: argparse.Namespace) -> bool:
    """Return True when ``args`` came from ``smolvm <preset> ...``.

    Accepts canonical preset names and aliases (e.g. ``claude`` for
    ``claude-code``); argparse stores whichever spelling the user typed
    in ``args.command``.
    """
    from smolvm.presets import preset_command_names

    return args.command in preset_command_names()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smolvm",
        description="Create, manage, and connect to disposable sandboxes for AI agents.",
        epilog=(
            "Most non-interactive commands support --json to emit machine-readable "
            "output for LLMs, agents, and automation."
        ),
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {importlib.metadata.version('smolvm')}",
    )
    subparsers = parser.add_subparsers(dest="command")

    delete = subparsers.add_parser(
        "delete",
        help="Delete one or more sandboxes by ID.",
    )
    add_delete_args(delete)

    cleanup = subparsers.add_parser(
        "cleanup",
        help="Delete all sandboxes.",
    )
    add_cleanup_args(cleanup)

    update = subparsers.add_parser(
        "update",
        help="Upgrade SmolVM to the latest stable release.",
    )
    update.add_argument(
        "--check",
        action="store_true",
        help="Report whether an update is available without installing it.",
    )
    update.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    prune = subparsers.add_parser(
        "prune",
        help="Remove stale image caches from older SmolVM versions.",
    )
    prune.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without deleting anything.",
    )
    prune.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    prune.add_argument(
        "--cache-dir",
        default=None,
        help=argparse.SUPPRESS,
    )

    doctor = subparsers.add_parser(
        "doctor",
        help="Run host diagnostics for the selected backend.",
    )
    doctor.add_argument(
        "--backend",
        choices=["auto", "firecracker", "qemu", "libkrun"],
        default=None,
        help="Virtualization backend to check (default: auto-detected).",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    doctor.add_argument(
        "--strict",
        action="store_true",
        help="Exit with an error if any check reports a warning.",
    )

    def _linux_only_help(text: str) -> str:
        """Return *text* on Linux; suppress the flag from ``--help`` elsewhere."""
        return text if platform.system() == "Linux" else argparse.SUPPRESS

    setup = subparsers.add_parser(
        "setup",
        help="Install or validate one-time host prerequisites.",
    )
    setup.add_argument(
        "--check-only",
        action="store_true",
        help="Check what's needed without installing anything.",
    )
    setup.add_argument(
        "--with-docker",
        action="store_true",
        help="Also install or check Docker.",
    )
    setup.add_argument(
        "--no-configure-runtime",
        action=_LinuxOnlyOption,
        nargs=0,
        const=True,
        default=False,
        help=_linux_only_help("Skip runtime permission setup (Linux only)."),
    )
    setup.add_argument(
        "--skip-deps",
        action="store_true",
        default=False,
        help="Skip installing system packages.",
    )
    setup.add_argument(
        "--runtime-user",
        action=_LinuxOnlyOption,
        default=None,
        help=_linux_only_help("User to grant runtime permissions to (Linux only)."),
    )
    setup.add_argument(
        "--remove-runtime-config",
        action=_LinuxOnlyOption,
        nargs=0,
        const=True,
        default=False,
        help=_linux_only_help("Remove previously configured runtime permissions (Linux only)."),
    )
    setup.add_argument(
        "--for-bake",
        action=_LinuxOnlyOption,
        nargs=0,
        const=True,
        default=False,
        help=_linux_only_help(
            "Bake-friendly install: skip KVM and runtime self-tests so this can run "
            "on a builder without /dev/kvm. Run 'smolvm doctor' on the runtime host."
        ),
    )
    setup.add_argument(
        "--skip-kvm-check",
        action=_LinuxOnlyOption,
        nargs=0,
        const=True,
        default=False,
        help=_linux_only_help("Do not require /dev/kvm at install time (Linux only)."),
    )
    setup.add_argument(
        "--skip-runtime-check",
        action=_LinuxOnlyOption,
        nargs=0,
        const=True,
        default=False,
        help=_linux_only_help("Skip the post-install sudoers self-test (Linux only)."),
    )
    setup.add_argument(
        "--firecracker-version",
        action=_LinuxOnlyOption,
        default=None,
        metavar="VER",
        help=_linux_only_help(
            "Pin Firecracker release tag (e.g. v1.14.1). "
            "Falls back to the built-in default (Linux only)."
        ),
    )
    setup.add_argument(
        "--assets-dir",
        action="store_true",
        help="Print the packaged setup-assets directory and exit.",
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
        help="Start the SmolVM dashboard UI server.",
    )
    _add_ui_args(ui)

    list_parser = subparsers.add_parser(
        "list",
        help="List sandboxes and their status.",
    )
    list_filters = list_parser.add_mutually_exclusive_group()
    list_filters.add_argument(
        "--all",
        action="store_true",
        help="Show all sandboxes, not just running ones.",
    )
    list_filters.add_argument(
        "--status",
        choices=[state.value for state in VMState],
        default=None,
        help="Only show sandboxes with this status.",
    )
    list_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    info_parser = subparsers.add_parser(
        "info",
        help="Show full details for a sandbox.",
    )
    info_parser.add_argument("vm_id", metavar="sandbox", help="Name or ID of the sandbox.")
    info_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    create_parser = subparsers.add_parser(
        "create",
        help="Create a sandbox and leave it running.",
    )
    create_parser.add_argument(
        "-n",
        "--name",
        help="Name for the sandbox (default: auto-generated).",
    )
    # --os and --image are not in an argparse mutex group: Windows guests
    # need BOTH (--os windows + --image /path/to/win11.qcow2). The facade
    # rejects illegal combos (e.g. --os alpine + S3 image) at runtime with
    # a clearer message than argparse can produce.
    create_parser.add_argument(
        "--os",
        choices=[guest_os.value for guest_os in GuestOS],
        default=None,
        help=(
            "Operating system image (default: auto-detected based on backend). "
            "--os windows requires --image with a local Windows qcow2 path."
        ),
    )
    create_parser.add_argument(
        "--image",
        default=None,
        help=(
            "Image URI: S3 URI (e.g. s3://bucket/path/to/image/) or a local "
            "filesystem path / file:// URI to a pre-built qcow2 (used with "
            "--os windows). Configure S3-compatible stores via "
            "SMOLVM_S3_ENDPOINT_URL, SMOLVM_S3_ACCESS_KEY_ID, "
            "SMOLVM_S3_SECRET_ACCESS_KEY env vars."
        ),
    )
    create_parser.add_argument(
        "--memory",
        dest="memory_mib",
        type=int,
        default=None,
        metavar="MIB",
        help="Sandbox memory in MiB (default: 512).",
    )
    create_parser.add_argument(
        "--disk-size",
        dest="disk_size_mib",
        type=int,
        default=None,
        metavar="MIB",
        help=(
            "Sandbox disk size in MiB. Defaults: 512 for alpine, "
            "4096 for debian/ubuntu. Minimum: 64 for alpine; 2048 for "
            "debian/ubuntu on qemu (values below 2048 are rejected)."
        ),
    )
    create_parser.add_argument(
        "--backend",
        choices=["auto", "firecracker", "qemu", "libkrun"],
        default=None,
        help="Virtualization backend (default: auto-detected).",
    )
    create_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    create_parser.add_argument(
        "--mount",
        action="append",
        default=None,
        dest="mounts",
        metavar="HOST_PATH[:GUEST_PATH]",
        help=(
            "Host directory to mount inside the sandbox. "
            "Defaults to /workspace if no guest path is given. "
            "The host directory stays read-only by default; writes go "
            "to an overlay and do not affect the host. "
            "Pass --writable-mounts to allow guest writes to reach the host. "
            "Can be repeated for multiple mounts."
        ),
    )
    create_parser.add_argument(
        "--writable-mounts",
        action="store_true",
        dest="writable_mounts",
        help=(
            "Allow the sandbox to write back to mounted host directories. "
            "Default is read-only with a writable in-VM overlay; writes "
            "from the guest do not reach the host."
        ),
    )
    _add_boot_timeout_arg(create_parser)

    _add_preset_parsers(subparsers)

    stop_parser = subparsers.add_parser(
        "stop",
        help="Stop a running sandbox.",
    )
    stop_parser.add_argument("vm_id", metavar="sandbox", help="Name or ID of the sandbox.")
    stop_parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=3.0,
        help="Seconds to wait before forcing shutdown (default: 3).",
    )
    stop_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    pause_parser = subparsers.add_parser(
        "pause",
        help="Pause a running sandbox.",
    )
    pause_parser.add_argument("vm_id", metavar="sandbox", help="Name or ID of the sandbox.")
    pause_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    resume_parser = subparsers.add_parser(
        "resume",
        help="Resume a paused sandbox.",
    )
    resume_parser.add_argument("vm_id", metavar="sandbox", help="Name or ID of the sandbox.")
    resume_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    start_parser = subparsers.add_parser(
        "start",
        help="Start a stopped sandbox.",
    )
    start_parser.add_argument("vm_id", metavar="sandbox", help="Name or ID of the sandbox.")
    _add_boot_timeout_arg(start_parser)
    start_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    snapshot_parser = subparsers.add_parser(
        "snapshot",
        help="Save and restore sandbox state.",
    )
    snapshot_sub = snapshot_parser.add_subparsers(dest="snapshot_action")

    snapshot_create = snapshot_sub.add_parser(
        "create",
        help="Create a full snapshot for a sandbox.",
    )
    snapshot_create.add_argument("vm_id", metavar="sandbox", help="Name or ID of the sandbox.")
    snapshot_create.add_argument(
        "--snapshot-id",
        default=None,
        help="Custom snapshot name (default: auto-generated).",
    )
    snapshot_create.add_argument(
        "--snapshot-type",
        choices=[t.value for t in SnapshotType],
        default=SnapshotType.FULL.value,
        help=(
            "How much disk to store. 'full' (default) is a self-contained copy "
            "that always restores on its own. 'diff' stores only what changed "
            "since the base image to save space, but needs that base image to "
            "stay present."
        ),
    )
    snapshot_create.add_argument(
        "--resume-source",
        action="store_true",
        help="Keep the sandbox running after the snapshot is taken.",
    )
    snapshot_create.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    snapshot_restore = snapshot_sub.add_parser(
        "restore",
        help="Restore a snapshot back into its original sandbox.",
    )
    snapshot_restore.add_argument(
        "snapshot_id", metavar="snapshot", help="Name or ID of the snapshot."
    )
    snapshot_restore.add_argument(
        "--resume",
        action="store_true",
        help="Resume the restored VM immediately.",
    )
    snapshot_restore.add_argument(
        "--force",
        action="store_true",
        help="Allow restoring a snapshot that was already restored once.",
    )
    snapshot_restore.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    snapshot_delete = snapshot_sub.add_parser(
        "delete",
        help="Delete a snapshot and its files.",
    )
    snapshot_delete.add_argument(
        "snapshot_id", metavar="snapshot", help="Name or ID of the snapshot."
    )
    snapshot_delete.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    snapshot_list = snapshot_sub.add_parser(
        "list",
        help="List snapshots.",
    )
    snapshot_list.add_argument(
        "--vm-id",
        default=None,
        help="Only show snapshots from this sandbox.",
    )
    snapshot_list.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    ssh_parser = subparsers.add_parser(
        "ssh",
        help="Open a shell in a sandbox (starts it if stopped).",
    )
    ssh_parser.add_argument("vm_id", metavar="sandbox", help="Name or ID of the sandbox.")
    _add_ssh_auth_args(ssh_parser)
    _add_boot_timeout_arg(ssh_parser)

    file_parser = subparsers.add_parser(
        "file",
        help="Copy files into or out of a sandbox.",
    )
    file_sub = file_parser.add_subparsers(dest="file_action")

    file_upload = file_sub.add_parser(
        "upload",
        help="Upload one local file into a running sandbox.",
    )
    file_upload.add_argument("vm_id", metavar="sandbox", help="Name or ID of the sandbox.")
    file_upload.add_argument("local_path", metavar="local-path", help="File on this machine.")
    file_upload.add_argument(
        "guest_path",
        metavar="guest-path",
        help="Destination path in the sandbox.",
    )
    file_upload.add_argument(
        "--no-create-dirs",
        action="store_true",
        help="Do not create the destination directory in the sandbox.",
    )
    file_upload.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    _add_ssh_auth_args(file_upload)
    _add_comm_channel_arg(file_upload)

    file_download = file_sub.add_parser(
        "download",
        help="Download one file from a running sandbox.",
    )
    file_download.add_argument("vm_id", metavar="sandbox", help="Name or ID of the sandbox.")
    file_download.add_argument(
        "guest_path",
        metavar="guest-path",
        help="File path in the sandbox.",
    )
    file_download.add_argument(
        "local_path",
        metavar="local-path",
        help="Destination path on this machine.",
    )
    file_download.add_argument(
        "--no-create-dirs",
        action="store_true",
        help="Do not create the destination directory on this machine.",
    )
    file_download.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    _add_ssh_auth_args(file_download)
    _add_comm_channel_arg(file_download)

    # ── windows: image-building helpers ────────────────────────────
    windows_parser = subparsers.add_parser(
        "windows",
        help="Windows-guest helpers (image building, etc.).",
    )
    windows_sub = windows_parser.add_subparsers(dest="windows_action")

    windows_build = windows_sub.add_parser(
        "build-image",
        help=(
            "Build a reusable Windows qcow2 from a Windows ISO via "
            "unattended install (no clicks required)."
        ),
    )
    windows_build.add_argument(
        "--iso",
        dest="windows_iso",
        required=True,
        metavar="PATH",
        help="Path to the Windows ISO (e.g. ./Win11.iso).",
    )
    windows_build.add_argument(
        "--virtio-win-iso",
        dest="virtio_win_iso",
        required=True,
        metavar="PATH",
        help=(
            "Path to the virtio-win driver ISO. Download from "
            "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/"
            "stable-virtio/virtio-win.iso (one-time, ~750 MiB)."
        ),
    )
    windows_build.add_argument(
        "--output",
        dest="output_qcow2",
        required=True,
        metavar="PATH",
        help="Where to write the built qcow2 (e.g. ~/.smolvm/images/win11.qcow2).",
    )
    windows_build.add_argument(
        "--username",
        default="smolvm",
        help="Local admin account name to create (default: smolvm).",
    )
    windows_build.add_argument(
        "--password",
        default="smolvm",
        help=(
            "Local admin account password (default: smolvm — OVERRIDE for "
            "any image you'll re-use beyond throwaway POC work)."
        ),
    )
    windows_build.add_argument(
        "--hostname",
        default="smolvm-win",
        help="Windows computer name baked into the install (default: smolvm-win).",
    )
    windows_build.add_argument(
        "--edition",
        default="Windows 11 Pro",
        help=(
            "Edition to install, as it appears in install.wim "
            "(default: 'Windows 11 Pro'; 'Windows 11 Home' also valid)."
        ),
    )
    windows_build.add_argument(
        "--disk-size",
        dest="disk_size_mib",
        type=_positive_int,
        default=64 * 1024,
        metavar="MIB",
        help="Virtual size of the built qcow2 in MiB (default: 65536 = 64 GiB).",
    )
    windows_build.add_argument(
        "--build-timeout",
        dest="build_timeout_s",
        type=_positive_float,
        default=45 * 60,
        metavar="SECONDS",
        help="Upper bound on the install duration (default: 2700 = 45 min).",
    )
    windows_build.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    browser_parser = subparsers.add_parser(
        "browser",
        help="Manage disposable browser sandboxes.",
    )
    browser_sub = browser_parser.add_subparsers(dest="browser_action")

    browser_start = browser_sub.add_parser(
        "start",
        help="Create and start a browser sandbox.",
    )
    browser_start.add_argument(
        "--session-id",
        default=None,
        help="Custom session ID (default: auto-generated).",
    )
    browser_start.add_argument(
        "--backend",
        choices=["auto", "firecracker", "qemu", "libkrun"],
        default="auto",
        help="Virtualization backend (default: auto-detected).",
    )
    browser_start.add_argument(
        "--live",
        action="store_true",
        help="Expose viewer_url and display_url so you can watch and control the browser.",
    )
    browser_start.add_argument(
        "--profile-mode",
        choices=["ephemeral", "persistent"],
        default="ephemeral",
        help="Browser profile mode: ephemeral (fresh each time) or persistent (default: ephemeral).",
    )
    browser_start.add_argument(
        "--profile-id",
        default=None,
        help="Reuse a saved browser profile by its ID.",
    )
    browser_start.add_argument(
        "--timeout-minutes",
        type=int,
        default=30,
        help="Auto-stop the sandbox after this many minutes (default: 30).",
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
        "--memory",
        dest="memory_mib",
        type=int,
        default=2048,
        metavar="MIB",
        help="Sandbox memory in MiB (default: 2048).",
    )
    browser_start.add_argument(
        "--disk-size",
        dest="disk_size_mib",
        type=int,
        default=4096,
        metavar="MIB",
        help="Sandbox disk size in MiB (default: 4096).",
    )
    browser_start.add_argument(
        "--record-video",
        action="store_true",
        help="Record a video of the browser sandbox.",
    )
    browser_start.add_argument(
        "--no-downloads",
        action="store_true",
        help="Prevent the browser from saving downloaded files.",
    )
    browser_start.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    _add_boot_timeout_arg(browser_start)

    browser_stop = browser_sub.add_parser(
        "stop",
        help="Stop and delete a browser sandbox.",
    )
    browser_stop_target = browser_stop.add_mutually_exclusive_group(required=True)
    browser_stop_target.add_argument(
        "session_id",
        nargs="?",
        metavar="sandbox",
        help="Sandbox ID (printed by 'browser start').",
    )
    browser_stop_target.add_argument(
        "--all",
        action="store_true",
        help="Stop all browser sandboxes.",
    )

    browser_list = browser_sub.add_parser(
        "list",
        help="List browser sandboxes.",
    )
    browser_list.add_argument(
        "--status",
        choices=["created", "starting", "ready", "stopping", "error"],
        default=None,
        help="Only show sandboxes with this status.",
    )
    browser_list.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )

    browser_open = browser_sub.add_parser(
        "open",
        help="Open a browser sandbox viewer in your default browser.",
    )
    browser_open.add_argument(
        "session_id", metavar="sandbox", help="Sandbox ID (printed by 'browser start')."
    )

    browser_logs = browser_sub.add_parser(
        "logs",
        help="Print browser sandbox logs.",
    )
    browser_logs.add_argument(
        "session_id", metavar="sandbox", help="Sandbox ID (printed by 'browser start')."
    )
    browser_logs.add_argument(
        "--tail",
        type=int,
        default=100,
        help="Number of recent log lines to show (default: 100).",
    )

    env_parser = subparsers.add_parser(
        "env",
        help="Manage environment variables on a running sandbox.",
    )
    env_sub = env_parser.add_subparsers(dest="env_action")

    env_set = env_sub.add_parser(
        "set",
        help="Set environment variables (merges with existing).",
    )
    env_set.add_argument("vm_id", metavar="sandbox", help="Name or ID of the sandbox.")
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
    _add_comm_channel_arg(env_set)

    env_unset = env_sub.add_parser(
        "unset",
        help="Remove environment variables.",
    )
    env_unset.add_argument("vm_id", metavar="sandbox", help="Name or ID of the sandbox.")
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
    _add_comm_channel_arg(env_unset)

    env_list = env_sub.add_parser(
        "list",
        help="List current environment variables.",
    )
    env_list.add_argument("vm_id", metavar="sandbox", help="Name or ID of the sandbox.")
    env_list.add_argument(
        "--show-values",
        action="store_true",
        help="Show variable values (masked by default).",
    )
    env_list.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    _add_ssh_auth_args(env_list)

    port_parser = subparsers.add_parser(
        "port",
        help="Manage port forwarding for a running sandbox.",
    )
    port_sub = port_parser.add_subparsers(dest="port_action")

    port_expose = port_sub.add_parser(
        "expose",
        help="Forward a guest port to localhost.",
    )
    port_expose.add_argument("vm_id", metavar="sandbox", help="Name or ID of the same sandbox.")
    port_expose.add_argument(
        "mapping",
        metavar="[host-port:]sandbox-port",
        help="Port mapping, e.g. 8080:3000 or just 3000 to auto-select host port.",
    )
    port_expose.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    _add_ssh_auth_args(port_expose)
    _add_comm_channel_arg(port_expose)

    port_close = port_sub.add_parser(
        "close",
        help="Remove a forwarded port.",
    )
    port_close.add_argument("vm_id", metavar="sandbox", help="Name or ID of the sandbox.")
    port_close.add_argument(
        "mapping",
        metavar="host-port:sandbox-port",
        help="Mapping to remove, e.g. 8080:3000.",
    )
    port_close.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    _add_ssh_auth_args(port_close)
    _add_comm_channel_arg(port_close)

    port_list = port_sub.add_parser(
        "list",
        help="List active port forwards for a sandbox.",
    )
    port_list.add_argument("vm_id", metavar="sandbox", help="Name or ID of the sandbox.")
    port_list.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    _add_ssh_auth_args(port_list)
    _add_comm_channel_arg(port_list)

    _add_comm_channel_arg(env_list)

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


def _vm_warnings(vm: VMInfo) -> list[str]:
    """Collect human-facing warnings about a VM's persisted config.

    Today this only covers stale workspace-mount host paths. The
    message is one short sentence that names the missing folder and
    the recovery commands; it deliberately makes no claim about
    consequences (e.g. "cannot restart") because those are either
    false (running sandbox) or irrelevant to the user's intent.
    """
    warnings: list[str] = []
    for mount in vm.config.workspace_mounts:
        if not mount.host_path.exists():
            warnings.append(
                f"Shared folder is missing on your machine: "
                f"'{mount.host_path}'. Restore it, or run "
                f"'smolvm delete {vm.vm_id}' to remove the sandbox."
            )
    return warnings


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
                "warnings": _vm_warnings(vm),
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
        name = str(row["name"])
        if row["warnings"]:
            name = f"⚠ {name}"
        table.add_row(
            name,
            Text(str(row["status"]), style=status_style(str(row["status"]))),
            str(row["pid"] or "-"),
        )

    console = console_stdout()
    console.print(table)
    console.print(f"Total: {len(rows)} VM(s).")

    flagged = [row for row in rows if row["warnings"]]
    if flagged:
        console.print()
        console.print(Text("Warnings:", style="bold yellow"))
        for row in flagged:
            for warning in row["warnings"]:
                console.print(f"  • {warning}")


def _run_setup(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Handle ``smolvm setup``."""
    from smolvm.host.setup import SetupOptions, packaged_asset_root, run_setup

    if args.assets_dir:
        print(packaged_asset_root())
        return 0

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
        for_bake=args.for_bake,
        skip_kvm_check=args.skip_kvm_check,
        skip_runtime_check=args.skip_runtime_check,
        firecracker_version=args.firecracker_version,
    )

    if options.for_bake:
        console_stdout().print(
            "[yellow]ℹ️  --for-bake skips KVM and runtime self-tests. "
            "Run 'smolvm doctor' on the runtime host before booting VMs.[/yellow]"
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
            # Cheap per-row liveness check: a VM marked RUNNING/PAUSED whose
            # QEMU process is gone gets demoted to ERROR right here, so the
            # rendered table reflects reality instead of a stale DB row.
            vms = [sdk.refresh_status(vm) for vm in vms]
            if state is not None:
                vms = [vm for vm in vms if vm.status == state]
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


_OS_HINT_KEYWORDS = ("ubuntu", "alpine")


def _guess_os_from_paths(vm: VMInfo) -> str | None:
    """Best-effort guess of the guest OS from cached image paths.

    The per-VM rootfs clone lives under ``data_dir/disks/`` so it carries no
    OS hint, but ``kernel_path`` (kernel-boot images) and the original rootfs
    cache directory still embed the OS name. Returns ``None`` when no known
    OS keyword is found.
    """
    candidates: list[str] = []
    config = vm.config
    if config.kernel_path is not None:
        candidates.append(str(config.kernel_path))
    if config.initrd_path is not None:
        candidates.append(str(config.initrd_path))
    candidates.append(str(config.rootfs_path))
    for path in candidates:
        lowered = path.lower()
        for keyword in _OS_HINT_KEYWORDS:
            if keyword in lowered:
                return keyword
    return None


def _query_live_vm_info(vm: VMInfo) -> dict[str, object]:
    """Query a running VM via SSH for OS pretty-name and used memory.

    Connects directly with a short ``connect_timeout`` so half-dead VMs
    (status=running in state but SSH unreachable) fail fast instead of
    blocking on the SmolVM facade's 30-second SSH-ready wait.

    Returns an empty dict on any failure so the caller can render fall-through
    placeholders without surfacing transient SSH errors to the user.
    """
    from smolvm.ssh import SSHClient

    network = vm.network
    if network is None:
        return {}

    if network.ssh_host_port is not None:
        host, port = "127.0.0.1", network.ssh_host_port
    else:
        host, port = network.guest_ip, 22

    key_path = Path.home() / ".smolvm" / "keys" / "id_ed25519"
    client = SSHClient(
        host=host,
        port=port,
        key_path=str(key_path) if key_path.exists() else None,
        connect_timeout=3,
    )

    cmd = (
        "(. /etc/os-release 2>/dev/null && printf '%s' \"${PRETTY_NAME:-}\"); "
        "printf '\\n---\\n'; "
        "free -m 2>/dev/null | awk 'NR==2 {print $3}'"
    )
    try:
        result = client.run(cmd, timeout=5, shell="raw")
    except Exception:  # noqa: BLE001 - any SSH failure means "skip live data"
        return {}
    finally:
        # Best-effort cleanup; a failed close on a dead transport is harmless.
        with suppress(Exception):
            client.close()

    if result.exit_code != 0:
        return {}
    parts = result.stdout.split("---")
    out: dict[str, object] = {}
    if parts and parts[0].strip():
        out["os"] = parts[0].strip()
    if len(parts) > 1:
        try:
            out["memory_used"] = int(parts[1].strip())
        except ValueError:
            # `free -m` output unparseable on this guest (busybox variant,
            # locale, etc.) — omit the field rather than fail the whole probe.
            pass
    return out


def _disk_size_mib(rootfs_path: Path | None) -> int | None:
    """Return the rootfs disk size visible to the guest, in MiB.

    For qcow2 images, the host file footprint can be far smaller than the
    guest-visible disk because of qcow2's sparse/copy-on-write semantics, so
    we shell out to ``qemu-img`` for the virtual size. Other image formats
    (raw, ext4) match the host file size, where ``stat`` is sufficient.
    """
    if rootfs_path is None:
        return None
    if rootfs_path.suffix.lower() == ".qcow2":
        from smolvm.facade import _qcow2_virtual_size_mib

        try:
            return _qcow2_virtual_size_mib(rootfs_path)
        except Exception:  # noqa: BLE001 - qemu-img missing or image unreadable
            pass
    try:
        return rootfs_path.stat().st_size // (1024 * 1024)
    except OSError:
        return None


def _info_payload(vm: VMInfo, *, live_data: dict[str, object] | None = None) -> InfoPayload:
    """Build the info command payload from a VMInfo plus optional live data."""
    network = vm.network
    config = vm.config
    live = live_data or {}

    disk_size = _disk_size_mib(config.rootfs_path)
    os_value = live.get("os") or _guess_os_from_paths(vm)
    memory_used = live.get("memory_used")

    return {
        "vm": {
            "name": vm.vm_id,
            "status": vm.status.value,
            "os": str(os_value) if os_value else None,
            "backend": config.backend or "auto",
            "ip_address": network.guest_ip if network else None,
            "ssh_port": network.ssh_host_port if network else None,
            "pid": vm.pid,
            "vcpus": config.vcpu_count,
            "memory": config.memory,
            "memory_used": memory_used if isinstance(memory_used, int) else None,
            "disk_size": disk_size,
        }
    }


def _render_info_result(data: InfoPayload) -> None:
    """Render the human-facing info result."""
    console = console_stdout()
    vm_data = data["vm"]

    if vm_data["memory_used"] is not None:
        memory_str = f"{vm_data['memory_used']} / {vm_data['memory']} MiB used"
    else:
        memory_str = f"{vm_data['memory']} MiB"

    disk_str = f"{vm_data['disk_size']} MiB" if vm_data["disk_size"] is not None else "-"

    details = Table(title="VM Details", show_header=False)
    details.add_column("Field")
    details.add_column("Value")
    details.add_row("Name", str(vm_data["name"]))
    details.add_row(
        "Status",
        Text(str(vm_data["status"]), style=status_style(str(vm_data["status"]))),
    )
    details.add_row("OS", str(vm_data["os"] or "-"))
    details.add_row("Backend", str(vm_data["backend"]))
    details.add_row("IP Address", str(vm_data["ip_address"] or "-"))
    details.add_row(
        "SSH Port",
        str(vm_data["ssh_port"]) if vm_data["ssh_port"] is not None else "-",
    )
    details.add_row("CPUs", str(vm_data["vcpus"]))
    details.add_row("Memory", memory_str)
    details.add_row("Disk Size", disk_str)
    details.add_row(
        "PID",
        str(vm_data["pid"]) if vm_data["pid"] is not None else "-",
    )
    console.print(details)


def _run_info(*, vm_id: str, json_output: bool) -> int:
    """Handle ``smolvm info``."""
    from smolvm.vm import SmolVMManager

    with SmolVMManager() as sdk:
        try:
            vm = sdk.state.get_vm(vm_id)
            live_data: dict[str, object] | None = None
            if vm.status == VMState.RUNNING:
                live_data = _query_live_vm_info(vm)
            data = _info_payload(vm, live_data=live_data)
            if json_output:
                emit_json("info", 0, data=data)
            else:
                _render_info_result(data)
            return 0
        except Exception as exc:
            return _emit_cli_error("info", 1, exc, json_output=json_output)


def _format_started_at(iso_ts: str) -> str:
    """Render an ISO timestamp as ``YYYY-MM-DD HH:MM:SS UTC``."""
    dt = datetime.fromisoformat(iso_ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


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
    details.add_row("Started", _format_started_at(vm_data["started_at"]))
    console.print(details)
    console.print(f"Next: [bold]{next_step['ssh_command']}[/bold]")
    console.print(f"      [bold]{next_step['info_command']}[/bold]")


def _build_and_boot_with_progress(
    *,
    console: object,
    build_fn: object,
    boot_timeout: int,
    mounts: list[str] | None = None,
    writable_mounts: bool = False,
) -> object:
    """Build a VM config and boot it, showing a Rich progress bar.

    Displays per-file download progress when the OS image is not cached,
    then switches to a spinner while the VM boots and SSH becomes available.
    Returns a started :class:`~smolvm.facade.SmolVM` instance.
    """
    from collections.abc import Callable
    from typing import Any

    from rich.console import Console

    from smolvm.facade import SmolVM

    _console: Console = console  # type: ignore[assignment]
    _build_fn: Callable[..., Any] = build_fn  # type: ignore[assignment]

    download_tasks: dict[str, Any] = {}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeElapsedColumn(),
        console=_console,
        transient=True,
    ) as progress:

        def on_download(label: str, chunk: int, total: int | None) -> None:
            if label not in download_tasks:
                download_tasks[label] = progress.add_task(f"Downloading {label}", total=total)
            progress.update(download_tasks[label], advance=chunk)

        config, ssh_key_path = _build_fn(on_download)

        # One task that re-labels as the boot pipeline progresses
        # (boot → ssh-ready → workspace mount when --mount is set).
        # Phases come from start()/wait_for_ssh() via on_progress, so the
        # spinner reflects what's actually slow rather than parking on
        # "Starting VM..." for the full duration.
        boot_task = progress.add_task("Booting sandbox...", total=None)

        def on_phase(phase: str) -> None:
            progress.update(boot_task, description=phase)

        vm = SmolVM(
            config,
            ssh_key_path=ssh_key_path,
            mounts=mounts,
            writable_mounts=writable_mounts,
        )
        vm.start(boot_timeout=boot_timeout, on_progress=on_phase)
        # Idempotent: a no-op when start() already waited (mounts/env_vars
        # path), and the on_progress flips the label only when a real wait
        # actually happens.
        vm.wait_for_ssh(timeout=boot_timeout, on_progress=on_phase)
        progress.remove_task(boot_task)

    return vm


def _run_create(args: argparse.Namespace) -> int:
    """Handle ``smolvm create``."""
    from smolvm.facade import (
        SmolVM,
        _build_auto_config,
        _build_local_image_config,
        _build_s3_image_config,
        _default_guest_os_for_backend,
        _is_local_image,
    )
    from smolvm.runtime.backends import resolve_backend

    vm: SmolVM | None = None
    try:
        # Workspace mounts ride a virtio-9p share, which only the QEMU backend
        # exposes today. Auto-pick QEMU when the user asked for --mount but did
        # not pin a backend; an explicit --backend other than 'auto' is left
        # alone so the downstream check still catches incompatible combos.
        if args.mounts and args.backend in (None, "auto"):
            args.backend = "qemu"

        # Windows guests only boot on QEMU (firmware boot + swtpm). Auto-pick
        # QEMU when the user did not pin a backend; reject explicit
        # non-QEMU choices upfront with a one-sentence error.
        if args.os == "windows":
            if args.backend in (None, "auto"):
                args.backend = "qemu"
            elif args.backend != "qemu":
                raise ValueError(
                    f"--os windows requires --backend qemu (got --backend "
                    f"{args.backend!r}); drop --backend or pass --backend qemu."
                )

        resolved_backend = resolve_backend(args.backend)
        image_uri: str | None = getattr(args, "image", None)
        use_s3_image = image_uri is not None
        use_local_image = use_s3_image and _is_local_image(image_uri)

        # S3 images carry their own OS in the manifest, so --os would
        # conflict. Local images need --os to disambiguate, so the pair
        # is allowed there. (The argparse mutex used to enforce this
        # blanket; with local images now supported we have to gate by
        # image kind explicitly.)
        if use_s3_image and not use_local_image and args.os is not None:
            raise ValueError(
                "--image (S3) and --os are mutually exclusive; the S3 image "
                "manifest already names the OS — drop --os."
            )

        resolved_guest_os = (
            GuestOS(args.os)
            if args.os is not None
            else _default_guest_os_for_backend(resolved_backend)
        )

        # --disk-size has no effect for prebuilt S3 images (the rootfs size
        # is baked into the image). Reject it explicitly so users aren't
        # silently misled into thinking it took effect.
        if use_s3_image and args.disk_size_mib is not None:
            raise ValueError(
                "--disk-size is incompatible with --image: the disk size of "
                "an S3 image is fixed by the image itself."
            )

        # CLI default: roomier disk for ubuntu so package installs
        # and apt cache don't fill the rootfs on a basic `smolvm create`.
        if not use_s3_image and args.disk_size_mib is None and resolved_guest_os is GuestOS.UBUNTU:
            args.disk_size_mib = 4096

        if use_local_image:
            # Local image path (Windows POC). The build is a file existence
            # check — fast, no download — but we still funnel through the
            # same builder/progress shape so the JSON/non-JSON branches
            # stay parallel to the S3 path.
            if not args.json:
                console = console_stdout()
                vm = _build_and_boot_with_progress(
                    console=console,
                    build_fn=lambda on_download: _build_local_image_config(  # noqa: ARG005
                        image=image_uri,
                        os_input=args.os,
                        backend=args.backend,
                        memory=args.memory_mib,
                        ssh_key_path=None,
                        vm_name=args.name,
                    ),
                    boot_timeout=args.boot_timeout,
                    mounts=args.mounts,
                    writable_mounts=args.writable_mounts,
                )
            else:
                config, ssh_key_path = _build_local_image_config(
                    image=image_uri,
                    os_input=args.os,
                    backend=args.backend,
                    memory=args.memory_mib,
                    ssh_key_path=None,
                    vm_name=args.name,
                )
                vm = SmolVM(
                    config,
                    ssh_key_path=ssh_key_path,
                    mounts=args.mounts,
                    writable_mounts=args.writable_mounts,
                )
                vm.start(boot_timeout=args.boot_timeout)
                vm.wait_for_ssh(timeout=args.boot_timeout)
        elif use_s3_image:
            # S3 image path
            if not args.json:
                console = console_stdout()
                vm = _build_and_boot_with_progress(
                    console=console,
                    build_fn=lambda on_download: _build_s3_image_config(
                        image=image_uri,
                        vm_name=args.name,
                        backend=args.backend,
                        memory=args.memory_mib,
                        ssh_key_path=None,
                        on_download=on_download,
                    ),
                    boot_timeout=args.boot_timeout,
                    mounts=args.mounts,
                    writable_mounts=args.writable_mounts,
                )
            else:
                config, ssh_key_path = _build_s3_image_config(
                    image=image_uri,
                    vm_name=args.name,
                    backend=args.backend,
                    memory=args.memory_mib,
                    ssh_key_path=None,
                )
                vm = SmolVM(
                    config,
                    ssh_key_path=ssh_key_path,
                    mounts=args.mounts,
                    writable_mounts=args.writable_mounts,
                )
                vm.start(boot_timeout=args.boot_timeout)
                vm.wait_for_ssh(timeout=args.boot_timeout)
        else:
            # Standard auto-config path
            if not args.json:
                console = console_stdout()
                vm = _build_and_boot_with_progress(
                    console=console,
                    build_fn=lambda on_download: _build_auto_config(
                        vm_name=args.name,
                        os=args.os,
                        backend=args.backend,
                        memory=args.memory_mib,
                        disk_size_mib=args.disk_size_mib,
                        ssh_key_path=None,
                        on_download=on_download,
                    ),
                    boot_timeout=args.boot_timeout,
                    mounts=args.mounts,
                    writable_mounts=args.writable_mounts,
                )
            else:
                config, ssh_key_path = _build_auto_config(
                    vm_name=args.name,
                    os=args.os,
                    backend=args.backend,
                    memory=args.memory_mib,
                    disk_size_mib=args.disk_size_mib,
                    ssh_key_path=None,
                )
                vm = SmolVM(
                    config,
                    ssh_key_path=ssh_key_path,
                    mounts=args.mounts,
                    writable_mounts=args.writable_mounts,
                )
                vm.start(boot_timeout=args.boot_timeout)
                vm.wait_for_ssh(timeout=args.boot_timeout)

        os_label = "s3-image" if use_s3_image else resolved_guest_os.value
        data: CreatePayload = {
            "vm": {
                "name": vm.vm_id,
                "status": (
                    vm.info.status.value
                    if isinstance(vm.info.status, VMState)
                    else VMState.RUNNING.value
                ),
                "os": os_label,
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
            "next": {
                "ssh_command": f"smolvm ssh {vm.vm_id}",
                "info_command": f"smolvm info {vm.vm_id}",
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


def _render_start_result(data: StartPayload) -> None:
    """Render the human-facing ``smolvm <preset> start`` result."""
    console = console_stdout()
    vm_data = data["vm"]
    preset = data["preset"]
    next_step = data["next"]

    console.print(
        Panel.fit(
            f"Started '{vm_data['name']}' with [bold]{preset['name']}[/bold] preinstalled.",
            title="Sandbox Ready",
            border_style="green",
        )
    )

    details = Table(title="Sandbox Details", show_header=False)
    details.add_column("Field")
    details.add_column("Value")
    details.add_row("Name", str(vm_data["name"]))
    details.add_row(
        "Status",
        Text(str(vm_data["status"]), style=status_style(str(vm_data["status"]))),
    )
    details.add_row("OS", str(vm_data["os"]))
    details.add_row("IP Address", str(vm_data["ip_address"] or "-"))
    details.add_row(
        "SSH Port",
        str(vm_data["ssh_port"]) if vm_data["ssh_port"] is not None else "-",
    )
    details.add_row("Preset", preset["name"])
    details.add_row(
        "Configs Copied",
        ", ".join(preset["copied_configs"]) if preset["copied_configs"] else "-",
    )
    details.add_row(
        "Env Vars Forwarded",
        ", ".join(preset["injected_env_keys"]) if preset["injected_env_keys"] else "-",
    )
    console.print(details)
    if not preset["injected_env_keys"] and preset.get("no_env_hint"):
        console.print(f"\n[yellow]{preset['no_env_hint']}[/yellow]")
    console.print(f"Next: [bold]{next_step['ssh_command']}[/bold]")


# Boot args for the published-image launch path, keyed by (preset, vmm).
# Firecracker uses MMIO virtio + 8250 silenced (no PCI); QEMU/libkrun use
# PCI virtio with an arch-specific console (added by _boot_args_for).
#
# Every published preset bakes a SmolVM init script at /init that reads
# smolvm.authorized_key_b64=<base64> from the cmdline for pubkey injection
# — openclaw via build_openclaw_rootfs(), the layered presets via
# scripts/ci/preset-init.sh baked by build-preset.sh.
_PUBLISHED_BOOT_ARGS_BY_VMM: dict[Vmm, str] = {
    "firecracker": "reboot=k panic=1 pci=off init=/init 8250.nr_uarts=0",
    "qemu": "reboot=k panic=1 init=/init",
    "libkrun": "reboot=k panic=1 init=/init",
}
_PUBLISHED_IMAGE_BOOT_ARGS: dict[tuple[str, Vmm], str] = {
    (preset, vmm): args
    for preset in ("openclaw", "codex", "claude-code", "hermes", "pi")
    for vmm, args in _PUBLISHED_BOOT_ARGS_BY_VMM.items()
}

# vmm → SmolVM runtime backend. libkrun ships a Firecracker-API-compatible
# control plane; we route it through the qemu backend on macOS until the
# libkrun spike lands its own runtime path. Explicit dict (vs ternary)
# keeps that intentional aliasing visible at the call site.
_VMM_TO_BACKEND: dict[Vmm, str] = {
    "firecracker": "firecracker",
    "qemu": "qemu",
    "libkrun": "qemu",
}


def _boot_args_for(preset_name: str, vmm: Vmm, arch: Arch) -> str:
    """Resolve boot args for the published-image path.

    For QEMU/libkrun, the console driver is arch-specific: arm64's QEMU
    ``virt`` machine wires the console to a PL011 (ttyAMA0); x86's exposes
    an 8250 (ttyS0). The Firecracker base string already disables 8250 and
    relies on Firecracker's own console wiring, so no console= is needed
    there.
    """
    base = _PUBLISHED_IMAGE_BOOT_ARGS[(preset_name, vmm)]
    if vmm == "firecracker":
        return base
    console = "ttyAMA0" if arch == "arm64" else "ttyS0"
    return f"console={console} {base}"


def _vmm_for_host() -> Vmm:
    """Pick the published-image kernel variant for this host's OS.

    Linux runs Firecracker directly (KVM); macOS runs QEMU on top of
    Hypervisor.framework. ``libkrun`` is reserved for a future spike and
    intentionally not returned here yet — the CLI sticks to the two
    runtimes we ship working kernels + backends for.

    Deliberately doesn't read ``SMOLVM_BACKEND`` — the published path
    pairs a specific kernel build with a specific runtime, so an env
    override that swapped only one half would silently mismatch them.
    """
    system = platform.system()
    if system == "Linux":
        return "firecracker"
    if system == "Darwin":
        return "qemu"
    raise RuntimeError(f"Unsupported host OS for published images: {system!r}.")


def _host_arch_for_published() -> Arch:
    """Host CPU architecture in the form the manifest uses (``amd64``/``arm64``)."""
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine in {"x86_64", "amd64"}:
        return "amd64"
    raise RuntimeError(f"Unsupported host architecture for published images: {machine!r}.")


def _run_start_with_published_image(args: argparse.Namespace, preset: object) -> int:
    """Launch a sandbox using a pre-built published image.

    Bypasses the default install-at-boot flow:
    - downloads the kernel + rootfs from GitHub Releases (cached at
      ``~/.smolvm/images/<preset>-v<version>-<arch>/``)
    - boots Firecracker (matching how images are built in CI)
    - skips ``apply_preset`` since the preset's tools are already baked in
    - injects the user's pubkey via the kernel cmdline so SSH works on
      first boot

    Falls back to a clean error message when the preset has no published
    image yet (e.g. presets without bake builders).
    """
    from smolvm.exceptions import ImageError
    from smolvm.facade import SmolVM, _resolve_vm_name
    from smolvm.images.published import ensure_published_image
    from smolvm.presets._types import Preset
    from smolvm.types import VMConfig
    from smolvm.utils import ensure_ssh_key

    _preset: Preset = preset  # type: ignore[assignment]

    try:
        vmm = _vmm_for_host()
    except RuntimeError as exc:
        return _emit_cli_error("start", 2, exc, json_output=args.json)

    if (_preset.name, vmm) not in _PUBLISHED_IMAGE_BOOT_ARGS:
        return _emit_cli_error(
            "start",
            2,
            ValueError(
                f"Preset {_preset.name!r} isn't available as a prebuilt image "
                "for this platform yet."
            ),
            json_output=args.json,
        )

    requested_os = GuestOS(args.os) if args.os is not None else GuestOS.UBUNTU

    try:
        arch = _host_arch_for_published()
        private_key, public_key_path = ensure_ssh_key()
        public_key_value = public_key_path.read_text().strip()

        # Raises ImageError with a clear message if the (preset, arch, vmm,
        # os) tuple has no manifest entry — covers the gap where a host
        # platform is supported in principle but no published kernel exists
        # for it yet.
        local_image = ensure_published_image(_preset.name, arch, vmm, requested_os.value)

        backend = _VMM_TO_BACKEND[vmm]

        config = VMConfig(
            vm_id=_resolve_vm_name(args.name, prefix=_preset.name),
            memory=args.memory_mib if args.memory_mib is not None else _preset.default_mem_mib,
            kernel_path=local_image.kernel_path,
            rootfs_path=local_image.rootfs_path,
            boot_args=_boot_args_for(_preset.name, vmm, arch),
            backend=backend,
            ssh_public_key=public_key_value,
        )

        vm: SmolVM | None = None
        success = False
        try:
            vm = SmolVM(
                config,
                ssh_key_path=str(private_key),
                mounts=args.mounts,
                writable_mounts=args.writable_mounts,
            )
            vm.start(boot_timeout=args.boot_timeout)
            vm.wait_for_ssh(timeout=args.boot_timeout)

            # Transfer credentials — keychain only, no config copies or
            # install scripts. The CLI is already baked into the image;
            # we only need the OAuth token so the guest is logged in.
            from smolvm.presets import transfer_keychain_secrets

            ssh = vm._ensure_ssh_for_env()
            extracted_secrets = transfer_keychain_secrets(ssh, _preset)

            network = vm.info.network
            data: StartPayload = {
                "vm": {
                    "name": vm.vm_id,
                    "status": (
                        vm.info.status.value
                        if isinstance(vm.info.status, VMState)
                        else VMState.RUNNING.value
                    ),
                    # Mirrors the install-at-boot path so JSON callers see the
                    # same vocabulary regardless of which path served the boot.
                    "os": requested_os.value,
                    "backend": backend,
                    "ip_address": network.guest_ip if network else None,
                    "ssh_port": network.ssh_host_port if network else None,
                },
                "preset": {
                    "name": _preset.name,
                    "copied_configs": extracted_secrets,
                    "injected_env_keys": [],
                    "no_env_hint": _preset.no_env_hint,
                },
                "next": {"ssh_command": f"smolvm ssh {vm.vm_id}"},
            }
            if args.json:
                emit_json("start", 0, data=data)
            else:
                _render_start_result(data)

            success = True
            if not args.json and _preset.launch_command:
                return _maybe_attach_and_launch(vm, _preset, attach=getattr(args, "attach", None))
            return 0
        finally:
            if vm is not None:
                # On failure (e.g. wait_for_ssh timeout) the VM is unusable
                # to the caller, but the underlying QEMU/Firecracker process
                # is still alive and burning CPU. close() only releases
                # SDK handles, not the runtime — explicitly stop+delete to
                # reap the process. On success we leave the VM running so
                # the user can ssh into it.
                if not success:
                    with suppress(Exception):
                        vm.stop()
                    with suppress(Exception):
                        vm.delete()
                vm.close()
    except ImageError as exc:
        return _emit_cli_error("start", 1, exc, json_output=args.json)
    except Exception as exc:
        return _emit_cli_error("start", 1, exc, json_output=args.json)


def _run_start(args: argparse.Namespace) -> int:
    """Handle ``smolvm <preset> start``."""
    from smolvm.facade import SmolVM, _build_auto_config
    from smolvm.images.published import is_preset_published
    from smolvm.presets import apply_preset, get_preset

    preset = get_preset(args.preset_name)

    # The user-facing default is ubuntu when --os is omitted.
    requested_os = GuestOS(args.os) if args.os is not None else GuestOS.UBUNTU

    # Published-image fast path: use a pre-built image from GitHub Releases
    # if one exists for this (preset, arch, vmm, os) tuple. Falls through to
    # install-at-boot when no matching manifest entry exists — e.g. when
    # the user asks for ``--os alpine`` for a preset whose Alpine variant
    # hasn't been published yet.
    try:
        arch = _host_arch_for_published()
        vmm = _vmm_for_host()
    except RuntimeError:
        pass
    else:
        requested_backend = args.backend or "auto"
        published_backend = _VMM_TO_BACKEND[vmm]
        if requested_backend in {"auto", published_backend} and is_preset_published(
            preset.name, arch, vmm, requested_os.value
        ):
            return _run_start_with_published_image(args, preset)

    backend = args.backend or "qemu"
    if backend != "qemu":
        # Built-in presets target the ubuntu cloud image, which only boots on
        # qemu in this codebase. Fail loudly rather than silently downgrade.
        return _emit_cli_error(
            "start",
            2,
            ValueError(f"Preset {preset.name!r} requires --backend qemu (got {backend!r})."),
            json_output=args.json,
        )

    memory_mib = args.memory_mib if args.memory_mib is not None else preset.default_mem_mib
    disk_size_mib = (
        args.disk_size_mib if args.disk_size_mib is not None else preset.default_disk_mib
    )

    vm: SmolVM | None = None
    success = False
    try:
        if not args.json:
            console = console_stdout()
            vm = _build_and_boot_with_progress(
                console=console,
                build_fn=lambda on_download: _build_auto_config(
                    vm_name=args.name,
                    name_prefix=preset.name,
                    os=requested_os,
                    backend=backend,
                    memory=memory_mib,
                    disk_size_mib=disk_size_mib,
                    ssh_key_path=None,
                    on_download=on_download,
                ),
                boot_timeout=args.boot_timeout,
                mounts=args.mounts,
                writable_mounts=args.writable_mounts,
            )
            apply_summary = _apply_preset_with_progress(
                console=console,
                vm=vm,
                preset=preset,
                install_timeout=int(args.install_timeout),
            )
        else:
            config, ssh_key_path = _build_auto_config(
                vm_name=args.name,
                name_prefix=preset.name,
                os=requested_os,
                backend=backend,
                memory=memory_mib,
                disk_size_mib=disk_size_mib,
                ssh_key_path=None,
            )
            vm = SmolVM(
                config,
                ssh_key_path=ssh_key_path,
                mounts=args.mounts,
                writable_mounts=args.writable_mounts,
            )
            vm.start(boot_timeout=args.boot_timeout)
            vm.wait_for_ssh(timeout=args.boot_timeout)
            ssh = vm._ensure_ssh_for_env()
            apply_summary = apply_preset(
                ssh,
                preset,
                install_timeout=int(args.install_timeout),
            )

        network = vm.info.network
        data: StartPayload = {
            "vm": {
                "name": vm.vm_id,
                "status": (
                    vm.info.status.value
                    if isinstance(vm.info.status, VMState)
                    else VMState.RUNNING.value
                ),
                "os": requested_os.value,
                "backend": vm.info.config.backend or "auto",
                "ip_address": network.guest_ip if network else None,
                "ssh_port": network.ssh_host_port if network else None,
            },
            "preset": {
                "name": str(apply_summary["preset"]),
                "copied_configs": list(apply_summary["copied_configs"]),  # type: ignore[arg-type]
                "injected_env_keys": list(apply_summary["injected_env_keys"]),  # type: ignore[arg-type]
                "no_env_hint": preset.no_env_hint,
            },
            "next": {
                "ssh_command": f"smolvm ssh {vm.vm_id}",
            },
        }

        if args.json:
            emit_json("start", 0, data=data)
        else:
            _render_start_result(data)

        success = True
        if not args.json and preset.launch_command:
            return _maybe_attach_and_launch(vm, preset, attach=getattr(args, "attach", None))
        return 0
    except Exception as exc:
        return _emit_cli_error("start", 1, exc, json_output=args.json)
    finally:
        if vm is not None:
            if not success:
                with suppress(Exception):
                    vm.stop()
                with suppress(Exception):
                    vm.delete()
            vm.close()


def _maybe_attach_and_launch(
    vm: object,
    preset: object,
    *,
    attach: bool | None,
) -> int:
    """Maybe SSH into *vm* and exec the preset's launch command.

    *attach* tri-state: ``True`` skip prompt and attach; ``False`` skip
    everything; ``None`` (default) ask the user when stdin is a TTY.
    Returns the exit code of the SSH session, or 0 when no attach happens.
    """
    from smolvm.facade import SmolVM
    from smolvm.presets._types import Preset

    _vm: SmolVM = vm  # type: ignore[assignment]
    _preset: Preset = preset  # type: ignore[assignment]
    if _preset.launch_command is None:
        return 0

    if attach is False:
        return 0

    console = console_stdout()
    if attach is None:
        if not sys.stdin.isatty():
            return 0
        prompt = f"\nLaunch [bold]{_preset.launch_command}[/bold] in '{_vm.vm_id}' now? \\[Y/n] "
        try:
            console.print(prompt, end="")
            answer = input("").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        if answer in {"n", "no"}:
            return 0

    return _exec_launch_command(_vm, _preset.launch_command)


def _exec_launch_command(vm: object, launch_command: str) -> int:
    """SSH into *vm* with a TTY and run *launch_command* under a login shell."""
    from smolvm.env import ENV_FILE
    from smolvm.facade import SmolVM

    _vm: SmolVM = vm  # type: ignore[assignment]
    cmd = list(_vm._ssh_attach_command())
    # Insert -t before user@host so OpenSSH allocates a TTY for the remote
    # command. Source profile.d so injected env vars (API keys) are visible
    # to the harness, then exec to keep signal handling clean.
    #
    # The env file is only created when at least one host env var was
    # injected (env.inject_env_vars short-circuits on an empty mapping),
    # so it may legitimately not exist — e.g. claude-code with subscription
    # auth where ANTHROPIC_API_KEY is unset on the host. Guard the source
    # and chain with ';' so a missing file never prevents the launch.
    cmd.insert(-1, "-t")
    # Prepend ~/.local/bin to PATH so harnesses that self-migrate to a
    # native install on first run (claude-code drops a binary there via
    # its npm postinstall) are picked up without a "not in your PATH"
    # warning. SSH non-login shells skip /etc/profile, and Ubuntu's
    # default root PATH does not include ~/.local/bin.
    cmd.append(
        f"[ -r {ENV_FILE} ] && . {ENV_FILE}; "
        f'export PATH="$HOME/.local/bin:$PATH"; '
        f"exec {launch_command}"
    )
    completed = subprocess.run(cmd, check=False)
    return completed.returncode


def _apply_preset_with_progress(
    *,
    console: object,
    vm: object,
    preset: object,
    install_timeout: int,
) -> dict[str, object]:
    """Run :func:`apply_preset` with a Rich spinner showing each step."""
    from rich.console import Console

    from smolvm.facade import SmolVM
    from smolvm.presets import apply_preset
    from smolvm.presets._types import Preset

    _console: Console = console  # type: ignore[assignment]
    _vm: SmolVM = vm  # type: ignore[assignment]
    _preset: Preset = preset  # type: ignore[assignment]

    ssh = _vm._ensure_ssh_for_env()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=_console,
        transient=True,
    ) as progress:
        task = progress.add_task(f"Preparing {_preset.name}...", total=None)

        def on_progress(message: str) -> None:
            progress.update(task, description=message)

        summary = apply_preset(
            ssh,
            _preset,
            on_progress=on_progress,
            install_timeout=install_timeout,
        )
        progress.remove_task(task)

    return summary


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
        "backend": snapshot.backend,
        "snapshot_type": snapshot.snapshot_type.value,
        "restored": snapshot.restored,
        "restored_vm_id": snapshot.restored_vm_id,
        "created_at": snapshot.created_at.isoformat(),
        "artifacts": {
            "state_path": (
                str(snapshot.artifacts.state_path) if snapshot.artifacts.state_path else None
            ),
            "memory_path": (
                str(snapshot.artifacts.memory_path) if snapshot.artifacts.memory_path else None
            ),
            "disk_path": str(snapshot.artifacts.disk_path),
        },
    }


def _render_snapshot_list(rows: list[SnapshotRow]) -> None:
    """Render the human-facing snapshot list."""
    table = Table(title="SmolVM Snapshots")
    table.add_column("Snapshot")
    table.add_column("VM")
    table.add_column("Backend")
    table.add_column("Type")
    table.add_column("Restored")
    table.add_column("Restored VM")
    for row in rows:
        table.add_row(
            row["snapshot_id"],
            row["vm_id"],
            row["backend"],
            row["snapshot_type"],
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


def _run_vm_start(args: argparse.Namespace) -> int:
    """Handle ``smolvm start``."""
    from smolvm.facade import SmolVM

    vm: SmolVM | None = None
    try:
        vm = SmolVM.from_id(args.vm_id)
        vm.start(boot_timeout=args.boot_timeout)

        data = _vm_lifecycle_payload(vm.vm_id, VMState.RUNNING)
        if args.json:
            emit_json("start", 0, data=data)
        else:
            _render_vm_lifecycle_result(
                data,
                message=f"Started VM '{vm.vm_id}'.",
                title="VM Started",
                border_style="green",
            )
        return 0
    except Exception as exc:
        return _emit_cli_error("start", 1, exc, json_output=args.json)
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
                snapshot_type=args.snapshot_type,
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
            comm_channel=args.comm_channel,
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
                            f"No matching variables found on '{args.vm_id}': {', '.join(args.keys)}"
                        ),
                        rows=[],
                        show_reload_hint=False,
                    )
            return 0

        current = vm.list_env_vars()
        variables = {key: current[key] if args.show_values else "****" for key in sorted(current)}
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


def _render_file_upload(data: FileUploadPayload) -> None:
    """Render a human-facing file upload result."""
    console = console_stdout()
    console.print(
        Panel.fit(
            (f"Uploaded '{data['local_path']}' to '{data['guest_path']}' on '{data['vm_id']}'."),
            title="File Uploaded",
            border_style="green",
        )
    )


def _render_file_download(data: FileDownloadPayload) -> None:
    """Render a human-facing file download result."""
    console = console_stdout()
    console.print(
        Panel.fit(
            (
                f"Downloaded '{data['guest_path']}' from '{data['vm_id']}' "
                f"to '{data['local_path']}'."
            ),
            title="File Downloaded",
            border_style="green",
        )
    )


def _run_file(args: argparse.Namespace) -> int:
    """Handle ``smolvm file`` commands."""
    from smolvm.facade import SmolVM

    if args.file_action is None:
        render_error("Usage: smolvm file {upload,download} ...")
        return 2

    json_output = args.json
    command_name = f"file.{args.file_action}"
    vm: SmolVM | None = None
    try:
        vm = SmolVM.from_id(
            args.vm_id,
            ssh_user=args.ssh_user,
            ssh_key_path=args.ssh_key,
            comm_channel=args.comm_channel,
        )

        if args.file_action == "upload":
            guest_path = vm.upload_file(
                args.local_path,
                args.guest_path,
                make_dirs=not args.no_create_dirs,
            )
            upload_data: FileUploadPayload = {
                "vm_id": args.vm_id,
                "local_path": str(Path(args.local_path).expanduser()),
                "guest_path": guest_path,
            }
            if json_output:
                emit_json(command_name, 0, data=upload_data)
            else:
                _render_file_upload(upload_data)
            return 0

        if args.file_action == "download":
            local_path = vm.download_file(
                args.guest_path,
                args.local_path,
                make_dirs=not args.no_create_dirs,
            )
            download_data: FileDownloadPayload = {
                "vm_id": args.vm_id,
                "guest_path": args.guest_path,
                "local_path": local_path,
            }
            if json_output:
                emit_json(command_name, 0, data=download_data)
            else:
                _render_file_download(download_data)
            return 0

        render_error("Usage: smolvm file {upload,download} ...")
        return 2
    except Exception as exc:
        return _emit_cli_error(command_name, 1, exc, json_output=json_output)
    finally:
        if vm is not None:
            vm.close()


def _hint_if_vm_crashed(vm: object) -> None:
    """Print a clearer error if a non-zero ssh exit was caused by a dead VM.

    ssh's own "Connection refused" is unhelpful when the real cause is that
    the underlying QEMU process is gone but the DB still says ``running``.
    Surface the recovery command without changing the ssh exit code.
    """
    from smolvm.facade import SmolVM
    from smolvm.vm import _crashed_message

    _vm: SmolVM = vm  # type: ignore[assignment]
    try:
        refreshed = _vm._sdk.refresh_status(_vm._info)
    except Exception:
        return
    if refreshed.status == VMState.ERROR:
        render_error(_crashed_message(_vm.vm_id))


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

        console = console_stdout()
        if vm.status in {VMState.CREATED, VMState.STOPPED}:
            with console.status(f"Starting sandbox '{args.vm_id}'...", spinner="dots") as status:
                vm.start(boot_timeout=args.boot_timeout)
                status.update("Waiting for SSH...")
                vm.wait_for_ssh(timeout=args.boot_timeout)
        elif vm.status == VMState.PAUSED:
            with console.status(f"Resuming sandbox '{args.vm_id}'...", spinner="dots") as status:
                vm.resume()
                status.update("Waiting for SSH...")
                vm.wait_for_ssh(timeout=args.boot_timeout)
        elif vm.status == VMState.ERROR:
            raise RuntimeError(
                f"VM '{args.vm_id}' is in error state. Recreate it or inspect the VM logs "
                "before attaching."
            )
        else:
            # VM is already running — connect directly without probing.
            completed = subprocess.run(vm._ssh_direct_command(), check=False)
            if completed.returncode != 0:
                _hint_if_vm_crashed(vm)
            return completed.returncode
        completed = subprocess.run(vm._ssh_attach_command(), check=False)
        if completed.returncode != 0:
            _hint_if_vm_crashed(vm)
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

def _parse_port_mapping(mapping: str) -> tuple[int | None, int]:
    """Parse '[host-port:]sandbox-port' into (host_port_or_None, sandbox_port)."""
    if ":" in mapping:
        host_str, guest_str = mapping.split(":", 1)
        return int(host_str), int(guest_str)
    return None, int(mapping)

def _port_forwards_path(vm_id: str) -> Path:
    """Path to the JSON file tracking active port forwards for a VM."""
    state_dir = (Path.home() / ".smolvm" / "forwards").resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    target = (state_dir / f"{vm_id}.json").resolve()
    if not str(target).startswith(str(state_dir) + "/"):
        raise ValueError(f"Invalid sandbox name: {vm_id!r}")
    return target

def _load_port_forwards(vm_id: str) -> list[dict]:
    import json
    p = _port_forwards_path(vm_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except Exception as exc:
        raise RuntimeError(
            f"Port forward state for '{vm_id}' is unreadable. "
            f"Remove '{p}' to reset: rm '{p}'"
        ) from exc
    if not isinstance(data, list):
        raise RuntimeError(
            f"Port forward state for '{vm_id}' is corrupt. "
            f"Remove '{p}' to reset: rm '{p}'"
        )
    return data
    
def _save_port_forwards(vm_id: str, forwards: list[dict]) -> None:
    import json
    _port_forwards_path(vm_id).write_text(json.dumps(forwards, indent=2))

def _run_port_expose(args: argparse.Namespace) -> int:
    """Handle ``smolvm port expose``."""
    from smolvm.facade import SmolVM

    json_output: bool = args.json
    vm: SmolVM | None = None

    try:
        host_port_req, guest_port = _parse_port_mapping(args.mapping)
    except ValueError:
        return _emit_cli_error(
            "port expose",
            1,
            ValueError(
                f"Invalid mapping {args.mapping!r}. "
                f"Run 'smolvm port expose {args.vm_id} 8080:3000' to forward host port 8080 to sandbox port 3000, "
                f"or 'smolvm port expose {args.vm_id} 3000' to auto-select a host port."
            ),
            json_output=json_output,
        )
    try:
        vm = SmolVM.from_id(
            args.vm_id,
            ssh_user=args.ssh_user,
            ssh_key_path=args.ssh_key,
            comm_channel=args.comm_channel,
        )
        host_port = vm.expose_local(guest_port, host_port_req)

        # Persist forward info so `port close` and `port list` can find it.
        forward_entry: dict = {"host_port": host_port, "guest_port": guest_port}
        tracked = vm._local_forwards.get((host_port, guest_port))
        if tracked is not None and tracked.tunnel_proc is not None:
            forward_entry["pid"] = tracked.tunnel_proc.pid
            forward_entry["transport"] = "ssh_tunnel"
        else:
            forward_entry["transport"] = "nftables"
        forwards = _load_port_forwards(args.vm_id)
        # Remove any stale entry for the same pair before appending.
        forwards = [
            f for f in forwards
            if not (f["host_port"] == host_port and f["guest_port"] == guest_port)
        ]
        forwards.append(forward_entry)
        _save_port_forwards(args.vm_id, forwards)

        if json_output:
            emit_json(
                "port expose",
                0,
                data={
                    "sandbox": args.vm_id,
                    "guest_port": guest_port,
                    "host_port": host_port,
                    "mapping": f"{host_port}:{guest_port}",
                },
            )
        else:
            console = console_stdout()
            console.print(
                f"Exposed [bold]localhost:{host_port}[/bold] → "
                f"guest:[bold]{guest_port}[/bold]"
            )
            console.print(f"Connect to [bold]localhost:{host_port}[/bold]")
            console.print(
                f"Stop with: [bold]smolvm port close {args.vm_id} {host_port}:{guest_port}[/bold]"
            )
    except Exception as exc:
        return _emit_cli_error("port expose", 1, exc, json_output=json_output)
    finally:
        if vm is not None:
            vm.close()
    
    return 0
    

def _run_port_close(args: argparse.Namespace) -> int:
    """Handle ``smolvm port close``."""
    import signal as _signal
    from smolvm.facade import SmolVM

    json_output: bool = args.json
    vm: SmolVM | None = None

    try:
        host_port, guest_port = _parse_port_mapping(args.mapping)
        if host_port is None:
            raise ValueError(
                f"Use 'host-port:sandbox-port' format. "
                f"Run 'smolvm port list {args.vm_id}' to see active forwards."
            )
    except ValueError as exc:
        return _emit_cli_error("port close", 1, exc, json_output=json_output)

    try:
        # Kill stored SSH tunnel process if present.
        forwards = _load_port_forwards(args.vm_id)
        entry = next(
            (f for f in forwards if f["host_port"] == host_port and f["guest_port"] == guest_port),
            None,
        )
        if entry and entry.get("transport") == "ssh_tunnel" and entry.get("pid"):
            import os
            import signal
            import subprocess as _sp
            pid = entry["pid"]
            try:
                # Verify it's still our SSH tunnel before killing.
                result = _sp.run(
                    ["ps", "-p", str(pid), "-o", "comm="],
                    capture_output=True, text=True,
                )
                if "ssh" in result.stdout:
                    os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass

        # Clean up nftables rules via the facade.
        vm = SmolVM.from_id(
            args.vm_id,
            ssh_user=args.ssh_user,
            ssh_key_path=args.ssh_key,
            comm_channel=args.comm_channel,
        )
        vm.unexpose_local(host_port, guest_port)

        # Update state file.
        forwards = [
            f for f in forwards
            if not (f["host_port"] == host_port and f["guest_port"] == guest_port)
        ]
        _save_port_forwards(args.vm_id, forwards)

        if json_output:
            emit_json(
                "port close",
                0,
                data={"sandbox": args.vm_id, "host_port": host_port, "guest_port": guest_port},
            )
        else:
            console_stdout().print(
                f"Closed [bold]localhost:{host_port}[/bold] → guest:[bold]{guest_port}[/bold]"
            )

    except Exception as exc:
        return _emit_cli_error("port close", 1, exc, json_output=json_output)
    finally:
        if vm is not None:
            vm.close()

    return 0

def _run_port_list(args: argparse.Namespace) -> int:
    """Handle ``smolvm port list``."""
    json_output: bool = args.json
    forwards = _load_port_forwards(args.vm_id)

    if json_output:
        emit_json("port list", 0 , data={"sandbox": args.vm_id, "forwards": forwards})
    else:
        console = console_stdout()
        if not forwards:
            render_empty("Port Forwards", f"No active port forwards for '{args.vm_id}'.")
        else:
            from rich.table import Table
            table = Table(title=f"Port Forwards — {args.vm_id}")
            table.add_column("Host port", justify="right")
            table.add_column("Sandbox port", justify="right")
            table.add_column("Transport")
            for f in forwards:
                table.add_row(
                    str(f["host_port"]),
                    str(f["guest_port"]),
                    f.get("transport", "unknown"),
                )
            console.print(table)

    return 0

def _run_port(args: argparse.Namespace) -> int:
    """Dispatch ``smolvm port <action>``."""
    action = getattr(args, "port_action", None)
    if action == "expose":
        return _run_port_expose(args)
    if action == "close":
        return _run_port_close(args)
    if action == "list":
        return _run_port_list(args)
    from smolvm.cli.main import build_parser
    build_parser().parse_args(["port", "--help"])
    return 2

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
    """Normalize browser sandbox info objects into CLI rows."""
    rows: list[BrowserRow] = []
    for session in sessions:
        rows.append(
            {
                "session_id": session.session_id,
                "vm_id": session.vm_id,
                "status": session.status.value,
                "cdp_url": session.cdp_url,
                "viewer_url": session.live_url,
                "display_url": session.vnc_url,
                "profile_id": session.profile_id,
            }
        )
    return rows


def _render_browser_list(rows: list[BrowserRow]) -> None:
    """Render the human-facing browser sandbox list."""
    table = Table(title="SmolVM Browser Sandboxes")
    table.add_column("Sandbox")
    table.add_column("Status")
    table.add_column("VM")
    table.add_column("Viewer URL")
    table.add_column("Display URL")
    for row in rows:
        table.add_row(
            str(row["session_id"]),
            Text(str(row["status"]), style=status_style(str(row["status"]))),
            str(row["vm_id"]),
            str(row["viewer_url"] or "-"),
            str(row["display_url"] or "-"),
        )

    console = console_stdout()
    console.print(table)
    console.print(f"Total: {len(rows)} sandbox(es).")


def _run_windows(args: argparse.Namespace) -> int:
    """Handle ``smolvm windows`` commands."""
    action = getattr(args, "windows_action", None)
    if action == "build-image":
        return _run_windows_build_image(args)
    # No verb supplied — print the windows subparser help.
    print(
        "smolvm windows: choose a subcommand (e.g. `smolvm windows build-image --help`).",
        file=sys.stderr,
    )
    return 2


def _run_windows_build_image(args: argparse.Namespace) -> int:
    """Handle ``smolvm windows build-image``."""
    from smolvm.windows import WindowsImageBuilder

    try:
        builder = WindowsImageBuilder(
            windows_iso=Path(args.windows_iso),
            virtio_win_iso=Path(args.virtio_win_iso),
            output_qcow2=Path(args.output_qcow2),
            username=args.username,
            password=args.password,
            hostname=args.hostname,
            edition=args.edition,
            disk_size_mib=args.disk_size_mib,
            build_timeout_s=args.build_timeout_s,
        )
        output = builder.build()
    except Exception as exc:  # noqa: BLE001
        return _emit_cli_error("windows build-image", 1, exc, json_output=args.json)

    if args.json:
        emit_json(
            "windows build-image",
            0,
            data={
                "output_qcow2": str(output),
                "size_bytes": output.stat().st_size,
                "username": args.username,
                "hostname": args.hostname,
                "edition": args.edition,
            },
        )
    else:
        console = console_stdout()
        console.print(
            Panel.fit(
                f"Built Windows image: [bold]{output}[/bold]\n\n"
                f"Boot it with:\n"
                f"  [bold]smolvm create --os windows --image {output}[/bold]\n"
                f"or in Python:\n"
                f'  [bold]SmolVM(os="windows", image="{output}", '
                f'ssh_user="{args.username}", ssh_password="<hidden>")[/bold]',
                title="Windows image ready",
                border_style="green",
            )
        )
    return 0


def _run_browser(args: argparse.Namespace) -> int:
    """Handle ``smolvm browser`` commands."""
    from smolvm.browser import _BrowserSandbox
    from smolvm.storage import create_state_manager
    from smolvm.types import BrowserSessionConfig
    from smolvm.vm import resolve_data_dir

    json_output = getattr(args, "json", False)
    command_name = f"browser.{args.browser_action}" if args.browser_action else "browser"

    if args.browser_action is None:
        render_error("Usage: smolvm browser {start,stop,list,open,logs} ...")
        return 2

    if args.browser_action == "start":
        session: _BrowserSandbox | None = None
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
            session = _BrowserSandbox(config)
            session.start(boot_timeout=args.boot_timeout)

            data: BrowserSandboxPayload = {
                "session_id": session.session_id,
                "vm_id": session.vm_id,
                "status": session.status.value,
                "cdp_url": session.cdp_url,
                "viewer_url": session.viewer_url,
                "display_url": session.display_url,
                "profile_id": session.info.profile_id,
                "artifacts_dir": str(session.artifacts_dir) if session.artifacts_dir else None,
            }

            if json_output:
                emit_json(command_name, 0, data=data)
            else:
                print(f"Started browser sandbox '{session.session_id}'.")
                print(f"  VM: {session.vm_id}")
                print(f"  Mode: {config.mode}")
                print(f"  CDP URL: {session.cdp_url}")
                if session.viewer_url:
                    print(f"  Viewer URL: {session.viewer_url}")
                if session.display_url:
                    print(f"  Display URL: {session.display_url}")
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
            state = create_state_manager(db_path=resolve_data_dir() / "smolvm.db")
            try:
                sessions = state.list_browser_sessions()
            except Exception as exc:
                return _emit_cli_error(command_name, 1, exc, json_output=False)

            if not sessions:
                render_empty("SmolVM Browser Sandboxes", "No browser sandboxes found.")
                return 0

            failures: list[str] = []
            stopped_session_ids: list[str] = []
            for session_info in sessions:
                session: _BrowserSandbox | None = None
                try:
                    session = _BrowserSandbox.from_id(session_info.session_id)
                    session.stop()
                    stopped_session_ids.append(session_info.session_id)
                except Exception as exc:
                    failures.append(f"{session_info.session_id}: {exc}")
                finally:
                    if session is not None:
                        session.close()

            if failures:
                failed_ids = [failure.split(":", 1)[0] for failure in failures]
                commands = ", then ".join(
                    f"`smolvm browser stop {session_id}`" for session_id in failed_ids
                )
                render_error(
                    "Failed to stop browser sandbox"
                    f"{'es' if len(failed_ids) != 1 else ''} "
                    f"{', '.join(failed_ids)}; to fix, run: {commands}."
                )
                return 1

            print(f"Stopped {len(stopped_session_ids)} browser sandbox(es).")
            return 0

        session: _BrowserSandbox | None = None
        try:
            assert args.session_id is not None
            session = _BrowserSandbox.from_id(args.session_id)
            session.stop()
            print(f"Stopped browser sandbox '{args.session_id}'.")
            return 0
        except Exception:
            render_error(
                f"Failed to stop browser sandbox {args.session_id}; "
                f"to fix, run: `smolvm browser stop {args.session_id}`."
            )
            return 1
        finally:
            if session is not None:
                session.close()

    if args.browser_action == "open":
        session: _BrowserSandbox | None = None
        try:
            session = _BrowserSandbox.from_id(args.session_id)
            if session.viewer_url is None:
                raise RuntimeError(
                    f"Browser sandbox '{args.session_id}' does not have a viewer_url."
                )
            opened = session.open_viewer()
            if not opened:
                print(f"Open this URL manually: {session.viewer_url}")
            else:
                print(f"Opened {session.viewer_url}")
            return 0
        except Exception as exc:
            return _emit_cli_error(command_name, 1, exc, json_output=False)
        finally:
            if session is not None:
                session.close()

    if args.browser_action == "logs":
        session: _BrowserSandbox | None = None
        try:
            session = _BrowserSandbox.from_id(args.session_id)
            output = session.logs(tail=args.tail)
            if output:
                print(output)
            return 0
        except Exception as exc:
            return _emit_cli_error(command_name, 1, exc, json_output=False)
        finally:
            if session is not None:
                session.close()

    state = create_state_manager(db_path=resolve_data_dir() / "smolvm.db")
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
            render_empty("SmolVM Browser Sandboxes", "No browser sandboxes found.")
            return 0

        _render_browser_list(rows)
        return 0
    except Exception as exc:
        return _emit_cli_error(command_name, 1, exc, json_output=json_output)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for `smolvm`."""
    # Best-effort: if the user has been added to the kvm group but the
    # current shell session hasn't picked it up yet, re-exec under
    # `sg kvm -c …` so /dev/kvm becomes accessible without a manual
    # `newgrp kvm` or relog. Linux-only; no-op everywhere else.
    maybe_reexec_for_kvm_group(argv)

    parser = build_parser()
    args = parser.parse_args(argv)

    # Best-effort PyPI update nag. Skipped in --json mode and for
    # `setup --assets-dir` so we never pollute machine-readable output
    # that scripts (Packer, Make, etc.) consume directly.
    suppress_update_notice = bool(getattr(args, "json", False)) or (
        args.command == "setup" and bool(getattr(args, "assets_dir", False))
    )
    maybe_print_update_notice(json_output=suppress_update_notice)

    if args.command == "delete":
        return run_delete(
            vm_ids=args.vm_ids,
            dry_run=args.dry_run,
            json_output=args.json,
        )

    if args.command == "cleanup":
        return run_cleanup(
            dry_run=args.dry_run,
            json_output=args.json,
            force=args.force,
        )

    if args.command == "update":
        from smolvm.cli.update import run_update

        return run_update(args)

    if args.command == "prune":
        from smolvm.cli.prune import run_prune

        return run_prune(args)

    if args.command == "setup":
        return _run_setup(parser, args)

    if args.command == "list":
        return _run_list(
            include_all=args.all,
            status_filter=args.status,
            json_output=args.json,
        )

    if args.command == "info":
        return _run_info(vm_id=args.vm_id, json_output=args.json)

    if args.command == "create":
        return _run_create(args)

    if _is_preset_command(args):
        return _run_start(args)

    if args.command == "stop":
        return _run_stop(args)

    if args.command == "pause":
        return _run_pause(args)

    if args.command == "resume":
        return _run_resume(args)

    if args.command == "start":
        return _run_vm_start(args)

    if args.command == "snapshot":
        return _run_snapshot(args)

    if args.command == "ssh":
        return _run_ssh(args)

    if args.command == "file":
        return _run_file(args)

    if args.command == "windows":
        return _run_windows(args)

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
    
    if args.command == "port":
        return _run_port(args)
    
    parser.print_help()
    return 2
