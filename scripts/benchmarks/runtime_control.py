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

"""Measure sandbox lifecycle control commands through the public CLI."""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Any

try:
    from .reporting import (
        CommandPlan,
        expected_cleanup_ok,
        finish_report,
        print_report,
        run_command,
        start_report,
    )
except ImportError:  # pragma: no cover - script execution path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from reporting import (  # type: ignore[no-redef]
        CommandPlan,
        expected_cleanup_ok,
        finish_report,
        print_report,
        run_command,
        start_report,
    )

OPERATIONS = ("info", "pause", "resume", "stop", "start")


def create_command(args: argparse.Namespace, sandbox_name: str) -> list[str]:
    command = [
        "smolvm",
        "sandbox",
        "create",
        "--name",
        sandbox_name,
        "--json",
        "--boot-timeout",
        str(args.boot_timeout),
    ]
    if args.os:
        command.extend(["--os", args.os])
    if args.backend:
        command.extend(["--backend", args.backend])
    if args.comm_channel:
        command.extend(["--comm-channel", args.comm_channel])
    if args.memory_mib is not None:
        command.extend(["--memory", str(args.memory_mib)])
    if args.disk_size_mib is not None:
        command.extend(["--disk-size", str(args.disk_size_mib)])
    for mount in args.mounts:
        command.extend(["--mount", mount])
    if args.writable_mounts:
        command.append("--writable-mounts")
    return command


def operation_plan(
    operation: str,
    sandbox_name: str,
    *,
    boot_timeout: float,
    stop_timeout: float,
    command_timeout: float,
) -> CommandPlan:
    if operation == "info":
        command = ["smolvm", "sandbox", "info", sandbox_name, "--json"]
    elif operation == "pause":
        command = ["smolvm", "sandbox", "pause", sandbox_name, "--json"]
    elif operation == "resume":
        command = ["smolvm", "sandbox", "resume", sandbox_name, "--json"]
    elif operation == "stop":
        command = [
            "smolvm",
            "sandbox",
            "stop",
            sandbox_name,
            "--timeout",
            str(stop_timeout),
            "--json",
        ]
    elif operation == "start":
        command = [
            "smolvm",
            "sandbox",
            "start",
            sandbox_name,
            "--boot-timeout",
            str(boot_timeout),
            "--json",
        ]
    else:
        raise ValueError(f"Unknown operation: {operation}")
    return CommandPlan(f"sandbox_{operation}", command, timeout_s=command_timeout)


def cleanup_plan(sandbox_name: str, *, timeout_s: float | None) -> CommandPlan:
    return CommandPlan(
        "sandbox_delete",
        ["smolvm", "sandbox", "delete", sandbox_name, "--json"],
        timeout_s=timeout_s,
    )


def run_iteration(
    args: argparse.Namespace, iteration: int, operations: list[str]
) -> dict[str, Any]:
    sandbox_name = f"{args.name_prefix}-{uuid.uuid4().hex[:8]}"
    record: dict[str, Any] = {
        "iter": iteration,
        "sandbox": sandbox_name,
        "operations": [],
        "cleanup_expected": False,
    }

    create = run_command(
        CommandPlan(
            "sandbox_create",
            create_command(args, sandbox_name),
            timeout_s=args.command_timeout,
        ),
        dry_run=args.dry_run,
    )
    record["create"] = create

    should_run_operations = args.dry_run or create["ok"]
    if should_run_operations:
        for operation in operations:
            step = run_command(
                operation_plan(
                    operation,
                    sandbox_name,
                    boot_timeout=args.boot_timeout,
                    stop_timeout=args.stop_timeout,
                    command_timeout=args.command_timeout,
                ),
                dry_run=args.dry_run,
            )
            record["operations"].append(step)
            if not args.dry_run and not step["ok"]:
                break

    cleanup_expected = not args.keep
    record["cleanup_expected"] = cleanup_expected
    if cleanup_expected:
        record["cleanup"] = run_command(
            cleanup_plan(sandbox_name, timeout_s=args.command_timeout),
            dry_run=args.dry_run,
        )
    return record


def parse_operations(raw: str) -> list[str]:
    operations = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = sorted(set(operations) - set(OPERATIONS))
    if unknown:
        allowed = ", ".join(OPERATIONS)
        raise ValueError(f"Unknown operation(s): {', '.join(unknown)}. Choices: {allowed}.")
    if not operations:
        raise ValueError("At least one operation is required.")
    return operations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--name-prefix", default="bench-runtime")
    parser.add_argument("--operations", default="info,pause,resume,stop,start")
    parser.add_argument("--os", choices=("alpine", "ubuntu"), default=None)
    parser.add_argument(
        "--backend",
        choices=("auto", "firecracker", "qemu", "libkrun"),
        default=None,
    )
    parser.add_argument("--comm-channel", choices=("ssh", "vsock"), default=None)
    parser.add_argument("--memory", dest="memory_mib", type=int, default=None)
    parser.add_argument("--disk-size", dest="disk_size_mib", type=int, default=None)
    parser.add_argument("--mount", dest="mounts", action="append", default=[])
    parser.add_argument("--writable-mounts", action="store_true")
    parser.add_argument("--boot-timeout", type=float, default=90.0)
    parser.add_argument("--stop-timeout", type=float, default=3.0)
    parser.add_argument("--command-timeout", type=float, default=900.0)
    parser.add_argument("--keep", action="store_true", help="Leave created sandboxes in place.")
    parser.add_argument("--dry-run", action="store_true", help="Print the command plan only.")
    parser.add_argument("--json", action="store_true", help="Print JSON.")
    parser.add_argument("--output", type=Path, help="Optional JSON output path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("--iterations must be >= 1")
    for option in ("boot_timeout", "stop_timeout", "command_timeout"):
        if getattr(args, option) <= 0:
            parser.error(f"--{option.replace('_', '-')} must be > 0")
    try:
        operations = parse_operations(args.operations)
    except ValueError as exc:
        parser.error(str(exc))

    config = {
        "iterations": args.iterations,
        "name_prefix": args.name_prefix,
        "operations": operations,
        "os": args.os,
        "backend": args.backend,
        "comm_channel": args.comm_channel,
        "memory_mib": args.memory_mib,
        "disk_size_mib": args.disk_size_mib,
        "mounts": args.mounts,
        "writable_mounts": args.writable_mounts,
        "boot_timeout": args.boot_timeout,
        "stop_timeout": args.stop_timeout,
        "command_timeout": args.command_timeout,
        "keep": args.keep,
    }
    report, started = start_report("runtime_control", config=config, dry_run=args.dry_run)
    report["records"] = [
        run_iteration(args, iteration, operations) for iteration in range(args.iterations)
    ]
    finish_report(report, started)
    print_report(
        report,
        json_output=args.json,
        output=args.output,
        human_lines=_human_lines(report),
    )
    return 0 if all(_record_ok(record) for record in report["records"]) else 1


def _record_ok(record: dict[str, Any]) -> bool:
    create = record.get("create")
    if not isinstance(create, dict) or not create.get("ok"):
        return False
    if not expected_cleanup_ok(record):
        return False
    operations = record.get("operations", [])
    return all(isinstance(operation, dict) and operation.get("ok") for operation in operations)


def _human_lines(report: dict[str, Any]) -> list[str]:
    lines = ["Runtime control:"]
    for record in report["records"]:
        create = record["create"]
        steps = ", ".join(step["status"] for step in record["operations"])
        lines.append(
            f"  {record['sandbox']}: create={create['status']}"
            + (f", operations={steps}" if steps else "")
        )
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
