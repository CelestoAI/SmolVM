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

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for `smolvm`."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "cleanup":
        return run_cleanup(delete_all=args.all, prefix=args.prefix, dry_run=args.dry_run)

    parser.print_help()
    return 2
