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

"""Move downloaded reports into the pipeline handoff folder and write a manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from datetime import date
from pathlib import Path


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest for a file."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wait_for_downloads(downloads_dir: Path, filenames: list[str], timeout: float) -> None:
    """Wait for browser downloads to appear and finish."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        missing = [name for name in filenames if not (downloads_dir / name).exists()]
        partial = list(downloads_dir.glob("*.crdownload"))
        if not missing and not partial:
            return
        time.sleep(0.5)

    missing = [name for name in filenames if not (downloads_dir / name).exists()]
    existing = ", ".join(sorted(path.name for path in downloads_dir.glob("*"))) or "<empty>"
    if missing:
        raise RuntimeError(
            "Missing expected report download(s): "
            + ", ".join(str(downloads_dir / name) for name in missing)
            + f". Found in {downloads_dir}: {existing}"
        )
    raise RuntimeError(
        f"Report downloads did not finish before the timeout. Found in {downloads_dir}: {existing}"
    )


def finalize_downloads(
    *,
    root: Path,
    session_id: str,
    report_date: str,
    downloads_dir: Path | None = None,
    inbox_dir: Path | None = None,
    timeout: float = 30.0,
) -> Path:
    """Copy downloaded files into the handoff folder and write manifest.json."""
    downloads = downloads_dir or Path(f"/opt/smolvm-browser/downloads/{session_id}")
    inbox = inbox_dir or root / "artifacts" / "inbox" / "acme" / report_date
    expected = [f"orders_{report_date}.csv", f"inventory_{report_date}.csv"]

    wait_for_downloads(downloads, expected, timeout)
    inbox.mkdir(parents=True, exist_ok=True)

    files: list[dict[str, str | int]] = []
    for filename in expected:
        source = downloads / filename
        target = inbox / filename
        shutil.copy2(source, target)
        files.append(
            {
                "name": filename,
                "path": str(target),
                "size_bytes": target.stat().st_size,
                "sha256": sha256_file(target),
                "status": "downloaded",
            }
        )

    manifest = {
        "source": "acme_legacy_reports",
        "run_date": date.today().isoformat(),
        "report_date": report_date,
        "status": "success",
        "files": files,
    }
    manifest_path = inbox / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Finalize downloaded Acme reports.")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--report-date", required=True)
    parser.add_argument("--downloads-dir", type=Path)
    parser.add_argument("--inbox-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    manifest_path = finalize_downloads(
        root=args.root,
        session_id=args.session_id,
        report_date=args.report_date,
        downloads_dir=args.downloads_dir,
        inbox_dir=args.inbox_dir,
        timeout=args.timeout,
    )
    print(f"Wrote manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
