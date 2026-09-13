#!/usr/bin/env python3
import csv
import json
import os
import re
import sys
from decimal import Decimal, InvalidOperation

ROOT = sys.argv[1]
REQUIRED = ["brief.md", "itinerary.md", "budget.csv", "sources.json"]
errors = []

for name in REQUIRED:
    path = os.path.join(ROOT, name)
    if not os.path.isfile(path):
        errors.append(f"Missing {name}.")

source_ids = set()
if not errors:
    with open(os.path.join(ROOT, "sources.json"), encoding="utf-8") as handle:
        sources = json.load(handle)
    source_ids = {source["id"] for source in sources}
    if not source_ids:
        errors.append("sources.json must include at least one source.")
    for name in ["brief.md", "itinerary.md"]:
        with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
            content = handle.read()
        for marker in re.findall(r"\[source:(S\d{2})\]", content):
            if marker not in source_ids:
                errors.append(f"{name} refers to unknown source {marker}.")
    with open(os.path.join(ROOT, "itinerary.md"), encoding="utf-8") as handle:
        itinerary = handle.read()
    for day in [1, 2, 3]:
        if not re.search(rf"^#{{1,3}}\s+Day {day}\b", itinerary, re.MULTILINE | re.IGNORECASE):
            errors.append(f"itinerary.md is missing Day {day}.")
    total = Decimal("0")
    contingencies = 0
    with open(os.path.join(ROOT, "budget.csv"), newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        expected = ["category", "item", "quantity", "unit_cost_inr", "total_inr", "source_id"]
        if reader.fieldnames != expected:
            errors.append("budget.csv has the wrong header.")
        else:
            for row in reader:
                try:
                    quantity = int(row["quantity"])
                    unit = Decimal(row["unit_cost_inr"])
                    line = Decimal(row["total_inr"])
                    if quantity <= 0 or unit < 0 or line != unit * quantity:
                        errors.append(f"Invalid budget row: {row['item']}.")
                    total += line
                except (ValueError, InvalidOperation):
                    errors.append(f"Invalid number in budget row: {row.get('item', 'unknown')}.")
                if row["category"] == "contingency":
                    contingencies += 1
                elif row["source_id"] not in source_ids:
                    errors.append(f"Unknown budget source: {row['source_id']}.")
    if contingencies != 1:
        errors.append("budget.csv must contain exactly one contingency row.")
    if total > Decimal("40000.00"):
        errors.append(f"Budget total {total} exceeds INR 40000.00.")
    with open(os.path.join(ROOT, "brief.md"), encoding="utf-8") as handle:
        brief = handle.read().lower()
    for phrase in ["total", "assumption", "not verified"]:
        if phrase not in brief:
            errors.append(f"brief.md must mention {phrase}.")

print(json.dumps({"ok": not errors, "errors": errors}))
sys.exit(0 if not errors else 2)
