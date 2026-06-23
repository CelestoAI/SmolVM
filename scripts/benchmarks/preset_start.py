#!/usr/bin/env python3
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

"""Measure SmolVM preset startup through the public CLI."""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Any

try:
    from .reporting import (
        CommandPlan,
        cli_data,
        expected_cleanup_ok,
        finish_report,
        parse_json_output,
        print_report,
        run_command,
        start_report,
    )
except ImportError:  # pragma: no cover - script execution path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from reporting import (  # type: ignore[no-redef]
        CommandPlan,
        cli_data,
        expected_cleanup_ok,
        finish_report,
        parse_json_output,
        print_report,
        run_command,
        start_report,
    )

PRESET_COMMANDS = {
    "codex": "codex",
    "claude": "claude",
    "claude-code": "claude",
    "openclaw": "openclaw",
    "hermes": "hermes",
    "pi": "pi",
}


def build_start_command(args: argparse.Namespace, sandbox_name: str) -> list[str]:
    command = [
        "smolvm",
        PRESET_COMMANDS[args.preset],
        "start",
        "--name",
        sandbox_name,
        "--json",
        "--no-attach",
        "--boot-timeout",
        str(args.boot_timeout),
        "--install-timeout",
        str(args.install_timeout),
    ]
    if args.os:
        command.extend(["--os", args.os])
    if args.backend:
        command.extend(["--backend", args.backend])
    if args.memory_mib is not None:
        command.extend(["--memory", str(args.memory_mib)])
    if args.disk_size_mib is not None:
        command.extend(["--disk-size", str(args.disk_size_mib)])
    for mount in args.mounts:
        command.extend(["--mount", mount])
    if args.writable_mounts:
        command.append("--writable-mounts")
    return command


def run_iteration(args: argparse.Namespace, iteration: int) -> dict[str, Any]:
    sandbox_name = f"{args.name_prefix}-{args.preset.replace('-', '')}-{uuid.uuid4().hex[:8]}"
    record: dict[str, Any] = {
        "iter": iteration,
        "preset": args.preset,
        "sandbox": sandbox_name,
        "cleanup_expected": not args.keep,
    }

    start_plan = CommandPlan(
        "preset_start",
        build_start_command(args, sandbox_name),
        timeout_s=args.command_timeout,
    )
    start_record = run_command(start_plan, dry_run=args.dry_run)
    record["start"] = start_record
    if args.dry_run:
        if not args.keep:
            record["cleanup"] = run_command(
                cleanup_plan(sandbox_name, timeout_s=args.command_timeout),
                dry_run=True,
            )
        return record

    vm_name = sandbox_name
    if start_record["ok"]:
        try:
            payload = parse_json_output(start_record.get("stdout", ""))
            data = cli_data(payload)
            record["start_payload"] = payload
            vm = data.get("vm") if isinstance(data, dict) else None
            if isinstance(vm, dict) and isinstance(vm.get("name"), str):
                vm_name = vm["name"]
                record["sandbox"] = vm_name
        except Exception as exc:  # noqa: BLE001
            record["start_parse_error"] = str(exc)

    if not args.keep:
        record["cleanup"] = run_command(
            cleanup_plan(vm_name, timeout_s=args.command_timeout),
            dry_run=False,
        )
    return record


def cleanup_plan(sandbox_name: str, *, timeout_s: float | None) -> CommandPlan:
    return CommandPlan(
        "sandbox_delete",
        ["smolvm", "sandbox", "delete", sandbox_name, "--json"],
        timeout_s=timeout_s,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset", choices=sorted(PRESET_COMMANDS), default="codex")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--name-prefix", default="bench")
    parser.add_argument("--os", choices=("alpine", "ubuntu"), default=None)
    parser.add_argument(
        "--backend",
        choices=("auto", "firecracker", "qemu", "libkrun"),
        default=None,
    )
    parser.add_argument("--memory", dest="memory_mib", type=int, default=None)
    parser.add_argument("--disk-size", dest="disk_size_mib", type=int, default=None)
    parser.add_argument("--mount", dest="mounts", action="append", default=[])
    parser.add_argument("--writable-mounts", action="store_true")
    parser.add_argument("--boot-timeout", type=float, default=90.0)
    parser.add_argument("--install-timeout", type=float, default=600.0)
    parser.add_argument("--command-timeout", type=float, default=900.0)
    parser.add_argument("--keep", action="store_true", help="Leave created sandboxes running.")
    parser.add_argument("--dry-run", action="store_true", help="Print the command plan only.")
    parser.add_argument("--json", action="store_true", help="Print JSON.")
    parser.add_argument("--output", type=Path, help="Optional JSON output path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("--iterations must be >= 1")
    if args.boot_timeout <= 0:
        parser.error("--boot-timeout must be > 0")
    if args.install_timeout <= 0:
        parser.error("--install-timeout must be > 0")
    if args.command_timeout <= 0:
        parser.error("--command-timeout must be > 0")

    config = {
        "preset": args.preset,
        "iterations": args.iterations,
        "name_prefix": args.name_prefix,
        "os": args.os,
        "backend": args.backend,
        "memory_mib": args.memory_mib,
        "disk_size_mib": args.disk_size_mib,
        "mounts": args.mounts,
        "writable_mounts": args.writable_mounts,
        "boot_timeout": args.boot_timeout,
        "install_timeout": args.install_timeout,
        "command_timeout": args.command_timeout,
        "keep": args.keep,
    }
    report, started = start_report("preset_start", config=config, dry_run=args.dry_run)
    report["records"] = [run_iteration(args, iteration) for iteration in range(args.iterations)]
    finish_report(report, started)
    print_report(
        report,
        json_output=args.json,
        output=args.output,
        human_lines=_human_lines(report),
    )
    return 0 if all(_record_ok(record) for record in report["records"]) else 1


def _record_ok(record: dict[str, Any]) -> bool:
    start = record.get("start")
    return isinstance(start, dict) and bool(start.get("ok")) and expected_cleanup_ok(record)


def _human_lines(report: dict[str, Any]) -> list[str]:
    lines = ["Preset startup:"]
    for record in report["records"]:
        start = record["start"]
        lines.append(
            f"  {record['preset']} {record['sandbox']}: "
            f"{start['status']} ({start['duration_ms']} ms)"
        )
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
