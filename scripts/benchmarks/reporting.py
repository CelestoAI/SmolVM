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

"""Shared JSON and command helpers for lightweight benchmark scripts."""

from __future__ import annotations

import json
import os
import platform
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, TextIO


@dataclass(frozen=True)
class CommandPlan:
    """A command that a benchmark script can run or report in dry-run mode."""

    label: str
    argv: list[str]
    timeout_s: float | None = None
    cwd: Path | None = None
    env: dict[str, str] | None = None

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "label": self.label,
            "command": self.argv,
            "command_text": command_text(self.argv),
        }
        if self.timeout_s is not None:
            record["timeout_s"] = self.timeout_s
        if self.cwd is not None:
            record["cwd"] = str(self.cwd)
        if self.env:
            record["env_keys"] = sorted(self.env)
        return record


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp with second precision."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def host_info() -> dict[str, str]:
    """Return basic host metadata used by benchmark reports."""

    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def git_info() -> dict[str, Any]:
    """Return best-effort git metadata for benchmark reports."""

    status = _git_output(["status", "--short"])
    return {
        "commit": _git_output(["rev-parse", "HEAD"]) or "unknown",
        "branch": _git_output(["rev-parse", "--abbrev-ref", "HEAD"]) or "unknown",
        "dirty": None if status is None else bool(status),
    }


def package_version(distribution: str) -> str:
    """Return the installed package version, or ``unknown`` when unavailable."""

    try:
        return version(distribution)
    except PackageNotFoundError:
        return "unknown"


def command_text(argv: list[str]) -> str:
    """Return a shell-safe display form for *argv*."""

    return " ".join(shlex.quote(part) for part in argv)


def start_report(
    script: str,
    *,
    config: dict[str, Any],
    dry_run: bool,
    thresholds: dict[str, Any] | None = None,
    variants: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], float]:
    """Create a benchmark report and return it with its monotonic start time."""

    parameters = dict(config)
    return (
        {
            "schema_version": 1,
            "benchmark": script,
            "script": script,
            "created_at": utc_now_iso(),
            "git": git_info(),
            "host": host_info(),
            "smolvm_version": package_version("smolvm"),
            "smolvm_core": package_version("smolvm-core"),
            "parameters": parameters,
            "thresholds": thresholds or {},
            "variants": variants or {},
            "dry_run": dry_run,
            "config": config,
            "records": [],
        },
        time.monotonic(),
    )


def finish_report(report: dict[str, Any], started: float) -> dict[str, Any]:
    """Attach total wall-clock duration to *report*."""

    report["duration_s"] = round(time.monotonic() - started, 3)
    return report


def json_dumps(data: dict[str, Any]) -> str:
    """Serialize report JSON in a stable, human-readable form."""

    return json.dumps(data, indent=2, sort_keys=True)


def write_json_report(path: Path, report: dict[str, Any]) -> None:
    """Write *report* atomically, creating parent directories when needed."""

    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json_dumps(report) + "\n")
    tmp.replace(path)


def print_report(
    report: dict[str, Any],
    *,
    json_output: bool,
    output: Path | None = None,
    human_lines: list[str] | None = None,
    stream: TextIO | None = None,
) -> None:
    """Print or write a report using the common benchmark script rules."""

    out = stream or sys.stdout
    if output is not None:
        write_json_report(output, report)

    if json_output:
        print(json_dumps(report), file=out)
        return

    if output is not None:
        print(f"Wrote {output}", file=out)
    for line in human_lines or default_human_lines(report):
        print(line, file=out)


def default_human_lines(report: dict[str, Any]) -> list[str]:
    """Return a compact human summary for scripts without custom formatting."""

    lines = [
        f"{report.get('script', 'benchmark')}: {len(report.get('records', []))} record(s)",
        f"dry_run: {report.get('dry_run', False)}",
    ]
    duration = report.get("duration_s")
    if isinstance(duration, int | float):
        lines.append(f"duration_s: {duration:.3f}")
    return lines


def expected_cleanup_ok(record: dict[str, Any]) -> bool:
    """Return whether expected cleanup completed successfully."""

    if not record.get("cleanup_expected"):
        return True
    cleanup = record.get("cleanup")
    return isinstance(cleanup, dict) and bool(cleanup.get("ok"))


def run_command(plan: CommandPlan, *, dry_run: bool = False) -> dict[str, Any]:
    """Run *plan* or return a dry-run command record."""

    record = plan.as_dict()
    record["dry_run"] = dry_run

    if dry_run:
        record.update(
            {
                "status": "dry-run",
                "ok": True,
                "exit_code": None,
                "duration_ms": 0.0,
            }
        )
        return record

    env = os.environ.copy()
    if plan.env:
        env.update(plan.env)

    started = time.monotonic()
    try:
        completed = subprocess.run(
            plan.argv,
            cwd=str(plan.cwd) if plan.cwd is not None else None,
            env=env,
            timeout=plan.timeout_s,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        record.update(
            {
                "status": "timeout",
                "ok": False,
                "exit_code": None,
                "duration_ms": _elapsed_ms(started),
                "stdout": _coerce_output(exc.stdout),
                "stderr": _coerce_output(exc.stderr),
                "error": f"Command timed out after {exc.timeout} seconds.",
                "error_type": type(exc).__name__,
            }
        )
        return record
    except OSError as exc:
        record.update(
            {
                "status": "error",
                "ok": False,
                "exit_code": None,
                "duration_ms": _elapsed_ms(started),
                "stdout": "",
                "stderr": "",
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
        )
        return record

    record.update(
        {
            "status": "ok" if completed.returncode == 0 else "failed",
            "ok": completed.returncode == 0,
            "exit_code": completed.returncode,
            "duration_ms": _elapsed_ms(started),
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    )
    return record


def parse_json_output(stdout: str) -> Any:
    """Parse JSON from command stdout, tolerating surrounding plain text."""

    text = stdout.strip()
    if not text:
        raise ValueError("Command did not print JSON.")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def cli_data(payload: Any) -> dict[str, Any]:
    """Return the SmolVM CLI JSON `data` object when present."""

    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    if isinstance(payload, dict):
        return payload
    return {}


def _git_output(args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=Path(__file__).resolve().parents[2],
            timeout=2,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 1)


def _coerce_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
