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

"""Unit tests for the legacy report fetcher demo helpers."""

from __future__ import annotations

import csv
import importlib.util
import json
import sqlite3
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_ROOT = REPO_ROOT / "examples" / "cua" / "legacy_report_fetcher"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_finalize_downloads_writes_manifest_and_pipeline_imports(tmp_path: Path) -> None:
    """The sandbox-side helper should create the expected handoff and SQLite output."""
    finalize = _load_module(
        "legacy_finalize_downloads",
        DEMO_ROOT / "ops" / "finalize_downloads.py",
    )
    pipeline = _load_module(
        "legacy_import_reports",
        DEMO_ROOT / "pipeline" / "import_reports.py",
    )

    report_date = "2026-05-06"
    downloads = tmp_path / "downloads"
    root = tmp_path / "demo"
    downloads.mkdir()
    root.mkdir()

    _write_csv(
        downloads / f"orders_{report_date}.csv",
        ["report_date", "order_id", "customer", "region", "amount"],
        [
            {
                "report_date": report_date,
                "order_id": "A-1",
                "customer": "Acme",
                "region": "West",
                "amount": "10",
            }
        ],
    )
    _write_csv(
        downloads / f"inventory_{report_date}.csv",
        ["report_date", "sku", "name", "on_hand", "warehouse"],
        [
            {
                "report_date": report_date,
                "sku": "SKU-1",
                "name": "Widget",
                "on_hand": "3",
                "warehouse": "SFO",
            }
        ],
    )

    manifest_path = finalize.finalize_downloads(
        root=root,
        session_id="browser-test",
        report_date=report_date,
        downloads_dir=downloads,
        timeout=0.1,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "success"
    assert [item["name"] for item in manifest["files"]] == [
        f"orders_{report_date}.csv",
        f"inventory_{report_date}.csv",
    ]
    assert all(item["sha256"] for item in manifest["files"])

    db_path = pipeline.import_reports(manifest_path.parent)

    assert db_path == root / "artifacts" / "warehouse" / "acme.sqlite"
    with sqlite3.connect(db_path) as conn:
        orders_count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        inventory_count = conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
    assert orders_count == 1
    assert inventory_count == 1
