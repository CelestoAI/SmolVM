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
import re
from collections.abc import Sequence

from smolvm.cleanup import run_cleanup
from smolvm.doctor import run_doctor

DASHBOARD_ALLOW_BETA_ENV = "SMOLVM_DASHBOARD_ALLOW_BETA"
DASHBOARD_URL_ENV = "SMOLVM_DASHBOARD_URL"

# Matches PEP 440 pre-release and dev-release version suffixes,
# e.g. "0.0.5.a1", "0.0.5b2", "0.0.5.dev1", "0.0.5rc1".
_PRERELEASE_RE = re.compile(r"[._]?(a|b|rc|alpha|beta|dev)\d*", re.IGNORECASE)


def _current_version_is_prerelease() -> bool:
    """Return True if the installed smolvm package version is a pre-release.

    Uses ``packaging.version.Version`` when available for accurate PEP 440
    parsing, and falls back to a regex heuristic otherwise.
    """
    try:
        ver = importlib.metadata.version("smolvm")
    except importlib.metadata.PackageNotFoundError:
        return False

    try:
        from packaging.version import InvalidVersion, Version

        return Version(ver).is_prerelease
    except (ImportError, InvalidVersion):  # packaging not installed or parse error
        return bool(_PRERELEASE_RE.search(ver))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smolvm",
        description="SmolVM command-line tools",
    )
    subparsers = parser.add_subparsers(dest="command")

    cleanup = subparsers.add_parser(
        "cleanup",
        help="Clean stale SmolVM resources",
    )
    cleanup.add_argument(
        "--all",
        action="store_true",
        help="Delete all VMs (not just stale/auto-created ones).",
    )
    cleanup.add_argument(
        "--prefix",
        default="vm-",
        help='Auto-VM prefix to clean (default: "vm-").',
    )
    cleanup.add_argument(
        "--dry-run",
        action="store_true",
        help="Print targets without deleting.",
    )

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

    # ── ui subcommand ─────────────────────────────────────────────────
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

    # ── list subcommand ───────────────────────────────────────────────
    list_parser = subparsers.add_parser(
        "list",
        help="List all SmolVMs and their status",
    )
    list_parser.add_argument(
        "--status",
        choices=["created", "running", "stopped", "error"],
        default=None,
        help="Filter VMs by status (created, running, stopped, error).",
    )

    # ── env subcommand group ──────────────────────────────────────────
    env_parser = subparsers.add_parser(
        "env",
        help="Manage environment variables on a running VM",
    )
    env_sub = env_parser.add_subparsers(dest="env_action")

    # smolvm env set <vm_id> KEY=VALUE ...
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
        "--ssh-key",
        default=None,
        help="SSH private key path (default fallback: ~/.smolvm/keys/id_ed25519).",
    )
    env_set.add_argument(
        "--ssh-user",
        default="root",
        help="SSH user (default: root).",
    )

    # smolvm env unset <vm_id> KEY ...
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
        "--ssh-key",
        default=None,
        help="SSH private key path (default fallback: ~/.smolvm/keys/id_ed25519).",
    )
    env_unset.add_argument(
        "--ssh-user",
        default="root",
        help="SSH user (default: root).",
    )

    # smolvm env list <vm_id>
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
        "--ssh-key",
        default=None,
        help="SSH private key path (default fallback: ~/.smolvm/keys/id_ed25519).",
    )
    env_list.add_argument(
        "--ssh-user",
        default="root",
        help="SSH user (default: root).",
    )

    return parser


def _parse_env_pairs(pairs: list[str]) -> dict[str, str]:
    """Parse ``KEY=VALUE`` pairs, raising on malformed entries."""
    from smolvm.env import validate_env_key

    result: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"Error: malformed pair (expected KEY=VALUE): {pair!r}")
        key, _, value = pair.partition("=")
        if not key:
            raise SystemExit(f"Error: empty key in pair: {pair!r}")
        try:
            validate_env_key(key)
        except ValueError as e:
            raise SystemExit(f"Error: {e}") from None
        result[key] = value
    return result


def _env_reload_hint() -> None:
    """Print hint about reloading env in existing sessions."""
    print("  Note: Changes apply to new SSH sessions. In an existing session, run:")
    print("    source /etc/profile.d/smolvm_env.sh")


def _run_env(args: argparse.Namespace) -> int:
    """Handle ``smolvm env set|unset|list``."""
    from smolvm.facade import SmolVM

    if args.env_action is None:
        print("Usage: smolvm env {set,unset,list} <vm_id> ...")
        return 2

    vm: SmolVM | None = None
    try:
        vm = SmolVM.from_id(
            args.vm_id,
            ssh_user=args.ssh_user,
            ssh_key_path=args.ssh_key,
        )

        if args.env_action == "set":
            env_vars = _parse_env_pairs(args.pairs)
            injected = vm.set_env_vars(env_vars)
            if injected:
                print(f"✓ Set {len(injected)} env var(s) on '{args.vm_id}': {', '.join(injected)}")
                _env_reload_hint()
            else:
                print("No variables to set.")
            return 0

        if args.env_action == "unset":
            removed = vm.unset_env_vars(args.keys)
            if removed:
                keys = ", ".join(sorted(removed))
                print(f"✓ Removed {len(removed)} env var(s) from '{args.vm_id}': {keys}")
                _env_reload_hint()
            else:
                not_found = ", ".join(args.keys)
                print(f"No matching variables found on '{args.vm_id}': {not_found}")
            return 0

        if args.env_action == "list":
            current = vm.list_env_vars()
            if not current:
                print(f"No SmolVM-managed environment variables on '{args.vm_id}'.")
                return 0
            print(f"Environment variables for '{args.vm_id}':")
            for key in sorted(current):
                if args.show_values:
                    print(f"  {key}={current[key]}")
                else:
                    print(f"  {key}=****")
            if not args.show_values:
                print("  (use --show-values to reveal)")
            return 0

        return 2

    except Exception as e:
        print(f"Error: {e}")
        return 1
    finally:
        if vm is not None:
            vm.close()


def _run_list(status_filter: str | None) -> int:
    """Handle ``smolvm list``."""
    from smolvm.types import VMState
    from smolvm.vm import SmolVMManager

    sdk = SmolVMManager()
    try:
        state = VMState(status_filter) if status_filter else None
        vms = sdk.list_vms(status=state)
    except Exception as e:
        print(f"Error: {e}")
        return 1
    finally:
        sdk.close()

    if not vms:
        if status_filter:
            print(f"No VMs found with status '{status_filter}'.")
        else:
            print("No VMs found.")
        return 0

    headers = ["VM ID", "STATUS", "IP ADDRESS", "SSH PORT", "PID"]

    rows = []
    for vm in vms:
        ip = vm.network.guest_ip if vm.network else "-"
        ssh_port = (
            str(vm.network.ssh_host_port) if (vm.network and vm.network.ssh_host_port) else "-"
        )
        pid = str(vm.pid) if vm.pid else "-"
        rows.append([vm.vm_id, vm.status.value, ip, ssh_port, pid])

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in col_widths))
    for row in rows:
        print(fmt.format(*row))

    print(f"\nTotal: {len(vms)} VM(s).")
    return 0


def _run_ui(host: str, port: int, allow_beta: bool) -> int:
    """Start the dashboard UI server with optional beta asset allowance."""
    try:
        uvicorn = importlib.import_module("uvicorn")
    except ImportError:
        print(
            "Error: Dashboard dependencies are not installed. "
            "Install with: pip install 'smolvm[dashboard]'"
        )
        return 1

    if port < 1 or port > 65535:
        print(f"Error: invalid port {port}. Expected 1-65535.")
        return 2

    # Automatically allow beta/prerelease UI assets when running a
    # pre-release version of smolvm (e.g. 0.0.5.a1, 0.0.5.dev).
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

    print(f"Starting SmolVM UI on http://{host}:{port} ...")
    print(f"Once started, open {dashboard_url} in your browser.")
    if allow_beta:
        if auto_beta:
            print(
                "Using prerelease dashboard UI assets "
                "(auto-enabled for pre-release version)."
            )
        else:
            print("Using prerelease dashboard UI assets (--allow-beta enabled).")

    try:
        uvicorn.run("smolvm.dashboard.server:app", host=host, port=port)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"Error: failed to start UI: {e}")
        return 1
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


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for `smolvm`."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "cleanup":
        return run_cleanup(delete_all=args.all, prefix=args.prefix, dry_run=args.dry_run)

    if args.command == "list":
        return _run_list(status_filter=args.status)

    if args.command == "doctor":
        return run_doctor(
            backend=args.backend,
            json_output=args.json,
            strict=args.strict,
        )

    if args.command == "ui":
        return _run_ui(host=args.host, port=args.port, allow_beta=args.allow_beta)

    if args.command == "env":
        return _run_env(args)

    parser.print_help()
    return 2
