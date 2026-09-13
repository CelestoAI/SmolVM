#!/usr/bin/env python3
import csv
import json
import sys
from decimal import ROUND_HALF_UP, Decimal


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def main():
    source, output = sys.argv[1], sys.argv[2]
    with open(source, encoding="utf-8") as handle:
        items = json.load(handle)
    rows = []
    subtotal = Decimal("0")
    for item in items:
        total = money(item["quantity"]) * money(item["unitCostInr"])
        subtotal += total
        rows.append(
            [
                item["category"],
                item["item"],
                item["quantity"],
                money(item["unitCostInr"]),
                total,
                item["sourceId"],
            ]
        )
    contingency = money(subtotal * Decimal("0.10"))
    rows.append(["contingency", "10% contingency", 1, contingency, contingency, ""])
    with open(output, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["category", "item", "quantity", "unit_cost_inr", "total_inr", "source_id"])
        writer.writerows(rows)


if __name__ == "__main__":
    main()
