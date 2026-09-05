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

"""Tests for the friendly sandbox name generator."""

from __future__ import annotations

import random
import re

from smolvm._naming import CITIES, SCIENTISTS, generate_sandbox_name
from smolvm.types import _IDENTIFIER_PATTERN


def test_bare_scientist_when_nothing_taken() -> None:
    name = generate_sandbox_name(set())
    assert name.startswith("sbx-")
    scientist = name.removeprefix("sbx-")
    assert scientist in SCIENTISTS


def test_appends_city_when_scientist_taken() -> None:
    """If every bare scientist name is taken, fall through to scientist-city."""
    taken = {f"sbx-{s}" for s in SCIENTISTS}
    name = generate_sandbox_name(taken, rng=random.Random(0))

    assert name not in taken
    assert name.startswith("sbx-")
    parts = name.removeprefix("sbx-").split("-")
    assert len(parts) == 2
    scientist, city = parts
    assert scientist in SCIENTISTS
    assert city in CITIES


def test_returned_name_passes_vm_id_validation() -> None:
    """Generated names must satisfy the VMConfig identifier pattern."""
    for _ in range(20):
        name = generate_sandbox_name()
        assert re.fullmatch(_IDENTIFIER_PATTERN, name), name


def test_custom_prefix() -> None:
    """A caller-supplied prefix replaces the default ``sbx`` in generated names."""
    name = generate_sandbox_name(set(), prefix="codex")
    assert name.startswith("codex-")
    scientist = name.removeprefix("codex-")
    assert scientist in SCIENTISTS


def test_custom_prefix_with_collision() -> None:
    taken = {f"codex-{s}" for s in SCIENTISTS}
    name = generate_sandbox_name(taken, prefix="codex", rng=random.Random(0))

    assert name.startswith("codex-")
    parts = name.removeprefix("codex-").split("-")
    assert len(parts) == 2
    assert parts[0] in SCIENTISTS
    assert parts[1] in CITIES


def test_falls_back_to_hex_when_space_exhausted() -> None:
    """Final fallback guarantees uniqueness even if scientists+cities collide."""
    taken = {f"sbx-{s}" for s in SCIENTISTS}
    taken |= {f"sbx-{s}-{c}" for s in SCIENTISTS for c in CITIES}

    name = generate_sandbox_name(taken)

    assert name not in taken
    assert name.startswith("sbx-")
    assert re.fullmatch(_IDENTIFIER_PATTERN, name)
