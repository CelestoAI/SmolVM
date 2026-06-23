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

"""Record lightweight metadata for benchmark artifact paths."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

try:
    from .reporting import finish_report, print_report, start_report
except ImportError:  # pragma: no cover - script execution path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from reporting import finish_report, print_report, start_report


def default_paths() -> list[Path]:
    """Return the default local paths worth recording for benchmark runs."""

    data_dir = Path(os.environ.get("SMOLVM_DATA_DIR", "~/.smolvm")).expanduser()
    return [data_dir / "images", data_dir / "browser-sessions"]


def inspect_path(
    path: Path,
    *,
    hash_files: bool,
    max_hash_bytes: int,
    max_entries: int,
) -> dict[str, Any]:
    """Return metadata for *path* without mutating it."""

    path = path.expanduser()
    record: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
    }
    if not path.exists():
        record["status"] = "missing"
        return record

    try:
        is_file = path.is_file()
        is_dir = path.is_dir()
    except OSError as exc:
        record.update({"status": "error", "error": str(exc)})
        return record

    if is_file:
        try:
            stat = path.stat()
        except OSError as exc:
            record.update({"status": "error", "kind": "file", "error": str(exc)})
            return record
        record.update(
            {
                "status": "ok",
                "kind": "file",
                "bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
        if hash_files:
            record.update(_hash_file(path, max_hash_bytes=max_hash_bytes))
        return record

    if not is_dir:
        record.update({"status": "skipped", "kind": "other"})
        return record

    total_bytes = 0
    file_count = 0
    dir_count = 0
    sample_files: list[dict[str, Any]] = []
    truncated = False

    for index, child in enumerate(path.rglob("*"), start=1):
        if index > max_entries:
            truncated = True
            break
        try:
            if child.is_dir():
                dir_count += 1
                continue
            if not child.is_file():
                continue
            stat = child.stat()
        except OSError as exc:
            sample_files.append({"path": str(child), "error": str(exc)})
            continue

        file_count += 1
        total_bytes += stat.st_size
        if len(sample_files) < 20:
            entry: dict[str, Any] = {
                "path": str(child),
                "relative_path": str(child.relative_to(path)),
                "bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
            if hash_files:
                entry.update(_hash_file(child, max_hash_bytes=max_hash_bytes))
            sample_files.append(entry)

    record.update(
        {
            "status": "ok",
            "kind": "directory",
            "files": file_count,
            "directories": dir_count,
            "bytes": total_bytes,
            "truncated": truncated,
            "max_entries": max_entries,
            "sample_files": sample_files,
        }
    )
    return record


def dry_run_record(path: Path, *, hash_files: bool, max_hash_bytes: int, max_entries: int) -> dict:
    return {
        "path": str(path.expanduser()),
        "status": "dry-run",
        "would_scan": True,
        "hash_files": hash_files,
        "max_hash_bytes": max_hash_bytes,
        "max_entries": max_entries,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        action="append",
        type=Path,
        dest="paths",
        help="Artifact path to record. Repeat for multiple paths.",
    )
    parser.add_argument(
        "--hash-files",
        action="store_true",
        help="Hash individual files up to --max-hash-mib.",
    )
    parser.add_argument("--max-hash-mib", type=float, default=512.0)
    parser.add_argument("--max-entries", type=int, default=5000)
    parser.add_argument("--dry-run", action="store_true", help="Print the scan plan only.")
    parser.add_argument("--json", action="store_true", help="Print JSON.")
    parser.add_argument("--output", type=Path, help="Optional JSON output path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_hash_mib <= 0:
        parser.error("--max-hash-mib must be > 0")
    if args.max_entries < 1:
        parser.error("--max-entries must be >= 1")

    paths = args.paths or default_paths()
    max_hash_bytes = int(args.max_hash_mib * 1024 * 1024)
    config = {
        "paths": [str(path.expanduser()) for path in paths],
        "hash_files": args.hash_files,
        "max_hash_mib": args.max_hash_mib,
        "max_entries": args.max_entries,
    }
    report, started = start_report("artifacts", config=config, dry_run=args.dry_run)
    report["records"] = [
        dry_run_record(
            path,
            hash_files=args.hash_files,
            max_hash_bytes=max_hash_bytes,
            max_entries=args.max_entries,
        )
        if args.dry_run
        else inspect_path(
            path,
            hash_files=args.hash_files,
            max_hash_bytes=max_hash_bytes,
            max_entries=args.max_entries,
        )
        for path in paths
    ]
    finish_report(report, started)
    print_report(
        report,
        json_output=args.json,
        output=args.output,
        human_lines=_human_lines(report),
    )
    return 0


def _hash_file(path: Path, *, max_hash_bytes: int) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        return {"sha256": None, "hash_error": str(exc)}
    if size > max_hash_bytes:
        return {"sha256": None, "hash_skipped": "file exceeds --max-hash-mib"}

    hasher = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
    except OSError as exc:
        return {"sha256": None, "hash_error": str(exc)}
    return {"sha256": hasher.hexdigest(), "hash_skipped": None}


def _human_lines(report: dict[str, Any]) -> list[str]:
    lines = ["Artifact paths:"]
    for record in report["records"]:
        status = record["status"]
        if status == "ok" and record.get("kind") == "directory":
            lines.append(f"  {record['path']}: {record['files']} file(s), {record['bytes']} bytes")
        elif status == "ok":
            lines.append(f"  {record['path']}: {record.get('bytes', 0)} bytes")
        else:
            lines.append(f"  {record['path']}: {status}")
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
