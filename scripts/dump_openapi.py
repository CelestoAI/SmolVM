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

"""Dump the SmolVM HTTP API's OpenAPI spec to ``ts/openapi.json``.

``ts/openapi.json`` is the committed contract the TypeScript client is
generated from. The Python server is its source of truth, so this script
re-dumps the spec straight from ``create_app().openapi()`` whenever a
route changes. Run it (or ``npm run sync`` in ``ts/``, which chains this
with the client codegen) after editing any endpoint, then commit the
result.

Usage::

    uv run --extra dashboard python scripts/dump_openapi.py

The CI drift check runs the same command and fails if the committed file
differs from a fresh dump — catching a route change that forgot to
regenerate.
"""

from __future__ import annotations

import json
from pathlib import Path

from smolvm.server.app import create_app

# scripts/dump_openapi.py -> repo root -> ts/openapi.json
SPEC_PATH = Path(__file__).resolve().parent.parent / "ts" / "openapi.json"


def main() -> None:
    spec = create_app().openapi()
    SPEC_PATH.write_text(json.dumps(spec, indent=2) + "\n")
    print(f"Wrote {SPEC_PATH}")


if __name__ == "__main__":
    main()
