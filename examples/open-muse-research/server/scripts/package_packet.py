#!/usr/bin/env python3
import base64
import os
import sys
import zipfile

root, target, encoded = sys.argv[1], sys.argv[2], sys.argv[3]
names = ["brief.md", "itinerary.md", "budget.csv", "sources.json"]
with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for name in names:
        archive.write(os.path.join(root, name), arcname=name)
with open(target, "rb") as source, open(encoded, "w", encoding="ascii") as output:
    output.write(base64.b64encode(source.read()).decode("ascii"))
