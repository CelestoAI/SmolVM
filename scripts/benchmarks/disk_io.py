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

"""Measure host disk copy and zstd decompression helpers."""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    from .reporting import finish_report, print_report, start_report
except ImportError:  # pragma: no cover - script execution path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from reporting import finish_report, print_report, start_report

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

DISK_DISABLE_ENV = "SMOLVM_DISABLE_NATIVE_DISK"
VARIANTS = ("native", "forced-off")
OPERATIONS = ("copy", "decompress")
SIZE_SUFFIXES = {
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024 * 1024,
    "mb": 1024 * 1024,
    "mib": 1024 * 1024,
    "g": 1024 * 1024 * 1024,
    "gb": 1024 * 1024 * 1024,
    "gib": 1024 * 1024 * 1024,
}


@contextmanager
def disk_variant(variant: str) -> Iterator[None]:
    old_value = os.environ.get(DISK_DISABLE_ENV)
    try:
        if variant == "forced-off":
            os.environ[DISK_DISABLE_ENV] = "1"
        else:
            os.environ.pop(DISK_DISABLE_ENV, None)
        yield
    finally:
        if old_value is None:
            os.environ.pop(DISK_DISABLE_ENV, None)
        else:
            os.environ[DISK_DISABLE_ENV] = old_value


def parse_size(raw: str) -> int:
    text = raw.strip().lower()
    if not text:
        raise argparse.ArgumentTypeError("size cannot be empty")
    number = text.rstrip("abcdefghijklmnopqrstuvwxyz")
    suffix = text[len(number) :] or "b"
    if suffix not in SIZE_SUFFIXES:
        raise argparse.ArgumentTypeError(f"unsupported size suffix in {raw!r}")
    try:
        value = float(number)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid size {raw!r}") from exc
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"invalid size {raw!r}")
    try:
        size = int(value * SIZE_SUFFIXES[suffix])
    except OverflowError as exc:
        raise argparse.ArgumentTypeError(f"size is too large: {raw!r}") from exc
    if size < 1:
        raise argparse.ArgumentTypeError("size must be at least 1 byte")
    return size


def parse_csv(raw: str, *, allowed: tuple[str, ...]) -> list[str]:
    values = [item.strip() for item in raw.split(",") if item.strip()]
    unknown = sorted(set(values) - set(allowed))
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown value(s): {', '.join(unknown)}")
    if not values:
        raise argparse.ArgumentTypeError("at least one value is required")
    return values


def allocated_bytes(path: Path) -> int | None:
    try:
        return path.stat().st_blocks * 512
    except (AttributeError, OSError):
        return None


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_sparse_source(path: Path, *, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    chunk_size = min(1024 * 1024, size)
    anchors = sorted({0, max(0, size // 2 - chunk_size // 2), max(0, size - chunk_size)})
    with path.open("wb") as handle:
        for offset in anchors:
            handle.seek(offset)
            handle.write(_deterministic_block(offset, chunk_size))
        handle.truncate(size)


def _deterministic_block(offset: int, size: int) -> bytes:
    seed = hashlib.sha256(f"smolvm-disk-benchmark:{offset}".encode()).digest()
    repeats = (size // len(seed)) + 1
    return (seed * repeats)[:size]


def compress_zstd(source: Path, target: Path) -> float:
    import zstandard

    started = time.monotonic()
    with source.open("rb") as src, target.open("wb") as dst:
        compressor = zstandard.ZstdCompressor(level=3)
        with compressor.stream_writer(dst) as writer:
            shutil.copyfileobj(src, writer, length=1024 * 1024)
    return elapsed_ms(started)


def elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 1)


def dry_run_records(
    args: argparse.Namespace,
    sizes: list[int],
    operations: list[str],
) -> list[dict]:
    records = []
    for iteration in range(args.iterations):
        for size in sizes:
            for operation in operations:
                for variant in args.variants:
                    records.append(
                        {
                            "iter": iteration,
                            "operation": operation,
                            "variant": variant,
                            "size_bytes": size,
                            "status": "dry-run",
                            "would_generate_sparse_source": True,
                        }
                    )
    return records


def run_records(args: argparse.Namespace, sizes: list[int], operations: list[str]) -> list[dict]:
    from smolvm.host import disk as disk_helpers

    base_dir = args.work_dir or Path(tempfile.mkdtemp(prefix="smolvm-disk-bench-"))
    base_dir = base_dir.expanduser()
    base_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    try:
        for iteration in range(args.iterations):
            for size in sizes:
                source = base_dir / f"source-{iteration}-{size}.img"
                compressed = base_dir / f"source-{iteration}-{size}.img.zst"
                write_sparse_source(source, size=size)
                source_hash = sha256_file(source)
                compress_ms = (
                    compress_zstd(source, compressed) if "decompress" in operations else None
                )

                for operation in operations:
                    for variant in args.variants:
                        record = {
                            "iter": iteration,
                            "operation": operation,
                            "variant": variant,
                            "size_bytes": size,
                            "source_allocated_bytes": allocated_bytes(source),
                            "source_sha256": source_hash,
                        }
                        if compress_ms is not None:
                            record["zstd_prepare_ms"] = compress_ms
                            record["zstd_bytes"] = compressed.stat().st_size
                        with disk_variant(variant):
                            if variant == "native" and not disk_helpers.has_native_disk_io():
                                record["status"] = "skipped"
                                record["reason"] = "native disk helpers are unavailable"
                                records.append(record)
                                continue
                            target = base_dir / f"{operation}-{variant}-{iteration}-{size}.img"
                            started = time.monotonic()
                            if operation == "copy":
                                method = disk_helpers.clone_or_sparse_copy(source, target)
                            else:
                                method = disk_helpers.decompress_zstd_sparse(
                                    compressed,
                                    target,
                                    chunk_size=args.chunk_size,
                                )
                            target_hash = sha256_file(target)
                            record.update(
                                {
                                    "status": "ok",
                                    "duration_ms": elapsed_ms(started),
                                    "method": method,
                                    "target_bytes": target.stat().st_size,
                                    "target_allocated_bytes": allocated_bytes(target),
                                    "target_sha256": target_hash,
                                    "sha256_match": target_hash == source_hash,
                                }
                            )
                            records.append(record)
        return records
    finally:
        if not args.keep and args.work_dir is None:
            shutil.rmtree(base_dir, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--sizes", default="16M,128M")
    parser.add_argument("--operations", default="copy,decompress")
    parser.add_argument("--variants", default="native,forced-off")
    parser.add_argument("--chunk-size", type=int, default=1024 * 1024)
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--keep", action="store_true", help="Keep generated benchmark files.")
    parser.add_argument("--dry-run", action="store_true", help="Print the benchmark plan only.")
    parser.add_argument("--json", action="store_true", help="Print JSON.")
    parser.add_argument("--output", type=Path, help="Optional JSON output path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("--iterations must be >= 1")
    if args.chunk_size < 1:
        parser.error("--chunk-size must be >= 1")
    try:
        sizes = [parse_size(raw) for raw in args.sizes.split(",") if raw.strip()]
        operations = parse_csv(args.operations, allowed=OPERATIONS)
        args.variants = parse_csv(args.variants, allowed=VARIANTS)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    if not sizes:
        parser.error("--sizes must include at least one size")

    config = {
        "iterations": args.iterations,
        "sizes_bytes": sizes,
        "operations": operations,
        "variants": args.variants,
        "chunk_size": args.chunk_size,
        "work_dir": str(args.work_dir.expanduser()) if args.work_dir else None,
        "keep": args.keep,
    }
    report, started = start_report("disk_io", config=config, dry_run=args.dry_run)
    report["records"] = (
        dry_run_records(args, sizes, operations)
        if args.dry_run
        else run_records(args, sizes, operations)
    )
    finish_report(report, started)
    print_report(
        report,
        json_output=args.json,
        output=args.output,
        human_lines=_human_lines(report),
    )
    ok_statuses = {"ok", "skipped", "dry-run"}
    return 0 if all(record.get("status") in ok_statuses for record in report["records"]) else 1


def _human_lines(report: dict[str, Any]) -> list[str]:
    lines = ["Disk I/O:"]
    for record in report["records"]:
        label = f"{record['operation']} {record['variant']} {record['size_bytes']} bytes"
        if record["status"] == "ok":
            lines.append(f"  {label}: {record['duration_ms']} ms ({record['method']})")
        elif record["status"] == "skipped":
            lines.append(f"  {label}: skipped - {record['reason']}")
        else:
            lines.append(f"  {label}: {record['status']}")
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
