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
from collections.abc import Sequence

from smolvm.cleanup import run_cleanup


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
    print(
        "  Note: Changes apply to new SSH sessions. "
        "In an existing session, run:"
    )
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


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for `smolvm`."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "cleanup":
        return run_cleanup(delete_all=args.all, prefix=args.prefix, dry_run=args.dry_run)

    if args.command == "env":
        return _run_env(args)

    parser.print_help()
    return 2

