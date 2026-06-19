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

"""SmolVM image-cache pruning — remove stale version directories."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from smolvm import __version__
from smolvm.cli.output import console_stdout, emit_json
from smolvm.images.manager import ImageManager


def _total_size(path: Path) -> int:
    """Recursively sum file sizes under *path*."""
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _format_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} TiB"


_VERSION_RE = re.compile(r"-v(\d+\.\d+\.\d+[a-z0-9]*)-")


def find_stale_caches(
    cache_dir: Path | None = None,
    current_version: str = __version__,
) -> list[Path]:
    """Return cache directories that belong to older SmolVM versions.

    Directories whose name contains ``-v<version>-`` where ``<version>``
    differs from ``current_version`` are considered stale. Unversioned
    directories (e.g. ``s3/``) and the current version are left alone.
    """
    root = cache_dir or ImageManager.DEFAULT_CACHE_DIR
    if not root.is_dir():
        return []
    stale: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        m = _VERSION_RE.search(child.name)
        if m and m.group(1) != current_version:
            stale.append(child)
    return stale


def run_prune(
    *,
    dry_run: bool = False,
    json_output: bool = False,
    cache_dir: str | None = None,
) -> int:
    """Execute ``smolvm prune``."""
    cache_root = Path(cache_dir) if cache_dir else None
    stale = find_stale_caches(cache_dir=cache_root)

    if not stale:
        if json_output:
            emit_json("prune", 0, data={"removed": [], "freed_bytes": 0})
        else:
            console = console_stdout()
            console.print("Nothing to prune — cache is clean.")
        return 0

    entries: list[dict[str, str | int]] = []
    total_bytes = 0
    for path in stale:
        size = _total_size(path)
        total_bytes += size
        entries.append({"path": str(path), "size_bytes": size})

    if dry_run:
        if json_output:
            emit_json(
                "prune",
                0,
                data={
                    "dry_run": True,
                    "would_remove": entries,
                    "would_free_bytes": total_bytes,
                },
            )
        else:
            console = console_stdout()
            console.print(
                f"[bold]Would remove {len(entries)} stale cache(s) "
                f"({_format_bytes(total_bytes)}):[/bold]"
            )
            for e in entries:
                size_bytes = e["size_bytes"]
                assert isinstance(size_bytes, int)
                console.print(f"  {e['path']}  ({_format_bytes(size_bytes)})")
        return 0

    for path in stale:
        shutil.rmtree(path)

    if json_output:
        emit_json(
            "prune",
            0,
            data={
                "removed": [str(p) for p in stale],
                "freed_bytes": total_bytes,
            },
        )
    else:
        console = console_stdout()
        console.print(f"Removed {len(stale)} stale cache(s), freed {_format_bytes(total_bytes)}.")
    return 0
