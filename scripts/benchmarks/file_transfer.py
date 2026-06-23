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

"""Measure file and directory transfer through SmolVM control channels."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

try:
    from .disk_io import parse_size
    from .reporting import finish_report, print_report, start_report
except ImportError:  # pragma: no cover - script execution path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from disk_io import parse_size  # type: ignore[no-redef]
    from reporting import finish_report, print_report, start_report

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))


def elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 1)


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_payload(path: Path, *, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    block = hashlib.sha256(f"smolvm-transfer-benchmark:{size}".encode()).digest()
    remaining = size
    with path.open("wb") as handle:
        while remaining > 0:
            chunk = (block * 32768)[: min(remaining, 1024 * 1024)]
            handle.write(chunk)
            remaining -= len(chunk)


def create_directory_payload(path: Path, *, files: int, file_size: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        write_payload(path / f"file-{index:05d}.bin", size=file_size)


def directory_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        hasher.update(str(child.relative_to(path)).encode())
        hasher.update(b"\0")
        hasher.update(sha256_file(child).encode())
        hasher.update(b"\0")
    return hasher.hexdigest()


def dry_run_records(args: argparse.Namespace, sizes: list[int]) -> list[dict[str, Any]]:
    records = []
    for iteration in range(args.iterations):
        for size in sizes:
            records.append(
                {
                    "iter": iteration,
                    "operation": "file_round_trip",
                    "size_bytes": size,
                    "status": "dry-run",
                    "would_start_vm": True,
                    "backend": args.backend,
                    "comm_channel": args.comm_channel,
                }
            )
        if not args.skip_directory:
            records.append(
                {
                    "iter": iteration,
                    "operation": "directory_round_trip",
                    "files": args.directory_files,
                    "file_size_bytes": args.directory_file_size,
                    "status": "dry-run",
                    "would_start_vm": True,
                    "backend": args.backend,
                    "comm_channel": args.comm_channel,
                }
            )
    return records


def run_iteration(args: argparse.Namespace, iteration: int, sizes: list[int]) -> dict[str, Any]:
    from smolvm.facade import SmolVM

    sandbox_hint = f"{args.name_prefix}-{uuid.uuid4().hex[:8]}"
    workspace = Path(tempfile.mkdtemp(prefix=f"smolvm-transfer-{iteration}-"))
    record: dict[str, Any] = {
        "iter": iteration,
        "sandbox_hint": sandbox_hint,
        "backend": args.backend,
        "comm_channel": args.comm_channel,
        "cleanup_expected": not args.keep,
        "file_records": [],
        "directory_record": None,
    }
    vm = SmolVM(
        backend=args.backend,
        os=args.os,
        comm_channel=None if args.comm_channel == "auto" else args.comm_channel,
        memory=args.memory_mib,
        disk_size=args.disk_size_mib,
    )
    try:
        started = time.monotonic()
        vm.start(boot_timeout=args.boot_timeout)
        record["start_ms"] = elapsed_ms(started)
        record["sandbox"] = vm._vm_id  # noqa: SLF001
        started = time.monotonic()
        vm.wait_for_ready(timeout=args.ready_timeout)
        record["ready_ms"] = elapsed_ms(started)

        for size in sizes:
            record["file_records"].append(measure_file_round_trip(vm, workspace, size=size))
        if not args.skip_directory:
            record["directory_record"] = measure_directory_round_trip(
                vm,
                workspace,
                files=args.directory_files,
                file_size=args.directory_file_size,
            )
        record["status"] = "ok"
    except Exception as exc:  # noqa: BLE001
        record["status"] = "error"
        record["error"] = str(exc)
        record["error_type"] = type(exc).__name__
    finally:
        if not args.keep:
            started = time.monotonic()
            with suppress(Exception):
                vm.stop(timeout=15.0)
            with suppress(Exception):
                vm.delete()
            record["cleanup_ms"] = elapsed_ms(started)
        shutil.rmtree(workspace, ignore_errors=True)
    return record


def measure_file_round_trip(vm: Any, workspace: Path, *, size: int) -> dict[str, Any]:
    source = workspace / f"source-{size}.bin"
    target = workspace / f"download-{size}.bin"
    remote = f"/tmp/smolvm-transfer-bench/file-{size}.bin"
    write_payload(source, size=size)
    source_hash = sha256_file(source)

    started = time.monotonic()
    vm.upload_file(source, remote)
    upload_ms = elapsed_ms(started)

    started = time.monotonic()
    vm.download_file(remote, target)
    download_ms = elapsed_ms(started)

    return {
        "operation": "file_round_trip",
        "size_bytes": size,
        "upload_ms": upload_ms,
        "download_ms": download_ms,
        "sha256_match": sha256_file(target) == source_hash,
    }


def measure_directory_round_trip(
    vm: Any,
    workspace: Path,
    *,
    files: int,
    file_size: int,
) -> dict[str, Any]:
    source = workspace / "dir-source"
    target = workspace / "dir-download"
    remote = "/tmp/smolvm-transfer-bench/directory"
    create_directory_payload(source, files=files, file_size=file_size)
    source_digest = directory_digest(source)

    channel = vm._ensure_control_for_file_transfer()  # noqa: SLF001
    put_directory: Callable[[Path, str], None] | None = getattr(channel, "put_directory", None)
    get_directory: Callable[[str, Path], Path] | None = getattr(channel, "get_directory", None)
    supports = getattr(channel, "supports", None)
    features = {
        "files.stream": bool(callable(supports) and supports("file_raw", "files.stream")),
        "files.directory_tar": bool(
            callable(supports) and supports("dir_tar", "files.directory_tar")
        ),
    }
    if put_directory is None or get_directory is None:
        return {
            "operation": "directory_round_trip",
            "status": "skipped",
            "reason": "selected control channel does not expose directory transfer",
            "features": features,
        }

    started = time.monotonic()
    put_directory(source, remote)
    upload_ms = elapsed_ms(started)

    started = time.monotonic()
    get_directory(remote, target)
    download_ms = elapsed_ms(started)

    return {
        "operation": "directory_round_trip",
        "status": "ok",
        "files": files,
        "file_size_bytes": file_size,
        "total_payload_bytes": files * file_size,
        "upload_ms": upload_ms,
        "download_ms": download_ms,
        "digest_match": directory_digest(target) == source_digest,
        "features": features,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--sizes", default="1K,1M,16M")
    parser.add_argument(
        "--backend",
        choices=("auto", "firecracker", "qemu", "libkrun"),
        default="auto",
    )
    parser.add_argument("--comm-channel", choices=("auto", "vsock", "ssh"), default="auto")
    parser.add_argument("--os", choices=("alpine", "ubuntu"), default="alpine")
    parser.add_argument("--name-prefix", default="bench-transfer")
    parser.add_argument("--memory", dest="memory_mib", type=int, default=None)
    parser.add_argument("--disk-size", dest="disk_size_mib", type=int, default=None)
    parser.add_argument("--boot-timeout", type=float, default=90.0)
    parser.add_argument("--ready-timeout", type=float, default=90.0)
    parser.add_argument("--directory-files", type=int, default=200)
    parser.add_argument("--directory-file-size", type=parse_size, default=parse_size("4K"))
    parser.add_argument("--skip-directory", action="store_true")
    parser.add_argument("--keep", action="store_true", help="Leave created sandboxes running.")
    parser.add_argument("--dry-run", action="store_true", help="Print the benchmark plan only.")
    parser.add_argument("--json", action="store_true", help="Print JSON.")
    parser.add_argument("--output", type=Path, help="Optional JSON output path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("--iterations must be >= 1")
    for option in ("boot_timeout", "ready_timeout"):
        if getattr(args, option) <= 0:
            parser.error(f"--{option.replace('_', '-')} must be > 0")
    if args.directory_files < 1:
        parser.error("--directory-files must be >= 1")
    try:
        sizes = [parse_size(raw) for raw in args.sizes.split(",") if raw.strip()]
    except argparse.ArgumentTypeError as exc:
        parser.error(f"invalid --sizes value {args.sizes!r}: {exc}")
    if not sizes:
        parser.error("--sizes must include at least one size")

    config = {
        "iterations": args.iterations,
        "sizes_bytes": sizes,
        "backend": args.backend,
        "comm_channel": args.comm_channel,
        "os": args.os,
        "name_prefix": args.name_prefix,
        "memory_mib": args.memory_mib,
        "disk_size_mib": args.disk_size_mib,
        "boot_timeout": args.boot_timeout,
        "ready_timeout": args.ready_timeout,
        "directory_files": args.directory_files,
        "directory_file_size": args.directory_file_size,
        "skip_directory": args.skip_directory,
        "keep": args.keep,
    }
    report, started = start_report("file_transfer", config=config, dry_run=args.dry_run)
    report["records"] = (
        dry_run_records(args, sizes)
        if args.dry_run
        else [run_iteration(args, iteration, sizes) for iteration in range(args.iterations)]
    )
    finish_report(report, started)
    print_report(
        report,
        json_output=args.json,
        output=args.output,
        human_lines=_human_lines(report),
    )
    return 0 if all(_record_ok(record) for record in report["records"]) else 1


def _record_ok(record: dict[str, Any]) -> bool:
    if record.get("status") == "dry-run":
        return True
    if record.get("status") != "ok":
        return False
    files = record.get("file_records", [])
    if not all(item.get("sha256_match") for item in files):
        return False
    directory = record.get("directory_record")
    if isinstance(directory, dict) and directory.get("status") == "ok":
        return bool(directory.get("digest_match"))
    return True


def _human_lines(report: dict[str, Any]) -> list[str]:
    lines = ["File transfer:"]
    for record in report["records"]:
        if record.get("status") == "dry-run":
            lines.append(
                f"  {record['operation']} {record.get('size_bytes', record.get('files'))}: dry-run"
            )
            continue
        lines.append(
            f"  {record.get('sandbox', record.get('sandbox_hint'))}: {record['status']} "
            f"(start={record.get('start_ms')} ms, ready={record.get('ready_ms')} ms)"
        )
        for item in record.get("file_records", []):
            lines.append(
                f"    file {item['size_bytes']} bytes: upload={item['upload_ms']} ms, "
                f"download={item['download_ms']} ms"
            )
        directory = record.get("directory_record")
        if isinstance(directory, dict):
            if directory.get("status") == "ok":
                lines.append(
                    f"    directory {directory['files']} file(s): "
                    f"upload={directory['upload_ms']} ms, download={directory['download_ms']} ms"
                )
            else:
                lines.append(
                    f"    directory: {directory.get('status')} - {directory.get('reason')}"
                )
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
