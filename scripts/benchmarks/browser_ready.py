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

"""Measure browser sandbox startup and CDP readiness through the public CLI."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
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


def build_start_command(args: argparse.Namespace, session_id: str) -> list[str]:
    command = [
        "smolvm",
        "browser",
        "start",
        "--session-id",
        session_id,
        "--json",
        "--backend",
        args.backend,
        "--boot-timeout",
        str(args.boot_timeout),
        "--timeout-minutes",
        str(args.timeout_minutes),
        "--viewport-width",
        str(args.viewport_width),
        "--viewport-height",
        str(args.viewport_height),
        "--memory",
        str(args.memory_mib),
        "--disk-size",
        str(args.disk_size_mib),
    ]
    if args.live:
        command.append("--live")
    if args.profile_mode:
        command.extend(["--profile-mode", args.profile_mode])
    if args.profile_id:
        command.extend(["--profile-id", args.profile_id])
    if args.record_video:
        command.append("--record-video")
    if args.no_downloads:
        command.append("--no-downloads")
    return command


def stop_plan(session_id: str, *, timeout_s: float | None) -> CommandPlan:
    return CommandPlan("browser_stop", ["smolvm", "browser", "stop", session_id], timeout_s)


def run_iteration(args: argparse.Namespace, iteration: int) -> dict[str, Any]:
    session_id = args.session_id or f"{args.session_prefix}-{uuid.uuid4().hex[:8]}"
    record: dict[str, Any] = {
        "iter": iteration,
        "session_id": session_id,
        "cleanup_expected": not args.keep,
    }

    start_record = run_command(
        CommandPlan(
            "browser_start",
            build_start_command(args, session_id),
            timeout_s=args.command_timeout,
        ),
        dry_run=args.dry_run,
    )
    record["start"] = start_record

    if args.dry_run:
        if not args.keep:
            record["cleanup"] = run_command(
                stop_plan(session_id, timeout_s=args.command_timeout),
                dry_run=True,
            )
        return record

    data: dict[str, Any] = {}
    if start_record["ok"]:
        try:
            payload = parse_json_output(start_record.get("stdout", ""))
            data = cli_data(payload)
            record["start_payload"] = payload
        except Exception as exc:  # noqa: BLE001
            record["start_parse_error"] = str(exc)

    cdp_url = data.get("cdp_url")
    if not args.no_cdp_poll:
        if isinstance(cdp_url, str) and cdp_url.strip():
            record["cdp_probe"] = poll_cdp(cdp_url, timeout_s=args.cdp_timeout)
        else:
            record["cdp_probe"] = {
                "url": cdp_url,
                "ready": False,
                "attempts": 0,
                "duration_ms": 0.0,
                "error": "browser start did not return a CDP URL",
            }

    if not args.keep:
        record["cleanup"] = run_command(
            stop_plan(session_id, timeout_s=args.command_timeout),
            dry_run=False,
        )
    return record


def poll_cdp(cdp_url: str, *, timeout_s: float) -> dict[str, Any]:
    url = cdp_url.rstrip("/") + "/json/version"
    started = time.monotonic()
    deadline = started + timeout_s
    attempts = 0
    last_error: str | None = None
    status_code: int | None = None
    payload: Any = None

    while time.monotonic() <= deadline:
        attempts += 1
        try:
            request_timeout = min(1.0, max(0.1, deadline - time.monotonic()))
            with urllib.request.urlopen(url, timeout=request_timeout) as response:
                status_code = response.status
                body = response.read().decode(errors="replace")
                payload = json.loads(body) if body.strip() else None
                return {
                    "url": url,
                    "ready": True,
                    "attempts": attempts,
                    "status_code": status_code,
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    "payload": payload,
                }
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            time.sleep(0.2)

    return {
        "url": url,
        "ready": False,
        "attempts": attempts,
        "status_code": status_code,
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
        "error": last_error,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--session-prefix", default="bench-browser")
    parser.add_argument(
        "--backend",
        choices=("auto", "firecracker", "qemu", "libkrun"),
        default="auto",
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--profile-mode",
        choices=("ephemeral", "persistent"),
        default="ephemeral",
    )
    parser.add_argument("--profile-id", default=None)
    parser.add_argument("--timeout-minutes", type=int, default=30)
    parser.add_argument("--viewport-width", type=int, default=1280)
    parser.add_argument("--viewport-height", type=int, default=720)
    parser.add_argument("--memory", dest="memory_mib", type=int, default=2048)
    parser.add_argument("--disk-size", dest="disk_size_mib", type=int, default=4096)
    parser.add_argument("--record-video", action="store_true")
    parser.add_argument("--no-downloads", action="store_true")
    parser.add_argument("--boot-timeout", type=float, default=90.0)
    parser.add_argument("--cdp-timeout", type=float, default=10.0)
    parser.add_argument("--no-cdp-poll", action="store_true")
    parser.add_argument("--command-timeout", type=float, default=900.0)
    parser.add_argument("--keep", action="store_true", help="Leave browser sandboxes running.")
    parser.add_argument("--dry-run", action="store_true", help="Print the command plan only.")
    parser.add_argument("--json", action="store_true", help="Print JSON.")
    parser.add_argument("--output", type=Path, help="Optional JSON output path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("--iterations must be >= 1")
    for option in ("boot_timeout", "cdp_timeout", "command_timeout"):
        if getattr(args, option) <= 0:
            parser.error(f"--{option.replace('_', '-')} must be > 0")
    for option in (
        "timeout_minutes",
        "viewport_width",
        "viewport_height",
        "memory_mib",
        "disk_size_mib",
    ):
        if getattr(args, option) < 1:
            parser.error(f"--{option.replace('_mib', '').replace('_', '-')} must be >= 1")

    config = {
        "iterations": args.iterations,
        "session_id": args.session_id,
        "session_prefix": args.session_prefix,
        "backend": args.backend,
        "live": args.live,
        "profile_mode": args.profile_mode,
        "profile_id": args.profile_id,
        "timeout_minutes": args.timeout_minutes,
        "viewport": {"width": args.viewport_width, "height": args.viewport_height},
        "memory_mib": args.memory_mib,
        "disk_size_mib": args.disk_size_mib,
        "record_video": args.record_video,
        "allow_downloads": not args.no_downloads,
        "boot_timeout": args.boot_timeout,
        "cdp_timeout": args.cdp_timeout,
        "cdp_poll": not args.no_cdp_poll,
        "command_timeout": args.command_timeout,
        "keep": args.keep,
    }
    report, started = start_report("browser_ready", config=config, dry_run=args.dry_run)
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
    if not isinstance(start, dict) or not start.get("ok"):
        return False
    if not expected_cleanup_ok(record):
        return False
    probe = record.get("cdp_probe")
    return not isinstance(probe, dict) or bool(probe.get("ready"))


def _human_lines(report: dict[str, Any]) -> list[str]:
    lines = ["Browser readiness:"]
    for record in report["records"]:
        start = record["start"]
        probe = record.get("cdp_probe")
        probe_text = ""
        if isinstance(probe, dict):
            probe_text = f", cdp_ready={probe.get('ready')}"
        lines.append(
            f"  {record['session_id']}: {start['status']} ({start['duration_ms']} ms{probe_text})"
        )
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
