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

"""Small stand-in for an existing report import pipeline."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path

TABLE_COLUMNS = {
    "orders": ["report_date", "order_id", "customer", "region", "amount"],
    "inventory": ["report_date", "sku", "name", "on_hand", "warehouse"],
}


def _table_for_file(path: Path) -> str:
    if path.name.startswith("orders_"):
        return "orders"
    if path.name.startswith("inventory_"):
        return "inventory"
    raise ValueError(f"Unknown report file: {path.name}")


def _ensure_table(conn: sqlite3.Connection, table: str, columns: list[str]) -> None:
    column_sql = ", ".join(f'"{column}" TEXT' for column in columns)
    conn.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({column_sql})')


def _import_csv(conn: sqlite3.Connection, table: str, path: Path) -> int:
    columns = TABLE_COLUMNS[table]
    _ensure_table(conn, table, columns)
    conn.execute(f'DELETE FROM "{table}" WHERE report_date = ?', (path.stem.rsplit("_", 1)[-1],))

    with path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))

    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(f'"{column}"' for column in columns)
    conn.executemany(
        f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders})',
        [[row.get(column, "") for column in columns] for row in rows],
    )
    return len(rows)


def import_reports(inbox_dir: Path) -> Path:
    """Import report files described by manifest.json into SQLite."""
    manifest_path = inbox_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    warehouse_dir = inbox_dir.parents[2] / "warehouse"
    warehouse_dir.mkdir(parents=True, exist_ok=True)
    db_path = warehouse_dir / "acme.sqlite"

    print(f"Found manifest.json for {manifest['source']}")
    print(f"Report date: {manifest['report_date']}")

    with sqlite3.connect(db_path) as conn:
        for item in manifest["files"]:
            path = Path(item["path"])
            table = _table_for_file(path)
            row_count = _import_csv(conn, table, path)
            print(f"Imported {path.name}: {row_count} rows into {table}")
        conn.commit()

    print(f"Stored normalized data in {db_path}")
    print("Run complete.")
    return db_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Acme report CSV files into SQLite.")
    parser.add_argument("inbox_dir", type=Path)
    args = parser.parse_args()

    import_reports(args.inbox_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
