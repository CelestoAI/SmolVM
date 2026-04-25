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

"""Friendly auto-names for sandboxes (e.g. ``sbx-einstein``).

When a user runs ``smolvm create`` without a name we pick a scientist's last
name. If that name is already taken we append a city for disambiguation
(``sbx-tesla-london``). A short hex suffix is used as a final fallback to
guarantee uniqueness when the scientist+city space is exhausted.
"""

from __future__ import annotations

import random
from uuid import uuid4

SANDBOX_PREFIX = "sbx"

SCIENTISTS: tuple[str, ...] = (
    "einstein",
    "tesla",
    "newton",
    "darwin",
    "curie",
    "hawking",
    "feynman",
    "turing",
    "lovelace",
    "edison",
    "galileo",
    "kepler",
    "maxwell",
    "planck",
    "bohr",
    "schrodinger",
    "heisenberg",
    "fermi",
    "oppenheimer",
    "mendel",
    "pasteur",
    "watson",
    "crick",
    "sagan",
    "hubble",
    "dirac",
    "faraday",
    "archimedes",
    "copernicus",
    "descartes",
    "euler",
    "gauss",
    "ramanujan",
    "noether",
    "hopper",
    "babbage",
    "pauling",
    "fleming",
    "jenner",
    "mendeleev",
    "rutherford",
    "ohm",
    "volta",
    "ampere",
    "joule",
    "kelvin",
    "leibniz",
    "pythagoras",
    "riemann",
    "fibonacci",
)

CITIES: tuple[str, ...] = (
    "london",
    "paris",
    "tokyo",
    "berlin",
    "madrid",
    "rome",
    "vienna",
    "oslo",
    "lisbon",
    "dublin",
    "athens",
    "cairo",
    "mumbai",
    "delhi",
    "beijing",
    "seoul",
    "sydney",
    "toronto",
    "boston",
    "chicago",
    "denver",
    "austin",
    "miami",
    "dallas",
    "seattle",
    "portland",
    "brussels",
    "prague",
    "warsaw",
    "helsinki",
    "stockholm",
    "amsterdam",
    "geneva",
    "kyoto",
    "bangkok",
    "jakarta",
    "bogota",
    "lima",
    "montreal",
    "vancouver",
)


def generate_sandbox_name(
    existing: set[str] | frozenset[str] | None = None,
    *,
    prefix: str = SANDBOX_PREFIX,
    rng: random.Random | None = None,
) -> str:
    """Return a friendly sandbox name like ``sbx-einstein`` or ``sbx-tesla-london``.

    Args:
        existing: Names that are already in use. The returned name is guaranteed
            to be absent from this set. When ``None``, no collision check is
            performed (callers without state-manager access can still get a
            scientist-style name and rely on the storage layer to reject
            duplicates).
        prefix: Namespace prefix for the generated name. Defaults to ``"sbx"``.
            Presets pass their own name (e.g. ``"codex"``) so sandboxes are
            visually grouped by harness type.
        rng: Optional ``random.Random`` for deterministic tests.

    Strategy:
        1. Try a bare ``{prefix}-{scientist}`` for a few random picks.
        2. On collision, append a random city: ``{prefix}-{scientist}-{city}``.
        3. If everything collides (vanishingly unlikely), fall back to a hex
           suffix that guarantees uniqueness.
    """
    taken = existing or frozenset()
    picker = rng or random

    for _ in range(len(SCIENTISTS)):
        scientist = picker.choice(SCIENTISTS)
        bare = f"{prefix}-{scientist}"
        if bare not in taken:
            return bare
        for _ in range(len(CITIES)):
            city = picker.choice(CITIES)
            qualified = f"{prefix}-{scientist}-{city}"
            if qualified not in taken:
                return qualified

    return f"{prefix}-{picker.choice(SCIENTISTS)}-{uuid4().hex[:6]}"
